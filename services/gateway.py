#!/usr/bin/env python3
"""Dendra <-> OpenAI gateway: exposes an OpenAI-compatible API (/v1/models, /v1/chat/completions)
that routes every chat request to the DENDRA NETWORK (escrow -> committee -> confidential inference
on GPU -> verdict + on-chain payment) and returns the response in OpenAI format.

=> Any OpenAI client (Open WebUI, etc.) pointed at this gateway uses Dendra without knowing it.
   The content stays encrypted/sealed; the chain only sees metadata + hash.

Run: python3 gateway.py   (port 8651; DENDRA_GW_PORT to change it).
Open WebUI: Connections -> OpenAI API -> URL http://localhost:8651/v1 , key "dendra".
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client as dc
try:                                  # ILLEGAL content filter
    import content_filter as cf
except Exception as _cf_err:          # pragma: no cover
    cf = None
    sys.stderr.write(f"[gateway] /!\\ content_filter UNAVAILABLE ({type(_cf_err).__name__}) -> NO "
                     f"content filter. Set DENDRA_REQUIRE_FILTER=1 to refuse serving without a filter.\n")
# Public exposure: require the filter (otherwise a silent fail-OPEN = server without legal moderation).
if os.environ.get("DENDRA_REQUIRE_FILTER", "0") == "1" and cf is None:
    sys.stderr.write("[gateway] DENDRA_REQUIRE_FILTER=1 but content_filter is unavailable -> STOP.\n")
    sys.exit(1)

RELAY = os.environ.get("DENDRA_RELAY", "http://127.0.0.1:8645")
PORT = int(os.environ.get("DENDRA_GW_PORT", "8651"))
FEE = int(os.environ.get("DENDRA_FEE", "4500"))      # udndr: 0.0045 DNDR per job (escrow)
REWARD = int(os.environ.get("DENDRA_REWARD", "1500"))  # udndr: 0.0015 DNDR per committee miner
TIMEOUT = int(os.environ.get("DENDRA_TIMEOUT", "240"))
MODEL_ID = "dendra-network"

# --- GATEWAY SECURITY ---
GW_HOST = os.environ.get("DENDRA_GW_HOST", "127.0.0.1")            # localhost by default (NOT 0.0.0.0)
API_KEY = os.environ.get("DENDRA_API_KEY", "")                     # if non-empty -> Bearer required
MAX_BODY = int(os.environ.get("DENDRA_MAX_BODY", str(256 * 1024)))  # body cap (bytes) -> 413
MAX_MESSAGES = int(os.environ.get("DENDRA_MAX_MESSAGES", "64"))    # max number of messages
MAX_PROMPT_CHARS = int(os.environ.get("DENDRA_MAX_PROMPT_CHARS", str(48_000)))  # prompt length
QUOTA_FILE = os.environ.get("DENDRA_QUOTA_FILE", "")              # persists counters (anti-reset)
# X-Forwarded-For is trusted ONLY behind a TRUSTED reverse-proxy (otherwise spoofable).
TRUST_PROXY = os.environ.get("DENDRA_TRUST_PROXY", "0") == "1"

# --- DYNAMIC PRICING (by workload): base + price/token x (estimated input tokens + capped output).
#     A 1-sentence prompt costs less than a 50-sentence one (the GPU works more). DENDRA_DYN_PRICING=0 -> flat rate.
DYN_PRICING = os.environ.get("DENDRA_DYN_PRICING", "1") == "1"
BASE_FEE = int(os.environ.get("DENDRA_BASE_FEE", "500"))     # udndr: minimum flat fee
PER_TOKEN = int(os.environ.get("DENDRA_PER_TOKEN", "10"))    # udndr per token (input + output)
OUT_ALLOW = int(os.environ.get("DENDRA_OUT_ALLOW", "2048"))            # output cap for the FREE tier (= miner num_predict)
PAID_OUT_ALLOW = int(os.environ.get("DENDRA_PAID_OUT_ALLOW", "8192"))  # output cap for the PAID tier (higher limit)


def price_job(prompt):
    """Return (fee, reward_per_miner) proportional to workload. Balanced escrow: fee = reward x 3 (committee)."""
    in_tok = max(1, len(prompt) // 4)                    # approx ~4 characters per token
    fee = BASE_FEE + PER_TOKEN * (in_tok + OUT_ALLOW)
    reward = max(1, fee // 3)
    return reward * 3, reward

# --- FREE TIER: the user does NOT pay; a SUBSIDY account (the Reserve = "bob")
#     settles the miners on-chain, bounded by anti-abuse QUOTAS (global/day + per-device/day)
#     to avoid draining the Reserve or being farmed. This is the "subsidized work" flow.
FREE_TIER = os.environ.get("DENDRA_FREE_TIER", "1") == "1"
SUBSIDY_CLIENT = os.environ.get("DENDRA_SUBSIDY_CLIENT", "bob")        # Reserve account that subsidizes
PAID_CLIENT = os.environ.get("DENDRA_CLIENT", "alice")                # account if free tier is disabled
FREE_TOK_DAY = int(os.environ.get("DENDRA_FREE_TOK_DAY", "500000"))   # free TOKENS per day (global)
FREE_TOK_IP = int(os.environ.get("DENDRA_FREE_TOK_IP", "50000"))      # free TOKENS per day per device
_QLOCK = threading.Lock()
_QUOTA = {"day": "", "glob": 0, "ip": {}}   # cumulative TOKENS consumed (input+output), not requests


def _check_auth(auth_header):
    """If DENDRA_API_KEY is set, require 'Bearer <key>' (CONSTANT-TIME comparison)."""
    if not API_KEY:
        return True                                   # no key -> local dev mode (bind 127.0.0.1)
    return hmac.compare_digest(auth_header or "", "Bearer " + API_KEY)


def _quota_roll():
    d = datetime.date.today().isoformat()
    if _QUOTA["day"] != d:                            # daily reset
        _QUOTA["day"], _QUOTA["glob"], _QUOTA["ip"] = d, 0, {}
        _quota_save()


def _quota_load():
    """Reload the counters from disk -> a restart no longer reopens the tap.
    FAIL-CLOSED: if the file exists but is UNREADABLE/corrupt, do NOT reset the
    counters to 0 (which would reopen the free tap) -> mark the GLOBAL quota as exhausted for the
    day + log the error. A truncated file (crash/full disk) must NEVER grant free service."""
    if QUOTA_FILE and os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, encoding="utf-8") as f:
                _QUOTA.update(json.load(f))
        except Exception as e:
            _QUOTA["glob"] = FREE_TOK_DAY  # fail-closed: free tier closed until the daily reset
            sys.stderr.write(f"[gateway] QUOTA unreadable ({type(e).__name__}) -> FAIL-CLOSED. "
                             f"Fix/remove {QUOTA_FILE}.\n")


def _quota_save():
    """ATOMIC write: tmp + os.replace -> a crash mid-write leaves
    the old file INTACT (no more truncated file that would reopen the tap on the next load)."""
    if QUOTA_FILE:
        try:
            tmp = QUOTA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_QUOTA, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, QUOTA_FILE)
        except Exception as e:
            sys.stderr.write(f"[gateway] QUOTA save failed ({type(e).__name__})\n")


def quota_reserve(key, est):
    """Anti-TOCTOU: under a SINGLE lock, DEBIT a HIGH estimate BEFORE the job.
    N concurrent requests can therefore no longer pass the check together."""
    est = int(est)
    with _QLOCK:
        _quota_roll()
        if _QUOTA["glob"] + est > FREE_TOK_DAY:
            return False, "global daily free-tier token quota reached"
        if _QUOTA["ip"].get(key, 0) + est > FREE_TOK_IP:
            return False, f"free-tier token quota reached ({FREE_TOK_IP}/day)"
        _QUOTA["glob"] += est
        _QUOTA["ip"][key] = _QUOTA["ip"].get(key, 0) + est
        _quota_save()
        return True, ""


def quota_settle(key, est, real):
    """AFTER the job: replace the estimate with the REAL consumption (corrects over/under-debit)."""
    with _QLOCK:
        _quota_roll()
        delta = int(real) - int(est)
        _QUOTA["glob"] = max(0, _QUOTA["glob"] + delta)
        _QUOTA["ip"][key] = max(0, _QUOTA["ip"].get(key, 0) + delta)
        _quota_save()


def messages_to_prompt(messages):
    """Flatten the OpenAI conversation into a single prompt (the miner infers this text)."""
    parts = []
    for m in messages or []:
        role, content = m.get("role", "user"), (m.get("content") or "")
        if isinstance(content, list):  # some clients send segments
            content = " ".join(seg.get("text", "") for seg in content if isinstance(seg, dict))
        if not content:
            continue
        if role == "system":
            parts.append(content)
        elif role == "assistant":
            parts.append("Assistant: " + content)
        else:
            parts.append("User: " + content)
    parts.append("Assistant:")
    return "\n".join(parts)


def _trim_sentence(text):
    """Cut at the last complete sentence (. ! ?) -> avoids ending mid-word (professional rendering)."""
    text = (text or "").rstrip()
    cut = max(text.rfind(". "), text.rfind("! "), text.rfind("? "),
              text.rfind(".\n"), text.rfind("!\n"), text.rfind("?\n"))
    return text[:cut + 1].rstrip() if cut > len(text) * 0.5 else text


def run_dendra(messages, ip="?", account=None):
    prompt = messages_to_prompt(messages)
    out_allow = OUT_ALLOW if FREE_TIER else PAID_OUT_ALLOW   # tier: free (2048) vs paid (8192)
    key = account or ip                  # quota per ACCOUNT if authenticated, otherwise per IP
    est = max(1, len(prompt) // 4) + out_allow   # HIGH estimate = what the escrow covers
    if FREE_TIER:
        ok, why = quota_reserve(key, est)        # reserve BEFORE the job (anti-race)
        if not ok:
            return (f"[Free tier] {why}. Try again tomorrow (or switch to paid mode).",
                    {"quota_block": why})
        client = SUBSIDY_CLIENT          # the Reserve pays the miners instead of the user
    else:
        client = PAID_CLIENT
    try:
        if DYN_PRICING:                  # pricing by REAL WORKLOAD (2 phases: max escrow -> settle the actual)
            r = dc.quick_metered(prompt, BASE_FEE, PER_TOKEN, out_allow, RELAY, client=client, timeout=TIMEOUT)
        else:                            # flat rate
            r = dc.quick(prompt, FEE, REWARD, RELAY, client=client, timeout=TIMEOUT)
    except Exception as e:               # no raw error leaked to the client
        if FREE_TIER:
            quota_settle(key, est, 0)    # job failed -> release the reservation
        return "[Dendra] the network could not process the request (please retry).", {"error": type(e).__name__}
    if not r or "error" in r:
        if FREE_TIER:
            quota_settle(key, est, 0)    # job not returned -> release the reservation
        return "[Dendra] the network could not process the request (please retry).", r
    answer = r.get("answer") or "(no committee response within the deadline)"
    if r.get("truncated"):               # response hit the CAP -> CLEAN ending + prompt to continue
        answer = _trim_sentence(answer) + (
            "\n\n*— Response reached the length limit. Type \"continue\" for the rest. —*")
    toks = int(r.get("in_tok", 0)) + int(r.get("out_tok", 0))
    if toks <= 0:                        # flat-rate/fallback: estimate from prompt + response
        toks = max(1, len(prompt) // 4) + max(1, len(answer) // 4)
    if FREE_TIER:
        quota_settle(key, est, toks)     # replace the estimate with the REAL consumption
    return answer, r


# SECURE CORS: "*" let a malicious site trigger PAID jobs
# via a victim's browser (the Open WebUI backend calls the gateway SERVER-side, not the
# browser -> CORS is useless for it). EMPTY default = NO CORS header -> the browser blocks all
# cross-origin. To allow a specific browser front-end: DENDRA_CORS_ORIGIN=http://localhost:8080.
CORS_ORIGIN = os.environ.get("DENDRA_CORS_ORIGIN", "")

# JOB METADATA: a plain OpenAI response is opaque (usage=0, no job_id, no cost). We expose a NON-STANDARD
# extension field `dendra` (strict OpenAI clients ignore it) plus a REAL `usage` (client-verifiable billed
# tokens — never the miner's self-declaration). NON-SECRET by construction: job_id/committee/beacon/fees are
# ALREADY on-chain; never the plaintext, never session keys. audit_state="pending" = the VRF audit lands
# AFTER settlement; track it via The Proof (/proof) and the explorer, using job_id. Disable: DENDRA_EXPOSE_META=0.
EXPOSE_META = os.environ.get("DENDRA_EXPOSE_META", "1") != "0"
# Public URL of The Proof (the_proof.py, read-only). Default = relative path "/proof" (reverse-proxy in front
# of the gateway); set DENDRA_PROOF_URL=https://.../proof (or http://<HOST>:8090) if The Proof is served elsewhere.
PROOF_URL = os.environ.get("DENDRA_PROOF_URL", "/proof")


def _dendra_meta(r):
    """Extract the (non-secret) `dendra` block from the client return. None if the job did not complete (no jid)."""
    if not EXPOSE_META or not isinstance(r, dict) or not r.get("jid"):
        return None
    comm = r.get("committee") or []
    return {
        "job_id": r.get("jid"),
        "miner_id": comm[0] if len(comm) == 1 else None,   # k=1 optimistic: THE primary; otherwise see committee
        "committee": comm,
        "beacon": r.get("beacon"),                          # public VRF seed of the assignment (anti-grinding)
        "audit_state": "pending",                           # the sampled audit lands AFTER; track via The Proof
        "cost_udndr": r.get("fee_actual"),
        "escrow_udndr": r.get("fee_escrow"),
        "verify": {"proof_endpoint": PROOF_URL, "query": f"dendrad query jobs get-job {r.get('jid')}"},
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if CORS_ORIGIN:  # CORS restricted to the configured origin (no more "*")
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        if CORS_ORIGIN:  # CORS restricted to the configured origin (no more "*")
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _authorized(self):
        return _check_auth(self.headers.get("Authorization", ""))

    def _client_ip(self):
        # Behind a TRUSTED reverse-proxy (DENDRA_TRUST_PROXY=1), the only X-Forwarded-For entry
        # NOT forgeable by the client is the LAST one (the one OUR proxy just appended). The old
        # code took the FIRST ([0]): the client sets "X-Forwarded-For: 1.2.3.4" itself -> the proxy
        # appends the real IP at the end -> [0] = the FORGED IP -> per-device quotas bypassable at
        # will. Rightmost = correct for 1 trusted hop (the documented deployment); without
        # TRUST_PROXY we keep the socket IP.
        if TRUST_PROXY:
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if xff:
                return xff
        return self.client_address[0]

    def _acct(self):
        # The "per-device" bucket was keyed BY A GLOBAL KEY -> all
        # legitimate clients shared ONE bucket (a single greedy client exhausted FREE_TOK_IP for
        # everyone = free DoS). Account = hash(key)+IP: the quota is genuinely per-device again.
        ip = self._client_ip()
        return ("acct:" + hashlib.sha256(API_KEY.encode()).hexdigest()[:12] + "|" + ip) if API_KEY else ip

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/health", "/v1"):                 # public health (no secret)
            self._json(200, {"status": "ok", "service": "dendra-openai-gateway", "relay": RELAY})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"}); return
        if p in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "dendra"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():                       # Bearer
            self._json(401, {"error": "unauthorized"}); return
        n = int(self.headers.get("Content-Length", 0) or 0)
        # PLAFOND DE CORPS. `Content-Length` est fourni par le CLIENT : le comparer au seul plafond HAUT
        # laisse passer les valeurs NEGATIVES, et `read(-1)` lit jusqu'a EOF — donc allocation illimitee
        # malgre la garde. Il faut borner des DEUX cotes. (Forme correcte de reference : capacity_server.py.)
        if n < 0 or n > MAX_BODY:                        # body too large (ou Content-Length negatif)
            self._json(413, {"error": "payload too large"}); return
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid json"}); return   # 400, not an empty 200
        msgs = req.get("messages", [])
        if not isinstance(msgs, list) or len(msgs) > MAX_MESSAGES:
            self._json(400, {"error": "too many messages"}); return
        total_chars = sum(len(str(m.get("content", ""))) for m in msgs if isinstance(m, dict))
        if total_chars > MAX_PROMPT_CHARS:               # giant prompt -> inflated escrow
            self._json(413, {"error": "prompt too long"}); return
        if cf is not None:                               # ILLEGAL content filter — BEFORE any escrow,
            _flat = messages_to_prompt(msgs)             #       on the cleartext, which never leaves the gateway
            _v = cf.screen(_flat)
            if _v.blocked:
                cf.log_refusal(self._acct(), _v, _flat)  # log WITHOUT the content (hash16 only)
                self._json(403, {"error": "content refused", "code": "illegal_content"}); return
        stream = bool(req.get("stream"))
        answer, meta = run_dendra(msgs, self.client_address[0], account=self._acct())
        if cf is not None:                               # screen the OUTPUT (anti-jailbreak — an illegal response
            _ov = cf.screen_output(answer)               #       despite a benign prompt). NO-OP if
            if _ov.blocked:                              #       DENDRA_SCREEN_OUTPUT!=1. Covers stream AND non-stream
                cf.log_refusal(self._acct(), _ov, answer)#       (before the send branch). Log WITHOUT content.
                self._json(403, {"error": "content refused", "code": "illegal_content"}); return
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        dmeta = _dendra_meta(meta)                       # NON-SECRET verifiability block (None if the job did not complete)
        in_tok = int(meta.get("in_tok", 0)) if isinstance(meta, dict) else 0
        out_tok = int(meta.get("out_tok", 0)) if isinstance(meta, dict) else 0

        if not stream:
            resp = {
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                # REAL usage = CLIENT-verifiable billed tokens, no more zeros mocking the UI
                "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                          "total_tokens": in_tok + out_tok},
            }
            if dmeta:
                resp["dendra"] = dmeta
            self._json(200, resp)
            return

        # SSE streaming: send the response word by word (smooth in Open WebUI)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        if CORS_ORIGIN:  # CORS restricted
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.end_headers()

        def send(delta, finish=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()

        try:
            send({"role": "assistant"})
            buf = ""
            for w in answer.split(" "):
                buf += w + " "
                if len(buf) >= 18:
                    send({"content": buf}); buf = ""; time.sleep(0.012)
            if buf:
                send({"content": buf})
            send({}, finish="stop")
            if dmeta:   # NON-STANDARD final chunk: real usage + dendra block (strict clients ignore it)
                tail = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                        "choices": [],
                        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                                  "total_tokens": in_tok + out_tok},
                        "dendra": dmeta}
                self.wfile.write(("data: " + json.dumps(tail) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


def main():
    # Fail-closed on exposure. A weak/default key like "dendra" or too-short keys are rejected. The guard also
    # keys on DENDRA_PUBLIC=1 (not only GW_HOST != loopback): an exposure via REVERSE-PROXY / DOCKER PORT-MAP
    # keeps GW_HOST at 127.0.0.1 inside the container, which would otherwise leave auth OFF.
    _WEAK_KEYS = {"dendra", "changeme", "change-me", "test", "default", "password", "secret", "admin", "key"}
    # Docker note: GW_HOST=0.0.0.0 in the compose file = INTER-CONTAINER bind (open-webui -> gateway), NOT a
    # public exposure -> we do not break the closed stack's boot for it. The public-exposure signal is EXPLICIT:
    # DENDRA_PUBLIC=1 (set by any truly exposed stack, including behind a reverse-proxy / port-map where GW_HOST
    # stays loopback). Two levels:
    _PUBLIC = os.environ.get("DENDRA_PUBLIC", "0").strip().lower() in ("1", "true", "yes")
    _nonlocal = GW_HOST not in ("127.0.0.1", "localhost", "::1")
    # (1) never a TOTALLY open non-local endpoint (no key) — true even in the closed stack.
    if _nonlocal and not API_KEY:
        print(f"[gateway] REFUSED: non-local bind ({GW_HOST}) without DENDRA_API_KEY. "
              f"Set a key (DENDRA_API_KEY=...) or bind 127.0.0.1.", flush=True)
        sys.exit(2)
    # (2) explicit PUBLIC exposure -> STRONG key required (closes the reverse-proxy/port-map gap).
    if _PUBLIC:
        if not API_KEY:
            print("[gateway] REFUSED: DENDRA_PUBLIC=1 without DENDRA_API_KEY.", flush=True)
            sys.exit(2)
        if API_KEY.strip().lower() in _WEAK_KEYS:
            print("[gateway] REFUSED: DENDRA_PUBLIC=1 with a WEAK/default key. "
                  "Set a real DENDRA_API_KEY (>=24 chars, e.g. `openssl rand -hex 24`).", flush=True)
            sys.exit(2)
        if len(API_KEY) < 16:
            print("[gateway] REFUSED: DENDRA_PUBLIC=1 with a DENDRA_API_KEY that is too short (<16 chars). "
                  "Generate `openssl rand -hex 24`.", flush=True)
            sys.exit(2)
        # (3) PUBLIC exposure -> the deterministic in-process regex floor (incl. the CSAM pattern) is the ONLY
        #     content filter and the mandatory baseline. The optional LLM classifier (llama-guard) is
        #     PERMANENTLY DISABLED (launch decision): never required, never called. We enforce only that the
        #     regex floor is importable, so a public gateway can never boot with NO content filter at all.
        #     This is a research/testnet posture, NOT a legal-content compliance guarantee.
        if cf is None:
            print("[gateway] REFUSED: DENDRA_PUBLIC=1 but content_filter (the regex floor) is not importable. "
                  "The deterministic regex floor is the mandatory content baseline.", flush=True)
            sys.exit(2)
        print("[gateway] moderation: deterministic REGEX floor only (incl. CSAM); LLM classifier permanently "
              "disabled. Do NOT rely on this for legal-content compliance.", flush=True)
    _quota_load()
    auth = "Bearer REQUIRED" if API_KEY else "OPEN (local only)"
    print(f"[gateway] OpenAI API -> Dendra on http://{GW_HOST}:{PORT}/v1   (relay {RELAY}; auth {auth})", flush=True)
    mode = "DYNAMIC (per-token, real output)" if DYN_PRICING else "FLAT-RATE"
    if FREE_TIER:
        print(f"[gateway] FREE TIER active: subsidy '{SUBSIDY_CLIENT}' (Reserve); pricing {mode}; "
              f"quotas {FREE_TOK_DAY} tokens/day, {FREE_TOK_IP} tokens/day/account", flush=True)
    else:
        print(f"[gateway] PAID mode: account '{PAID_CLIENT}'; pricing {mode}", flush=True)
    ThreadingHTTPServer((GW_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
