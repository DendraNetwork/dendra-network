"""Coordinateur (le node de l'ADR-013) : decouverte + routage des jobs.

Ne voit que des **metadonnees** (jamais de contenu) : id mineur, URL, cle publique, region,
operateur, sante. Roles :
  - registre des mineurs (register / heartbeat / liste) ;
  - **selection Mode A** : 1 mineur sain, tire par VRF ;
  - **selection Mode B** : 3 mineurs **d'une meme region (faible-RTT)** et **d'operateurs
    distincts** (anti-collusion) — c'est la reponse au goulot mesure du Mode B.

⚠️ AUDIT CR-10 / PY-13 : DEMONSTRATEUR (pas le chemin de prod). La "VRF" = sha256(seed|miner_id) avec
seed CHOISI par l'appelant (PAS une vraie VRF), et region/operator sont AUTO-DECLARES par le mineur
(non verifies) -> un Sybil declare 3 operateurs + 1 region pour rafler les groupes Mode B. L'assignation
de PROD est le BEACON on-chain (committee.go). Anti-collusion reel = vraie VRF + diversite d'operateur
PROUVEE (ASN/stake), non implementee.

Les fonctions de selection sont PURES (testables sans reseau) ; un service HTTP les expose.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .ledger import Ledger


# ------------------------------ registre --------------------------------------
class MinerRegistry:
    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._miners: dict[str, dict] = {}
        self._rep: dict[str, int] = defaultdict(int)   # reputation = jobs servis

    def register(self, rec: dict) -> None:
        rec = dict(rec)
        rec["last_seen"] = time.time()
        self._miners[rec["miner_id"]] = rec

    def heartbeat(self, miner_id: str) -> bool:
        m = self._miners.get(miner_id)
        if not m:
            return False
        m["last_seen"] = time.time()
        return True

    def bump(self, miner_id: str) -> None:
        self._rep[miner_id] += 1

    def healthy(self) -> list[dict]:
        now = time.time()
        out = []
        for m in self._miners.values():
            m = dict(m)
            m["healthy"] = (now - m["last_seen"]) <= self.ttl
            m["jobs_served"] = self._rep.get(m["miner_id"], 0)
            if m["healthy"]:
                out.append(m)
        return out

    def all(self) -> list[dict]:
        return list(self._miners.values())


# ------------------------------ selection (pure) ------------------------------
def _vrf(seed: str, key: str) -> int:
    return int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(), 16)


def select_mode_a(miners: list[dict], seed: str) -> dict | None:
    elig = [m for m in miners if m.get("healthy", True)]
    if not elig:
        return None
    return min(elig, key=lambda m: _vrf(seed, m["miner_id"]))


def select_mode_b(miners: list[dict], seed: str, group: int = 3) -> list[dict] | None:
    """3 mineurs : meme region (faible-RTT) + operateurs DISTINCTS (anti-collusion)."""
    elig = [m for m in miners if m.get("healthy", True)]
    by_region: dict[str, list[dict]] = defaultdict(list)
    for m in elig:
        by_region[m["region"]].append(m)

    viable: dict[str, list[dict]] = {}
    for region, ms in by_region.items():
        best_per_op: dict[str, dict] = {}
        for m in sorted(ms, key=lambda m: _vrf(seed, m["miner_id"])):
            best_per_op.setdefault(m["operator"], m)  # un seul (meilleur VRF) par operateur
        if len(best_per_op) >= group:
            viable[region] = list(best_per_op.values())

    if not viable:
        return None
    region = min(viable, key=lambda r: _vrf(seed, "region:" + r))
    return sorted(viable[region], key=lambda m: _vrf(seed, m["miner_id"]))[:group]


def _pub(m: dict) -> dict:
    """Vue publique d'un mineur (metadonnees uniquement)."""
    return {k: m[k] for k in ("miner_id", "url", "pubkey_hex", "region", "operator",
                              "jobs_served") if k in m}


# ------------------------------ service HTTP ----------------------------------
class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        reg: MinerRegistry = self.server.registry  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n).decode()) if n else {}
        if self.path == "/register":
            reg.register(data)
            self._send(200, {"ok": True})
        elif self.path == "/heartbeat":
            self._send(200, {"ok": reg.heartbeat(data.get("miner_id", ""))})
        elif self.path == "/settle":
            led: Ledger = self.server.ledger  # type: ignore[attr-defined]
            led.record(data["job_id"], data["miner_id"], data.get("client_commit", ""),
                       data.get("result_commit", ""), data.get("canary_commit", ""))
            if self.server.ledger_path:  # type: ignore[attr-defined]
                led.save(self.server.ledger_path)  # type: ignore[attr-defined]
            reg.bump(data["miner_id"])
            self._send(200, {"ok": True, "jobs_served": reg._rep[data["miner_id"]]})
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        reg: MinerRegistry = self.server.registry  # type: ignore[attr-defined]
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/miners":
            self._send(200, {"miners": [_pub(m) for m in reg.healthy()]})
        elif u.path == "/ledger":
            self._send(200, {"ledger": self.server.ledger.dump_public()})  # type: ignore[attr-defined]
        elif u.path == "/assign":
            seed = q.get("seed", [str(time.time())])[0]
            mode = q.get("mode", ["A"])[0].upper()
            miners = reg.healthy()
            if mode == "A":
                m = select_mode_a(miners, seed)
                self._send(200 if m else 503,
                           {"miner": _pub(m)} if m else {"error": "aucun mineur dispo"})
            else:
                g = select_mode_b(miners, seed)
                self._send(200 if g else 503,
                           {"group": [_pub(x) for x in g]} if g
                           else {"error": "pas de groupe Mode B (3 operateurs/region faible-RTT requis)"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def make_coordinator(host="127.0.0.1", port=0, ledger_path=None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.registry = MinerRegistry()  # type: ignore[attr-defined]
    srv.ledger = Ledger.load(ledger_path) if ledger_path else Ledger()  # type: ignore[attr-defined]
    srv.ledger_path = ledger_path  # type: ignore[attr-defined]
    return srv
