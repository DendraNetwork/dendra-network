#!/usr/bin/env python3
"""UNTRUSTED network relay (L4): carries ENCRYPTED messages and PUBLIC keys over HTTP
between the client and the miners. It NEVER sees cleartext (only ciphertext + pubs).

Endpoints (JSON body):
  POST /pub/<mid>            {"pub": hex}                 -- a miner publishes its public key
  GET  /pub/<mid>            -> {"pub": hex}              -- the client fetches it
  POST /req/<jid>__<mid>     {"client_eph_pk","nonce","ct"} (hex) -- sealed request client->miner
  GET  /req/<jid>__<mid>     -> same
  POST /res/<jid>__<mid>     {"nonce","ct"} (hex)          -- sealed response miner->client
  GET  /res/<jid>__<mid>     -> same
  GET  /list                 -> {kind: [keys...]}          -- debug (keys only; GUARDED by token)

In-memory storage. Usage: python3 relay.py [port]   (default 8645, on 127.0.0.1).

Confidentiality: END-TO-END guaranteed by Mode A. The relay is assumed hostile and only sees
ciphertext. BUT the miner pub is no longer the root of trust: the client encrypts to the ON-CHAIN
ANCHORED X25519 pub (signed by the miner's Cosmos key), so a pub substitution at the relay is
ineffective (the relay's /pub is now only a cache/transport).

Hardening:
  * Global LOCK -> ThreadingHTTPServer + shared state without data race (safe /list iteration).
  * Store BOUNDED per type (MAX_ENTRIES) -> no memory leak (FIFO eviction).
  * Body size cap (MAX_BODY) -> no unbounded allocation (local anti-DoS).
  * AUTH via shared token (DENDRA_RELAY_TOKEN) -> require the X-Dendra-Token header on ALL
    requests once a token is configured (local default = empty = no auth, backward-compatible).
  * RATE-LIMIT per IP (sliding window) -> a spammer can no longer evict (FIFO) legitimate
    entries nor saturate the relay.
  * /list (metadata: who processes what) GUARDED behind the token (otherwise network mapping).
  Multi-machine: DENDRA_RELAY_TOKEN=<secret> on all nodes + WireGuard/SSH tunnel for transport
  (the token authenticates; WireGuard encrypts/authenticates the channel). See the multi-machine
  deployment guide.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 1 << 20        # 1 MiB per request (well above a sealed prompt/response)
MAX_ENTRIES = 4000        # per type (pub/req/res) -> evict the oldest beyond this
TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")          # shared auth (empty = OFF, local)
RATE_MAX = int(os.environ.get("DENDRA_RELAY_RATE", "120"))  # max req / window / IP
RATE_WINDOW = float(os.environ.get("DENDRA_RELAY_WINDOW", "10"))  # seconds
# ATTESTATION gate for confidential jobs. When DENDRA_ATTEST_REQUIRE=1, the relay
# REFUSES a req/<jid>__<mid> until the miner <mid> has deposited a valid SIGNED attestation
# (POST /attest/<mid>) whose measured hash is in the allow-list DENDRA_ATTEST_ALLOW (comma-
# separated sha256 = recognized binaries/configs). OFF by default -> fully backward-compatible.
# HONEST: deterrence (the miner must sign a recognized measured client), NOT a proof of execution.
ATTEST_REQUIRE = os.environ.get("DENDRA_ATTEST_REQUIRE", "0") == "1"
ATTEST_ALLOW = {h.strip() for h in os.environ.get("DENDRA_ATTEST_ALLOW", "").split(",") if h.strip()}
LOCK = threading.Lock()
STORE = {"pub": OrderedDict(), "req": OrderedDict(), "res": OrderedDict(),
         "reveal": OrderedDict(),    # SEALED reveal primary->committee on an audited job
         "attest": OrderedDict()}    # signed software attestation of the miner (key = <mid>)
RATE = {}                 # ip -> deque[timestamps] (bounded by window)
RATE_LOCK = threading.Lock()


def _attest_ok(mid: str) -> bool:
    """Is a miner attested to receive a confidential job? Verifies the signature of the deposited
    attestation + the membership of the measured hash in the allow-list. Best-effort: if the
    `cryptography`/`modea` lib is not importable on the relay side, we DO NOT ALLOW (fail-closed when
    the gate is active). Without a configured allow-list, the gate cannot tell a good client ->
    we also refuse (inconsistent config = we close)."""
    if not ATTEST_ALLOW:
        return False
    raw = _get("attest", mid)
    if not raw:
        return False
    try:
        att = json.loads(raw)
    except Exception:
        return False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from modea import confine
    except Exception:
        return False
    ok, _ = confine.verify_attestation(att, allowed_hashes=ATTEST_ALLOW,
                                       expected_pubkey=att.get("attest_pubkey"))
    # NB: expected_pubkey=att[...] does NOT bind to an on-chain identity here (the relay has no
    # registry); that strong binding happens chain-side. The relay validates signature + allow-list.
    return ok and att.get("miner_id") == mid


def _put(kind: str, key: str, data: bytes) -> None:
    with LOCK:
        d = STORE[kind]
        if key in d:
            d.move_to_end(key)
        d[key] = data
        while len(d) > MAX_ENTRIES:
            d.popitem(last=False)   # evict the oldest (FIFO) -> bounds memory


def _get(kind: str, key: str):
    with LOCK:
        return STORE[kind].get(key)


def _snapshot_keys():
    with LOCK:
        return {k: list(v.keys()) for k, v in STORE.items()}   # copy under lock -> safe iteration


def _rate_ok(ip: str) -> bool:
    """Sliding window per IP. True if the request is allowed."""
    now = time.monotonic()
    with RATE_LOCK:
        dq = RATE.get(ip)
        if dq is None:
            dq = deque()
            RATE[ip] = dq
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_MAX:
            return False
        dq.append(now)
        # opportunistic purge of inactive IPs (bounds the counter's memory)
        if len(RATE) > 8192:
            for k in [k for k, v in RATE.items() if not v or now - v[-1] > RATE_WINDOW][:4096]:
                RATE.pop(k, None)
        return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self):
        return [p for p in self.path.strip("/").split("/") if p != ""]

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _authed(self) -> bool:
        if not TOKEN:
            return True   # no token configured -> local mode, no auth (backward-compatible)
        # CONSTANT-TIME comparison (== leaks the length of the correct prefix)
        return hmac.compare_digest(self.headers.get("X-Dendra-Token", ""), TOKEN)

    def _guard(self) -> bool:
        """Rate-limit + auth. Returns False (and has already responded) if rejected."""
        if not _rate_ok(self._client_ip()):
            self._send(429, b'{"error":"rate limited"}')
            return False
        if not self._authed():
            self._send(401, b'{"error":"unauthorized (X-Dendra-Token)"}')
            return False
        return True

    def do_POST(self):
        if not self._guard():
            return
        parts = self._route()
        n = int(self.headers.get("Content-Length", 0) or 0)
        # PLAFOND DE CORPS. `Content-Length` est fourni par le CLIENT : le comparer au seul plafond HAUT
        # laisse passer les valeurs NEGATIVES, et `read(-1)` lit jusqu'a EOF — donc allocation illimitee
        # malgre la garde. Il faut borner des DEUX cotes. (Forme correcte de reference : capacity_server.py.)
        if n < 0 or n > MAX_BODY:
            self._send(413, b'{"error":"body too large"}')
            return
        data = self.rfile.read(n) if n else b""
        if len(parts) == 2 and parts[0] in STORE:
            # attestation gate on the DEPOSIT of a confidential job (req/<jid>__<mid>).
            if ATTEST_REQUIRE and parts[0] == "req":
                mid = parts[1].split("__", 1)[1] if "__" in parts[1] else ""
                if not (mid and _attest_ok(mid)):
                    self._send(403, b'{"error":"miner not attested (DENDRA_ATTEST_REQUIRE)"}')
                    return
            _put(parts[0], parts[1], data)
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"route"}')

    def do_GET(self):
        if not self._guard():
            return
        parts = self._route()
        if parts == ["list"]:
            # /list exposes metadata (who processes what). If a token is configured, it IS
            # required by _guard; in local mode without a token, /list stays open (debug).
            self._send(200, json.dumps(_snapshot_keys()).encode())
            return
        if len(parts) == 2 and parts[0] in STORE:
            d = _get(parts[0], parts[1])
            self._send(404, b'{"error":"not found"}') if d is None else self._send(200, d)
        else:
            self._send(404, b'{"error":"route"}')

    def log_message(self, *a):
        pass  # silent


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8645
    # Multi-machine: DENDRA_RELAY_HOST=0.0.0.0 to accept REMOTE miners (default 127.0.0.1).
    # BEFORE exposing on 0.0.0.0: set DENDRA_RELAY_TOKEN (auth) AND a WireGuard/SSH tunnel (transport).
    host = os.environ.get("DENDRA_RELAY_HOST", "127.0.0.1")
    auth = "ON" if TOKEN else "OFF(local)"
    # FAIL-CLOSED mirroring the gateway: under EXPLICIT PUBLIC exposure
    # (DENDRA_PUBLIC=1, the same signal as the gateway), a relay without a token = open /list (network
    # mapping) + unauthenticated PUT/GET -> REFUSE TO BOOT. A 0.0.0.0 bind WITHOUT PUBLIC (closed compose,
    # private docker network) stays a warning: we don't break the closed stack, as with the gateway.
    _public = os.environ.get("DENDRA_PUBLIC", "0").strip().lower() in ("1", "true", "yes")
    if _public:
        if not TOKEN:
            print("[relay] REFUSED: DENDRA_PUBLIC=1 without DENDRA_RELAY_TOKEN (auth required in public).",
                  file=sys.stderr)
            sys.exit(2)
        if len(TOKEN) < 16:
            print("[relay] REFUSED: DENDRA_PUBLIC=1 with a DENDRA_RELAY_TOKEN that is too short (<16 chars). "
                  "Generate `openssl rand -hex 24`.", file=sys.stderr)
            sys.exit(2)
    elif host != "127.0.0.1" and not TOKEN:
        print("[relay] WARNING: exposed on non-localhost WITHOUT DENDRA_RELAY_TOKEN -> auth OFF. "
              "Set a token + WireGuard before a real deployment.", file=sys.stderr)
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"[relay] listening on http://{host}:{port}  auth={auth}  rate={RATE_MAX}/{RATE_WINDOW:g}s/IP  (encrypted transport)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
