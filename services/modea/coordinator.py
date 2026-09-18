"""Coordinator (the node of ADR-013): job discovery and routing.

It only ever sees METADATA, never content: miner id, URL, public key, region, operator, health.
Responsibilities:
  - miner registry (register / heartbeat / list);
  - Mode A selection: one healthy miner, drawn by VRF;
  - Mode B selection: three miners from the SAME region (low RTT) and from DISTINCT operators
    (anti-collusion), which answers the measured Mode B bottleneck.

DEMONSTRATOR, not the production path. The "VRF" here is sha256(seed|miner_id) with a seed CHOSEN by
the caller, which is not a VRF at all, and region/operator are SELF-DECLARED by the miner and never
verified: a Sybil declares three operators and one region to capture every Mode B group. Production
assignment is the on-chain BEACON (committee.go). Real anti-collusion needs a true VRF plus PROVEN
operator diversity (ASN or stake), which is not implemented here.

The selection functions are PURE, so they are testable without a network; an HTTP service exposes them.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .ledger import Ledger


# ------------------------------ registry --------------------------------------
class MinerRegistry:
    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._miners: dict[str, dict] = {}
        self._rep: dict[str, int] = defaultdict(int)   # reputation = jobs served

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
    """Three miners: same region (low RTT) and DISTINCT operators (anti-collusion)."""
    elig = [m for m in miners if m.get("healthy", True)]
    by_region: dict[str, list[dict]] = defaultdict(list)
    for m in elig:
        by_region[m["region"]].append(m)

    viable: dict[str, list[dict]] = {}
    for region, ms in by_region.items():
        best_per_op: dict[str, dict] = {}
        for m in sorted(ms, key=lambda m: _vrf(seed, m["miner_id"])):
            best_per_op.setdefault(m["operator"], m)  # a single miner (best VRF) per operator
        if len(best_per_op) >= group:
            viable[region] = list(best_per_op.values())

    if not viable:
        return None
    region = min(viable, key=lambda r: _vrf(seed, "region:" + r))
    return sorted(viable[region], key=lambda m: _vrf(seed, m["miner_id"]))[:group]


def _pub(m: dict) -> dict:
    """Public view of a miner (metadata only)."""
    return {k: m[k] for k in ("miner_id", "url", "pubkey_hex", "region", "operator",
                              "jobs_served") if k in m}


# ------------------------------ HTTP service ----------------------------------
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
                           {"miner": _pub(m)} if m else {"error": "no miner available"})
            else:
                g = select_mode_b(miners, seed)
                self._send(200 if g else 503,
                           {"group": [_pub(x) for x in g]} if g
                           else {"error": "no Mode B group (3 distinct operators in one low-RTT region required)"})
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
