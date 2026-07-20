#!/usr/bin/env python3
"""the_proof.py -- "The Proof": READ-ONLY HTTP facade over the real on-chain state,
so the public site can show a NON-staged feed (audited jobs, slashes, VRF health).

NO writes, NO secrets, NO content (the chain already only sees metadata/hashes -- this facade
only exposes counters + public states already queryable by anyone via the RPC).

  GET /proof   -> JSON (see build_proof); refreshed at most every DENDRA_PROOF_REFRESH_S (default 15 s).
  GET /health  -> {"status":"ok"}

Run: python3 the_proof.py   (binds 127.0.0.1:8090 by default -- put behind a reverse proxy for
public exposure; DENDRA_PROOF_HOST/PORT/CORS to adjust). Offline selftest: python3 the_proof.py --selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# PURE core (selftest without chain or network): builds the payload from already-fetched snapshots.
# State markers = on-chain predicates (job_state.go): paid/settled, disputed, resolved, clawed.
# ---------------------------------------------------------------------------


def build_proof(jobs, pools, seed_health, height, max_recent=10):
    """The Proof payload (PURE). `jobs` = list_jobs_full() (id/state/miner_id/fee/slashes)."""
    total = len(jobs or [])
    settled = audited = vindicated = clawed = pending = 0
    slash_events = []
    recent_audits = []
    for j in jobs or []:
        st = j.get("state", "") or ""
        if ("paid" in st) or ("settled" in st):
            settled += 1
        if "disputed" in st:
            audited += 1
            if "resolved" in st:
                if "clawed" in st:
                    clawed += 1
                else:
                    vindicated += 1
            else:
                pending += 1
            recent_audits.append({"job_id": j.get("id", ""), "state": st, "fee": int(j.get("fee", 0) or 0)})
        for s in j.get("slashes", []) or []:
            if int(s.get("amount", 0) or 0) > 0:
                slash_events.append({"job_id": j.get("id", ""), "miner_id": s.get("miner_id", ""),
                                     "amount": int(s.get("amount", 0))})
    slashed_total_u = sum(e["amount"] for e in slash_events)
    return {
        "generated_at": int(time.time()),
        "height": int(height or 0),
        "jobs": {"total": total, "settled": settled, "audited": audited,
                 "vindicated": vindicated, "clawed": clawed, "audit_pending": pending},
        "slashes": {"events": len(slash_events), "total_udndr": slashed_total_u,
                    "recent": slash_events[-max_recent:]},
        "recent_audits": recent_audits[-max_recent:],
        "vrf": seed_health or {},
        "pools": pools or {},
        "_provenance": {
            "claim": "Etat on-chain REEL (RPC public), lecture seule — rien n'est mis en scene.",
            "note": "audited=jobs tires par l'audit VRF ; clawed=paiement repris ; vindicated=confirme honnete. "
                    "Statut devnet/recherche.",
        },
    }


def _selftest():
    jobs = [
        {"id": "j1", "state": "open+paid+optimistic", "fee": 100, "slashes": []},
        {"id": "j2", "state": "open+paid+optimistic+disputed", "fee": 100, "slashes": []},
        {"id": "j3", "state": "open+paid+optimistic+disputed+resolved", "fee": 100, "slashes": []},
        {"id": "j4", "state": "open+paid+optimistic+disputed+resolved+clawed", "fee": 100,
         "slashes": [{"miner_id": "tricheur", "amount": 320000}]},
        {"id": "j5", "state": "open", "fee": 50, "slashes": []},
    ]
    p = build_proof(jobs, {"treasury": 7}, {"source": 1, "contributors": 2}, height=1234, max_recent=2)
    ok = True

    def chk(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    chk("total=5 settled=4", p["jobs"]["total"] == 5 and p["jobs"]["settled"] == 4)
    chk("audited=3 vindicated=1 clawed=1 pending=1",
        p["jobs"]["audited"] == 3 and p["jobs"]["vindicated"] == 1
        and p["jobs"]["clawed"] == 1 and p["jobs"]["audit_pending"] == 1)
    chk("slash events=1 total=320000", p["slashes"]["events"] == 1 and p["slashes"]["total_udndr"] == 320000)
    chk("recent_audits borne a 2", len(p["recent_audits"]) == 2)
    chk("height + vrf + pools presents", p["height"] == 1234 and p["vrf"]["contributors"] == 2 and p["pools"]["treasury"] == 7)
    chk("payload JSON-serialisable", bool(json.dumps(p)))
    print("SELFTEST THE-PROOF", "VERT" if ok else "ROUGE")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # BEFORE the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client as dc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DENDRA_PROOF_HOST", "127.0.0.1")  # public = reverse proxy in front (no bare 0.0.0.0 bind)
PORT = int(os.environ.get("DENDRA_PROOF_PORT", "8090"))
CORS = os.environ.get("DENDRA_PROOF_CORS", "")           # e.g. https://dendranetwork.com (empty = no header)
REFRESH_S = float(os.environ.get("DENDRA_PROOF_REFRESH_S", "15"))
_CACHE = {"t": 0.0, "payload": b"{}"}


def _refresh():
    if time.time() - _CACHE["t"] < REFRESH_S:
        return
    try:
        jobs = dc.list_jobs_full()
        pools = dc.pools()
        seed = dc.committee_seed_health()
        height = dc.height()
        _CACHE["payload"] = json.dumps(build_proof(jobs, pools, seed, height), ensure_ascii=False).encode()
        _CACHE["t"] = time.time()
    except Exception as e:  # the facade never breaks: it serves the last snapshot
        sys.stderr.write(f"[the-proof] refresh: {type(e).__name__}: {e}\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if CORS:
            self.send_header("Access-Control-Allow-Origin", CORS)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/proof", "/"):
            _refresh()
            self._send(200, _CACHE["payload"])
        elif self.path == "/health":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    print(f"[the-proof] facade lecture-seule sur http://{HOST}:{PORT}/proof (refresh {REFRESH_S:g}s ; CORS {CORS or 'off'})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
