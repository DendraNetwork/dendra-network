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
MAX_BODY = 8192          # une demande de faucet = une adresse + un PoW ; 8 KiB est deja large
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


def _pow_ok(addr, pow_str):
    """Optional anti-Sybil: PoW tied to the address. If POW_BITS=0, always OK (off)."""
    if POW_BITS <= 0:
        return True
    if not pow_str:
        return False
    h = hashlib.sha256((addr + ":" + str(pow_str)).encode()).digest()
    bits = 0
    for byte in h:
        if byte == 0:
            bits += 8
        else:
            bits += 8 - byte.bit_length()
            break
    return bits >= POW_BITS


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
        # FAIL-CLOSED (aligned with the gateway quota). Before: corrupted state ->
        # silent return -> empty counters -> the faucet RESTARTED FROM ZERO (free re-drain).
        # Now: global cap marked REACHED (~24 h, fresh timestamps) + explicit log.
        # Fix/remove STATE_FILE to reopen.
        with _RL_LOCK:
            _global_hits.extend(now for _ in range(DAILY_CAP))
        print(f"[faucet] STATE illisible ({type(e).__name__}) -> FAIL-CLOSED (plafond global atteint). "
              f"Corriger/supprimer {STATE_FILE}.", flush=True)
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
    print("[faucet] etat anti-abus recharge (%s, %d adresses en cooldown)" % (STATE_FILE, len(_addr_last)), flush=True)


def _rate_ok(addr, ip):
    """Authorizes the drip AND records it. Returns (ok, reason). Fail-closed when a cap is reached."""
    now = time.time()
    with _RL_LOCK:
        _global_hits[:] = [t for t in _global_hits if now - t < _DAY]
        if len(_global_hits) >= DAILY_CAP:
            return False, "plafond global quotidien atteint (anti-drain)"
        if now - _addr_last.get(addr, 0.0) < ADDR_COOLDOWN:
            return False, "adresse deja financee recemment (cooldown)"
        hits = [t for t in _ip_hits.get(ip, []) if now - t < _DAY]
        if len(hits) >= IP_DAILY:
            return False, "trop de demandes depuis cette IP (quota/jour)"
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


def _wait_tx(h, tries=20):
    for _ in range(tries):
        r = _run(["dendrad", "query", "tx", h, "--output", "json", *NODEF])
        if r.returncode == 0:
            try:
                if int(json.loads(r.stdout).get("code", 1)) == 0:
                    return True
            except Exception:
                pass
        time.sleep(1.5)
    return False


def fund(addr):
    with _LOCK:
        cmd = ["dendrad", "tx", "bank", "send", FROM, addr, AMOUNT + DENOM,
               *KB, *NODEF, "--chain-id", CHAIN_ID, "-y",
               "--fees", "0" + DENOM, "--gas", "auto", "--gas-adjustment", "1.5",
               "--output", "json", "--broadcast-mode", "sync"]
        r = _run(cmd)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "echec tx").strip()[:300]
        h = _txhash(r.stdout)
        if not h:
            return False, "pas de txhash"
        if not _wait_tx(h):
            return False, "tx non incluse (" + h + ")"
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
            # PLAFOND DE CORPS. `Content-Length` est fourni par le CLIENT : le comparer au seul plafond HAUT
            # laisse passer les valeurs NEGATIVES, et `read(-1)` lit jusqu'a EOF — donc allocation illimitee
            # malgre la garde. Il faut borner des DEUX cotes. (Forme correcte de reference : capacity_server.py.)
            # Ce service etait le seul SANS aucun plafond : la lecture avait lieu AVANT le rate-limit,
            # donc un seul socket suffisait a faire avaler ce qu'on voulait a un endpoint non authentifie.
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "corps trop volumineux"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "json invalide"})
        addr = str(body.get("address", "")).strip()
        if not _RE_ADDR.match(addr):
            return self._send(400, {"error": "adresse invalide"})
        if not _pow_ok(addr, body.get("pow", "")):
            return self._send(400, {"error": "PoW requis/invalide", "pow_bits": POW_BITS,
                                    "hint": "fournis 'pow' tel que sha256(address+':'+pow) ait >= %d bits de zeros de tete" % POW_BITS})
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
        print("[faucet] FATAL: DENDRA_PUBLIC=1 exige DENDRA_FAUCET_POW_BITS > 0 (anti-Sybil). Refus de boot.",
              flush=True)
        raise SystemExit(2)
    print("[faucet] ecoute %s:%d  from=%s  montant=%s%s  node=%s  pow_bits=%d  state=%s"
          % (host, PORT, FROM, AMOUNT, DENOM, NODE, POW_BITS, STATE_FILE or "RAM"), flush=True)
    ThreadingHTTPServer((host, PORT), H).serve_forever()
