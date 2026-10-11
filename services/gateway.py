#!/usr/bin/env python3
"""Dendra gateway: serves the chat-completions API (/v1/models, /v1/chat/completions), routes every
chat request to the Dendra network (escrow -> committee -> confidential inference on GPU -> verdict
and on-chain payment) and returns the response in the chat-completions format.

Any chat-completions client pointed at this gateway uses Dendra without other changes.
The content stays sealed; the chain only sees metadata and a hash.

Run: python3 gateway.py   (port 8651; DENDRA_GW_PORT to change it).
Chat interface: add a chat-completions connection to http://localhost:8651/v1 with the key "dendra".
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
from modea.exposure import InvalidFlag, listens_locally, public_enabled
try:                                  # Illegal content filter
    import content_filter as cf
except Exception as _cf_err:          # pragma: no cover
    cf = None
    # A neutral message on purpose: the decision is taken below, once the listen address is known.
    sys.stderr.write(f"[gateway] WARNING: content_filter unavailable ({type(_cf_err).__name__}) - "
                     f"the regex floor will not be active; the decision follows the listen address.\n")

RELAY = os.environ.get("DENDRA_RELAY", "http://127.0.0.1:8645")
PORT = int(os.environ.get("DENDRA_GW_PORT", "8651"))
FEE = int(os.environ.get("DENDRA_FEE", "4500"))      # udndr: 0.0045 DNDR per job (escrow)
REWARD = int(os.environ.get("DENDRA_REWARD", "1500"))  # udndr: 0.0015 DNDR per committee miner
TIMEOUT = int(os.environ.get("DENDRA_TIMEOUT", "240"))
MODEL_ID = "dendra-network"

# --- Gateway security ---
GW_HOST = os.environ.get("DENDRA_GW_HOST", "127.0.0.1")            # localhost by default (not 0.0.0.0)

# -------------------------------------------------------------------------------------------------
# Moderation floor: required by default, and decided by a fact.
#
# An environment variable may harden a security guard, never enable it. Keying the guard on
# `DENDRA_PUBLIC`, a declaration by the operator, while `docker-compose.yml` sets
# `DENDRA_GW_HOST: "0.0.0.0"`, would let a gateway reachable from outside run with no filter simply
# because nobody declared `PUBLIC=1`. So the listen address decides:
#   · non-loopback bind and no filter: refuse to start (the default, no variable needed);
#   · loopback bind and no filter: tolerated (a developer machine), with a warning;
#   · `DENDRA_ALLOW_NO_FILTER=1`: an explicit, named bypass, never a default;
#   · `DENDRA_REQUIRE_FILTER=1`: strict mode, refuse even on loopback.
# The regex floor is the only content filter by project decision, with known false negatives, and the
# project never claims that illegal content is filtered; that decision assumes the floor is always
# present. `listens_locally` is the shared reading of the address (`modea.exposure`): it counts the empty
# string as a network bind, since `bind("")` means INADDR_ANY.
_EXPOSED = not listens_locally(GW_HOST)
if cf is None:
    if os.environ.get("DENDRA_REQUIRE_FILTER", "0") == "1":
        sys.stderr.write("[gateway] refused: DENDRA_REQUIRE_FILTER=1 and content_filter is unavailable.\n")
        sys.exit(1)
    if _EXPOSED and os.environ.get("DENDRA_ALLOW_NO_FILTER", "0") != "1":
        sys.stderr.write(
            f"[gateway] refusing to start: the gateway listens on {GW_HOST} (reachable from outside)\n"
            "  and content_filter - the deterministic regex floor - is not importable.\n"
            "  Serving this way would expose a gateway without any moderation. This refusal depends on\n"
            "  no variable: it depends on the listen address, which is a fact, not a declaration.\n"
            "  Fix the import, or bind to loopback, or take it explicitly: DENDRA_ALLOW_NO_FILTER=1.\n")
        sys.exit(1)
    if _EXPOSED:
        sys.stderr.write(
            f"[gateway] WARNING: DENDRA_ALLOW_NO_FILTER=1 - gateway exposed on {GW_HOST} without a\n"
            "  content filter. Explicit bypass, taken by the operator. No moderation is active.\n")
    else:
        sys.stderr.write(
            "[gateway] WARNING: content_filter unavailable - tolerated because the bind is loopback\n"
            f"  ({GW_HOST}). Any network exposure will require the filter or DENDRA_ALLOW_NO_FILTER=1.\n")
API_KEY = os.environ.get("DENDRA_API_KEY", "")                     # if non-empty -> Bearer required
MAX_BODY = int(os.environ.get("DENDRA_MAX_BODY", str(256 * 1024)))  # body cap (bytes) -> 413
MAX_MESSAGES = int(os.environ.get("DENDRA_MAX_MESSAGES", "64"))    # max number of messages
MAX_PROMPT_CHARS = int(os.environ.get("DENDRA_MAX_PROMPT_CHARS", str(48_000)))  # prompt length
QUOTA_FILE = os.environ.get("DENDRA_QUOTA_FILE", "")              # persists counters; empty = memory only
# X-Forwarded-For is trusted only behind a trusted reverse proxy (otherwise the client can forge it).
TRUST_PROXY = os.environ.get("DENDRA_TRUST_PROXY", "0") == "1"

# --- Dynamic pricing by workload: base + price per token x (estimated input tokens + capped output).
#     A one-sentence prompt costs less than a fifty-sentence one. DENDRA_DYN_PRICING=0 sets a flat rate.
DYN_PRICING = os.environ.get("DENDRA_DYN_PRICING", "1") == "1"
BASE_FEE = int(os.environ.get("DENDRA_BASE_FEE", "500"))     # udndr: minimum flat fee
PER_TOKEN = int(os.environ.get("DENDRA_PER_TOKEN", "10"))    # udndr per token (input + output)
OUT_ALLOW = int(os.environ.get("DENDRA_OUT_ALLOW", "2048"))            # output cap for the free tier (= miner num_predict)
PAID_OUT_ALLOW = int(os.environ.get("DENDRA_PAID_OUT_ALLOW", "8192"))  # output cap for the paid tier (higher limit)


def price_job(prompt):
    """Return (fee, reward_per_miner) proportional to workload.

    The committee size comes from the chain (read through `dc`), never from a hard-coded factor: in
    optimistic mode k=1, so `reward = fee // 1` and the primary is paid for the real work (the 85/15
    split is applied by the chain at settlement, not modelled here). The client holds no model of the
    protocol, since a client-side copy of a protocol rule would eventually diverge."""
    in_tok = max(1, len(prompt) // 4)                    # approx ~4 characters per token
    fee = BASE_FEE + PER_TOKEN * (in_tok + OUT_ALLOW)
    k = dc._submit_committee_k()                         # k=1 in optimistic mode (read from the chain mode)
    reward = max(1, fee // k)
    return reward * k, reward

# --- Free tier: the user does not pay; a subsidy account (the Reserve, "bob") settles the miners on chain,
#     bounded by anti-abuse quotas (global per day and per device per day) so the Reserve is neither drained
#     nor farmed. This is the subsidised work flow.
FREE_TIER = os.environ.get("DENDRA_FREE_TIER", "1") == "1"
SUBSIDY_CLIENT = os.environ.get("DENDRA_SUBSIDY_CLIENT", "bob")        # Reserve account that subsidizes
PAID_CLIENT = os.environ.get("DENDRA_CLIENT", "alice")                # account if free tier is disabled
FREE_TOK_DAY = int(os.environ.get("DENDRA_FREE_TOK_DAY", "500000"))   # free tokens per day (global)
FREE_TOK_IP = int(os.environ.get("DENDRA_FREE_TOK_IP", "50000"))      # free tokens per day per device
_QLOCK = threading.Lock()
_QUOTA = {"day": "", "glob": 0, "ip": {}}   # cumulative tokens consumed (input+output), not requests


def _check_auth(auth_header):
    """If DENDRA_API_KEY is set, require 'Bearer <key>' (constant-time comparison)."""
    if not API_KEY:
        return True                                   # no key: local development mode (bind 127.0.0.1)
    return hmac.compare_digest(auth_header or "", "Bearer " + API_KEY)


def _quota_roll():
    d = datetime.date.today().isoformat()
    if _QUOTA["day"] != d:                            # daily reset
        _QUOTA["day"], _QUOTA["glob"], _QUOTA["ip"] = d, 0, {}
        _quota_save()


def _quota_load():
    """Reload the counters from disk, so that a restart does not reopen the free tap.
    Fails closed: when the file exists but cannot be read, the counters are not reset to 0; the global
    quota is marked exhausted for the day and the error logged. A truncated file (a crash, a full disk)
    must never grant free service."""
    if QUOTA_FILE and os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, encoding="utf-8") as f:
                _QUOTA.update(json.load(f))
        except Exception as e:
            # Fail closed, in a way that survives the first `_quota_roll`: it zeroes day, glob and ip whenever
            # the stored day differs from today, and a load that raised leaves the day empty. Stamping today
            # makes the refusal last until the real daily reset.
            _QUOTA["day"] = datetime.date.today().isoformat()
            _QUOTA["glob"] = FREE_TOK_DAY  # fail-closed: free tier closed until the daily reset
            sys.stderr.write(f"[gateway] quota unreadable ({type(e).__name__}) -> fail-closed. "
                             f"Fix/remove {QUOTA_FILE}.\n")


def _quota_save():
    """Atomic write (a temporary file, then os.replace): a crash mid-write leaves the old file intact, never
    a truncated one."""
    if QUOTA_FILE:
        try:
            tmp = QUOTA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_QUOTA, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, QUOTA_FILE)
        except Exception as e:
            sys.stderr.write(f"[gateway] quota save failed ({type(e).__name__})\n")


def quota_reserve(key, est):
    """Debits a high estimate before the job, under a single lock, so that concurrent requests cannot all
    pass the check together (no time-of-check to time-of-use gap)."""
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
    """After the job: replace the estimate with the real consumption."""
    with _QLOCK:
        _quota_roll()
        delta = int(real) - int(est)
        _QUOTA["glob"] = max(0, _QUOTA["glob"] + delta)
        _QUOTA["ip"][key] = max(0, _QUOTA["ip"].get(key, 0) + delta)
        _quota_save()


def messages_to_prompt(messages):
    """Flatten the chat-completions message list into a single prompt (the miner infers this text)."""
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
    """Cut at the last complete sentence (. ! ?), so that the answer does not end mid-word."""
    text = (text or "").rstrip()
    cut = max(text.rfind(". "), text.rfind("! "), text.rfind("? "),
              text.rfind(".\n"), text.rfind("!\n"), text.rfind("?\n"))
    return text[:cut + 1].rstrip() if cut > len(text) * 0.5 else text


def run_dendra(messages, ip="?", account=None):
    prompt = messages_to_prompt(messages)
    out_allow = OUT_ALLOW if FREE_TIER else PAID_OUT_ALLOW   # tier: free (2048) vs paid (8192)
    key = account or ip                  # quota per account if authenticated, otherwise per IP
    est = max(1, len(prompt) // 4) + out_allow   # high estimate: what the escrow covers
    if FREE_TIER:
        ok, why = quota_reserve(key, est)        # reserved before the job, against races
        if not ok:
            return (f"[Free tier] {why}. Try again tomorrow (or switch to paid mode).",
                    {"quota_block": why, "dendra_failure": {"http": 429, "code": "quota_exhausted"}})
        client = SUBSIDY_CLIENT          # the Reserve pays the miners instead of the user
    else:
        client = PAID_CLIENT
    try:
        if DYN_PRICING:                  # pricing by real workload (two phases: maximal escrow, then the actual)
            r = dc.quick_metered(prompt, BASE_FEE, PER_TOKEN, out_allow, RELAY, client=client, timeout=TIMEOUT)
        else:                            # flat rate
            r = dc.quick(prompt, FEE, REWARD, RELAY, client=client, timeout=TIMEOUT)
    except Exception as e:               # no raw error leaked to the client
        if FREE_TIER:
            quota_settle(key, est, 0)    # job failed -> release the reservation
        return ("[Dendra] the network could not process the request (please retry).",
                {"error": type(e).__name__,
                 "dendra_failure": {"http": 502, "code": "upstream_error"}})
    if not r or "error" in r:
        if FREE_TIER:
            quota_settle(key, est, 0)    # job not returned -> release the reservation
        # The marker is written, never inferred from an absence: `r` may legitimately be None here,
        # and a caller that deduced failure from "no error key" would read that as a success.
        _meta = dict(r) if isinstance(r, dict) else {}
        _meta["dendra_failure"] = {"http": 502, "code": "no_result"}
        return "[Dendra] the network could not process the request (please retry).", _meta
    answer = r.get("answer")
    if not answer:
        # A job that came back with no answer is a failed job, not a completion with an empty text, so it is
        # never returned as HTTP 200 with finish_reason "stop".
        if FREE_TIER:
            quota_settle(key, est, 0)
        _meta = dict(r)
        _meta["dendra_failure"] = {"http": 504, "code": "no_committee_response"}
        return "[Dendra] no committee response within the deadline (please retry).", _meta
    if r.get("truncated"):               # response hit the cap -> clean ending + prompt to continue
        answer = _trim_sentence(answer) + (
            "\n\n*— Response reached the length limit. Type \"continue\" for the rest. —*")
    toks = int(r.get("in_tok", 0)) + int(r.get("out_tok", 0))
    if toks <= 0:                        # flat-rate/fallback: estimate from prompt + response
        toks = max(1, len(prompt) // 4) + max(1, len(answer) // 4)
    if FREE_TIER:
        quota_settle(key, est, toks)     # replace the estimate with the real consumption
    return answer, r


# CORS: "*" would let a malicious site trigger paid jobs through a victim's browser (the chat interface's
# backend calls the gateway server-side, so it does not need CORS). The empty default sends no CORS header,
# and the browser blocks every cross-origin call. To allow one browser front end:
# DENDRA_CORS_ORIGIN=http://localhost:8080.
CORS_ORIGIN = os.environ.get("DENDRA_CORS_ORIGIN", "")

# Job metadata: a plain chat-completions response is opaque (usage 0, no job_id, no cost). The gateway adds
# a non-standard field `dendra` (strict clients ignore it) and a real `usage` (billed tokens the client can
# verify, never the miner's own declaration). Nothing in it is secret: job_id, committee, beacon and fees are
# already on chain, and the plaintext and session keys never appear. audit_state="pending" means the VRF audit
# lands after settlement; follow it on the Proof feed (/proof) and the explorer with job_id. Disable with
# DENDRA_EXPOSE_META=0.
EXPOSE_META = os.environ.get("DENDRA_EXPOSE_META", "1") != "0"
# Public URL of The Proof (the_proof.py, read-only). Default = relative path "/proof" (reverse-proxy in front
# of the gateway); set DENDRA_PROOF_URL=https://.../proof (or http://<host>:8090) if The Proof is served elsewhere.
PROOF_URL = os.environ.get("DENDRA_PROOF_URL", "/proof")


def _failure(meta):
    """Return the (http, code) of a failed job, or None when the job really produced an answer.

    It reads an explicit marker and nothing else. A failure returned as a successful chat completion
    (HTTP 200, `finish_reason: "stop"`, the error sentence as the assistant's message) would make no SDK
    raise or retry, and a caller could not tell it from a model that answered that sentence.

    Deducing the failure from a missing field would not work either (an absent field and a zero value
    look the same), so every failing path writes the marker.
    """
    if not isinstance(meta, dict):
        return None
    f = meta.get("dendra_failure")
    if not isinstance(f, dict):
        return None
    return int(f.get("http", 502)), str(f.get("code", "upstream_error"))


def _dendra_meta(r):
    """Extract the (non-secret) `dendra` block from the client return. None if the job did not complete (no jid)."""
    if not EXPOSE_META or not isinstance(r, dict) or not r.get("jid"):
        return None
    comm = r.get("committee") or []
    # Whether the chain accepted the settlement, read from the settlement's own answer. `audit_state` is a
    # constant (the audit lands later) and says nothing about it: a refused settlement left the job open,
    # so no audit will follow and the client's escrow comes back only when the job expires unserved.
    # Three states, never two: an answer that does not say is `null`, not `false` and not `true`.
    s = r.get("settle")
    settled = s.get("settled") if isinstance(s, dict) and isinstance(s.get("settled"), bool) else None
    return {
        "job_id": r.get("jid"),
        "miner_id": comm[0] if len(comm) == 1 else None,   # k=1 optimistic: the primary; otherwise see committee
        "committee": comm,
        "beacon": r.get("beacon"),                          # public VRF seed of the assignment (anti-grinding)
        "settled": settled,                                 # true / false / null (not known)
        "audit_state": "pending",                           # the sampled audit lands after; track via The Proof
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
        # Behind a trusted reverse proxy (DENDRA_TRUST_PROXY=1), the only X-Forwarded-For entry the client
        # cannot forge is the last one, the one the proxy appended: the first entry is whatever the client
        # sent, and trusting it would let anyone bypass the per-device quotas. The rightmost entry is correct
        # for one trusted hop (the documented deployment); without TRUST_PROXY the socket IP is used.
        if TRUST_PROXY:
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if xff:
                return xff
        return self.client_address[0]

    def _acct(self):
        # The account is hash(key) plus the IP, so the quota is really per device: keyed by the global key
        # alone, every legitimate client would share one bucket, and one greedy client could exhaust
        # FREE_TOK_IP for everyone.
        ip = self._client_ip()
        return ("acct:" + hashlib.sha256(API_KEY.encode()).hexdigest()[:12] + "|" + ip) if API_KEY else ip

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/health", "/v1"):                 # public health (no secret)
            self._json(200, {"status": "ok", "service": "dendra-gateway", "relay": RELAY})
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
        # Body cap. `Content-Length` comes from the client, and an upper bound alone would let a negative value
        # through, where `read(-1)` reads until EOF: it is bounded on both sides.
        if n < 0 or n > MAX_BODY:                        # body too large (or negative Content-Length)
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
        if cf is not None:                               # Illegal content filter — before any escrow,
            _flat = messages_to_prompt(msgs)             #       on the cleartext, which never leaves the gateway
            _v = cf.screen(_flat)
            if _v.blocked:
                cf.log_refusal(self._acct(), _v, _flat)  # log without the content (hash16 only)
                self._json(403, {"error": "content refused", "code": "illegal_content"}); return
        stream = bool(req.get("stream"))
        answer, meta = run_dendra(msgs, self.client_address[0], account=self._acct())
        if cf is not None:                               # screen the output (anti-jailbreak — an illegal response
            _ov = cf.screen_output(answer)               #       despite a benign prompt). No-op if
            if _ov.blocked:                              #       DENDRA_SCREEN_OUTPUT!=1. Covers stream and non-stream
                cf.log_refusal(self._acct(), _ov, answer)#       (before the send branch). Log without content.
                self._json(403, {"error": "content refused", "code": "illegal_content"}); return
        # A failed job gets an error status, decided here: this is the last point where a status can be
        # chosen, since the streaming branch below sends `200` with its headers. The check sits before the
        # branch, so it covers both the streamed and the plain response.
        _fail = _failure(meta)
        if _fail:
            _http, _code = _fail
            self._json(_http, {"error": {
                "message": answer,
                "type": "insufficient_quota" if _http == 429 else "upstream_unavailable",
                "code": _code,
                "param": None,
            }})
            return

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        dmeta = _dendra_meta(meta)                       # Non-secret verifiability block (None if the job did not complete)
        in_tok = int(meta.get("in_tok", 0)) if isinstance(meta, dict) else 0
        out_tok = int(meta.get("out_tok", 0)) if isinstance(meta, dict) else 0

        if not stream:
            resp = {
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                # Real usage: the billed tokens, which the client can verify
                "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                          "total_tokens": in_tok + out_tok},
            }
            if dmeta:
                resp["dendra"] = dmeta
            self._json(200, resp)
            return

        # SSE streaming: send the response word by word so a chat interface renders it smoothly
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
            if dmeta:   # Non-standard final chunk: real usage + dendra block (strict clients ignore it)
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
    # Fail closed on exposure: a weak or default key such as "dendra", or a key that is too short, is rejected.
    # The guard also keys on DENDRA_PUBLIC=1, not only on GW_HOST: an exposure through a reverse proxy or a
    # Docker port mapping keeps GW_HOST at 127.0.0.1 inside the container, which would otherwise leave
    # authentication off.
    _WEAK_KEYS = {"dendra", "changeme", "change-me", "test", "default", "password", "secret", "admin", "key"}
    # GW_HOST=0.0.0.0 in the compose file is a bind between containers (chat interface to gateway), not a public
    # exposure, so it does not stop the closed stack from booting. The public exposure signal is explicit,
    # DENDRA_PUBLIC=1, set by any exposed stack, including behind a reverse proxy or a port mapping where GW_HOST
    # stays on loopback. The flag is read by the shared reader (`modea.exposure`): an unexpected value refuses
    # the start instead of falling back to "not public", and `bind("")` counts as a network bind.
    try:
        _PUBLIC = public_enabled(os.environ.get("DENDRA_PUBLIC"))
    except InvalidFlag as e:
        print(f"[gateway] refused: {e}", flush=True)
        sys.exit(2)
    _nonlocal = not listens_locally(GW_HOST)
    # (1) never a non-local endpoint without a key, even in the closed stack.
    if _nonlocal and not API_KEY:
        print(f"[gateway] refused: non-local bind ({GW_HOST}) without DENDRA_API_KEY. "
              f"Set a key (DENDRA_API_KEY=...) or bind 127.0.0.1.", flush=True)
        sys.exit(2)
    # (2) an explicit public exposure requires a strong key (this covers the reverse proxy and port mapping case).
    if _PUBLIC:
        if not API_KEY:
            print("[gateway] refused: DENDRA_PUBLIC=1 without DENDRA_API_KEY.", flush=True)
            sys.exit(2)
        if API_KEY.strip().lower() in _WEAK_KEYS:
            print("[gateway] refused: DENDRA_PUBLIC=1 with a weak or default key. "
                  "Set a real DENDRA_API_KEY (>=24 chars, e.g. `openssl rand -hex 24`).", flush=True)
            sys.exit(2)
        if len(API_KEY) < 16:
            print("[gateway] refused: DENDRA_PUBLIC=1 with a DENDRA_API_KEY that is too short (<16 chars). "
                  "Generate `openssl rand -hex 24`.", flush=True)
            sys.exit(2)
        # (3) Under public exposure, the deterministic in-process regex floor (including the CSAM pattern) is the
        #     only content filter and the mandatory baseline. The optional LLM classifier (llama-guard) is
        #     disabled for good (a launch decision): never required, never called. Only the importability of the
        #     regex floor is enforced, so a public gateway never boots without any content filter. This is a
        #     testnet posture, not a compliance guarantee.
        if cf is None:
            print("[gateway] refused: DENDRA_PUBLIC=1 but content_filter (the regex floor) is not importable. "
                  "The deterministic regex floor is the mandatory content baseline.", flush=True)
            sys.exit(2)
        print("[gateway] moderation: deterministic regex floor only (incl. CSAM); LLM classifier permanently "
              "disabled. Do not rely on this for legal-content compliance.", flush=True)
    # (4) A free tier reachable from outside needs counters that outlive the process: they bound a tap that
    #     spends the project's own Reserve. Held in memory only, the daily cap would bound one run, not one
    #     day: a crash loop, a redeploy or `docker compose restart` would hand out the full allowance again,
    #     and `_quota_load`'s fail-closed branch would have nothing to read. As for a non-local bind without
    #     a key, the trigger is the listen address, a fact rather than a declaration. Loopback is left alone,
    #     since only this machine can spend it. The shipped stack passes DENDRA_QUOTA_FILE
    #     (docker-compose.yml, gateway service), so the standard path never meets this refusal.
    if _nonlocal and FREE_TIER and not QUOTA_FILE:
        print(f"[gateway] refused: non-local bind ({GW_HOST}) with the free tier on and no "
              "DENDRA_QUOTA_FILE. The subsidy counters would live in memory only, so every restart "
              "would reopen the full daily allowance. Set DENDRA_QUOTA_FILE=<path on a volume>, or "
              "bind 127.0.0.1, or run paid mode (DENDRA_FREE_TIER=0).", flush=True)
        sys.exit(2)
    _quota_load()
    auth = "Bearer required" if API_KEY else "open (local only)"
    print(f"[gateway] chat-completions API -> Dendra on http://{GW_HOST}:{PORT}/v1   (relay {RELAY}; auth {auth})", flush=True)
    mode = "dynamic (per-token, real output)" if DYN_PRICING else "flat-rate"
    if FREE_TIER:
        print(f"[gateway] free tier active: subsidy '{SUBSIDY_CLIENT}' (Reserve); pricing {mode}; "
              f"quotas {FREE_TOK_DAY} tokens/day, {FREE_TOK_IP} tokens/day/account", flush=True)
        # The banner says whether the counters survive a restart: "quotas N/day" alone cannot tell a bound
        # that holds for a day from one that resets with the process.
        if QUOTA_FILE:
            print(f"[gateway] free-tier counters persist in {QUOTA_FILE}: a restart resumes today's tally, "
                  "and an unreadable file closes the tap until the daily reset.", flush=True)
        else:
            print("[gateway] free-tier counters are in memory only (no DENDRA_QUOTA_FILE): every restart "
                  "resets them, so those figures bound this run, not this day. Tolerated here because the "
                  f"bind is local ({GW_HOST}); a non-local bind refuses to start without a quota file.",
                  flush=True)
    else:
        print(f"[gateway] paid mode: account '{PAID_CLIENT}'; pricing {mode}", flush=True)
    ThreadingHTTPServer((GW_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
