#!/usr/bin/env python3
"""Native Dendra faucet (replaces the ignite faucet).

POST {"address":"dendra1..."}  -> sends FAUCET_AMOUNT udndr from the FROM account (bob),
whose key lives in the shared keyring (volume /root/.dendra also mounted by the chain).
Sends are SERIALIZED (a lock) + we wait for tx inclusion -> no sequence collision.
GET /  -> health probe.

Env: DENDRA_NODE, DENDRA_CHAIN_ID, DENDRA_FAUCET_FROM=bob, DENDRA_FAUCET_AMOUNT=10000000,
     DENDRA_FAUCET_PORT=4500, DENDRA_HOME=/root/.dendra.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
CHAIN_ID = os.environ.get("DENDRA_CHAIN_ID", "dendra")
FROM = os.environ.get("DENDRA_FAUCET_FROM", "bob")
AMOUNT = os.environ.get("DENDRA_FAUCET_AMOUNT", "10000000")  # udndr = 10 DNDR
PORT = int(os.environ.get("DENDRA_FAUCET_PORT", "4500"))
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
DENOM = "udndr"
KB = ["--keyring-backend", "test", "--home", HOME]
NODEF = ["--node", NODE]
MAX_BODY = 8192          # a faucet request is an address plus a PoW; 8 KiB is already generous
_RE_ADDR = re.compile(r"^dendra1[0-9a-z]{38,70}$")
_LOCK = threading.Lock()

# --- ANTI-ABUSE (INCENTIVE testnet: without a cap, drain/Sybil is trivial). Cap per ADDRESS (cooldown) + per IP/day
#     + GLOBAL cap/day. Fail-closed (cap reached -> 429). PERSISTS state (survives restart = no re-drain).
#     OPTIONAL PoW tied to the address (anti-Sybil in public: each new address costs CPU). ---
ADDR_COOLDOWN = int(os.environ.get("DENDRA_FAUCET_ADDR_COOLDOWN", "86400"))  # 1 drip / address / 24 h
IP_DAILY = int(os.environ.get("DENDRA_FAUCET_IP_DAILY", "5"))                # max drips / IP / 24 h
DAILY_CAP = int(os.environ.get("DENDRA_FAUCET_DAILY_CAP", "2000"))           # GLOBAL cap / 24 h (anti-drain)
# Persistence: "" = RAM only (previous behavior). Default = under the DENDRA_HOME volume -> survives restart.
STATE_FILE = os.environ.get("DENDRA_FAUCET_STATE", os.path.join(HOME, "faucet-state.json"))
# PoW: 0 = off (closed testnet). >0 (e.g. 20) requires a 'pow' such that sha256(addr+':'+pow) has N leading zero bits.
POW_BITS = int(os.environ.get("DENDRA_FAUCET_POW_BITS", "0"))
_DAY = 86400.0
_RL_LOCK = threading.Lock()
_addr_last = {}    # addr -> ts of the last drip
_ip_hits = {}      # ip   -> [ts...] (24 h window)
_global_hits = []  # [ts...] global (24 h window)


# --- PoW CONTRACT: ONE DEFINITION, SHARED BY BOTH SIDES -------------------------------------------
# The expected token is a NONCE `pow` such that sha256("<address>:<nonce>") carries at least POW_BITS
# leading zero bits. The VERIFIER (server, `pow_ok`) and the SOLVER (client, `solve_pow`) both derive
# from the same digest `pow_digest`: two separate implementations that merely resemble each other end
# up diverging on a separator or an encoding, and that divergence makes the faucet unreachable while
# neither side reports a fault — the client believes it is paying the requested CPU, the server sees
# an invalid token.
POW_SEP = ":"


def pow_digest(addr, nonce):
    """Digest of the (address, nonce) pair.

    The nonce is bound TO THIS address: a token solved for another address is worthless, so every
    additional Sybil identity has to be paid for in CPU. That is the entire point of the faucet PoW.
    """
    return hashlib.sha256((str(addr) + POW_SEP + str(nonce)).encode()).digest()


def leading_zero_bits(digest):
    """Number of leading zero bits in the digest."""
    bits = 0
    for byte in digest:
        if byte:
            return bits + 8 - byte.bit_length()
        bits += 8
    return bits


def pow_ok(addr, nonce, bits):
    """PURE validity predicate for the token. `bits` <= 0 disarms the PoW (closed testnet)."""
    if bits <= 0:
        return True
    if not nonce:
        return False
    return leading_zero_bits(pow_digest(addr, nonce)) >= bits


def solve_pow(addr, bits, deadline_s=300.0, progress=None):
    """Solves the PoW on the CLIENT side. Returns the nonce, or "" when the time bound is reached.

    This is the client reference implementation: every faucet requester calls THIS, never a copy.
    `progress(attempts, seconds)` is invoked periodically so that a long wait does not look like a
    hang. It is time-bounded: a PoW that is too hard must produce a NAMED failure, not an infinite
    loop the requester gets lost in.
    """
    if bits <= 0:
        return ""
    t0 = time.time()
    n = 0
    while True:
        nonce = format(n, "x")
        if leading_zero_bits(pow_digest(addr, nonce)) >= bits:
            return nonce
        n += 1
        if n % 20000 == 0:
            elapsed = time.time() - t0
            if elapsed >= deadline_s:
                return ""
            if progress:
                progress(n, elapsed)


def _pow_ok(addr, pow_str):
    """Service-side verifier: applies the configured difficulty (DENDRA_FAUCET_POW_BITS)."""
    return pow_ok(addr, pow_str, POW_BITS)


def _save_state():
    """Persists the anti-abuse state (ATOMIC write). Best-effort: an error does not interrupt the service."""
    if not STATE_FILE:
        return
    now = time.time()
    with _RL_LOCK:
        data = {
            "addr_last": {a: t for a, t in _addr_last.items() if now - t < ADDR_COOLDOWN},
            "ip_hits": {ip: [t for t in ts if now - t < _DAY] for ip, ts in _ip_hits.items()},
            "global_hits": [t for t in _global_hits if now - t < _DAY],
        }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load_state():
    """Reloads the anti-abuse state at startup -> a restart does not reopen the drain."""
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    now = time.time()
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except Exception as e:
        # FAIL CLOSED (aligned with the gateway quota). A corrupted state file must not return
        # silently: empty counters would restart the faucet FROM ZERO, reopening a free re-drain.
        # Instead the global cap is marked REACHED (~24 h, fresh timestamps) with an explicit log.
        # Fix or remove STATE_FILE to reopen.
        with _RL_LOCK:
            _global_hits.extend(now for _ in range(DAILY_CAP))
        print(f"[faucet] STATE unreadable ({type(e).__name__}) -> FAIL CLOSED (global cap treated as reached). "
              f"Fix or remove {STATE_FILE}.", flush=True)
        return
    with _RL_LOCK:
        for a, t in d.get("addr_last", {}).items():
            if now - float(t) < ADDR_COOLDOWN:
                _addr_last[a] = float(t)
        for ip, ts in d.get("ip_hits", {}).items():
            kept = [float(t) for t in ts if now - float(t) < _DAY]
            if kept:
                _ip_hits[ip] = kept
        _global_hits.extend(float(t) for t in d.get("global_hits", []) if now - float(t) < _DAY)
    print("[faucet] anti-abuse state reloaded (%s, %d addresses in cooldown)" % (STATE_FILE, len(_addr_last)), flush=True)


def _rate_ok(addr, ip):
    """Authorizes the drip AND records it. Returns (ok, reason). Fail-closed when a cap is reached."""
    now = time.time()
    with _RL_LOCK:
        _global_hits[:] = [t for t in _global_hits if now - t < _DAY]
        if len(_global_hits) >= DAILY_CAP:
            return False, "global daily cap reached (anti-drain)"
        if now - _addr_last.get(addr, 0.0) < ADDR_COOLDOWN:
            return False, "address funded recently (cooldown)"
        hits = [t for t in _ip_hits.get(ip, []) if now - t < _DAY]
        if len(hits) >= IP_DAILY:
            return False, "too many requests from this IP (daily quota)"
        _addr_last[addr] = now
        hits.append(now)
        _ip_hits[ip] = hits
        _global_hits.append(now)
        return True, ""


def _run(cmd, t=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=t)


def _txhash(out):
    try:
        return json.loads(out).get("txhash")
    except Exception:
        m = re.search(r"txhash:\s*([0-9A-Fa-f]{64})", out or "")
        return m.group(1) if m else None


def _tx_code(out):
    """Result code carried by a tx answer, or None when the answer could not be read.

    proto3 OMITS a field worth its zero value, and success IS code zero: an answer without `code` is
    therefore a SUCCESS, and defaulting the absent field to a non-zero value turns every such answer
    into a failure. `None` is a separate value on purpose -- "we could not read it" must never share a
    value with "it worked" nor with "the chain refused it", because the three earn different answers.
    """
    try:
        d = json.loads(out)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        return int(d.get("code", 0))
    except (TypeError, ValueError):
        return None


def _wait_tx(h, tries=20):
    """(ok, why). Polls until the tx is readable on-chain; a refusal is FINAL, waiting cannot lift it."""
    for _ in range(tries):
        r = _run(["dendrad", "query", "tx", h, "--output", "json", *NODEF])
        if r.returncode == 0:
            c = _tx_code(r.stdout)
            if c == 0:
                return True, ""
            if c is not None:
                return False, "tx rejected by the chain (code %d)" % c
        time.sleep(1.5)
    return False, "tx not included"


def fund(addr):
    with _LOCK:
        cmd = ["dendrad", "tx", "bank", "send", FROM, addr, AMOUNT + DENOM,
               *KB, *NODEF, "--chain-id", CHAIN_ID, "-y",
               "--fees", "0" + DENOM, "--gas", "auto", "--gas-adjustment", "1.5",
               "--output", "json", "--broadcast-mode", "sync"]
        r = _run(cmd)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "tx failed").strip()[:300]
        h = _txhash(r.stdout)
        if not h:
            return False, "no txhash"
        included, why = _wait_tx(h)
        if not included:
            return False, why + " (" + h + ")"
        return True, h


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"status": "ok", "from": FROM, "amount": AMOUNT + DENOM, "pow_bits": POW_BITS})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            # BODY CEILING. `Content-Length` is supplied by the CLIENT: comparing it only against the
            # UPPER bound lets NEGATIVE values through, and `read(-1)` reads until EOF — unbounded
            # allocation despite the guard. It must be bounded on BOTH sides. (Reference form:
            # capacity_server.py.) The read happens BEFORE the rate limit, so without a ceiling a
            # single socket makes an unauthenticated endpoint swallow anything.
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid json"})
        addr = str(body.get("address", "")).strip()
        if not _RE_ADDR.match(addr):
            return self._send(400, {"error": "invalid address"})
        if not _pow_ok(addr, body.get("pow", "")):
            return self._send(400, {"error": "PoW required or invalid", "pow_bits": POW_BITS,
                                    "hint": "supply a 'pow' such that sha256(address+':'+pow) has >= %d leading zero bits" % POW_BITS})
        ip = self.client_address[0] if self.client_address else "?"
        rok, why = _rate_ok(addr, ip)
        if not rok:
            return self._send(429, {"ok": False, "error": "rate limited", "info": why, "address": addr})
        ok, info = fund(addr)
        if ok:
            _save_state()  # persist after a successful drip (atomic) -> survives restart
        self._send(200 if ok else 502, {"ok": ok, "info": info, "address": addr})

    def log_message(self, *a):
        return


if __name__ == "__main__":
    _load_state()
    # Host CONFIGURABLE (default 0.0.0.0 kept = reachable from the compose network,
    # nothing breaks) + FAIL-CLOSED under public exposure, exactly mirroring the gateway / relay:
    # DENDRA_PUBLIC=1 without anti-Sybil PoW (DENDRA_FAUCET_POW_BITS>0) = REFUSE to boot. A public faucet
    # without PoW = free Sybil drain; this knob is now ENFORCED by the code, not just documented.
    host = os.environ.get("DENDRA_FAUCET_HOST", "0.0.0.0")
    if os.environ.get("DENDRA_PUBLIC", "") == "1" and POW_BITS <= 0:
        print("[faucet] FATAL: DENDRA_PUBLIC=1 requires DENDRA_FAUCET_POW_BITS > 0 (anti-Sybil). Refusing to boot.",
              flush=True)
        raise SystemExit(2)
    print("[faucet] listening on %s:%d  from=%s  amount=%s%s  node=%s  pow_bits=%d  state=%s"
          % (host, PORT, FROM, AMOUNT, DENOM, NODE, POW_BITS, STATE_FILE or "RAM"), flush=True)
    ThreadingHTTPServer((host, PORT), H).serve_forever()
