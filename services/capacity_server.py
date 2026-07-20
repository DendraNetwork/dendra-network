#!/usr/bin/env python3
"""capacity_server.py — network capacity registry (what hardware and which models the network runs).

  POST /capacity   <- a node posts its deploy/hw_probe.sh report (JSON)
  GET  /capacity   -> aggregate + per-node list (feeds the /network page and Grafana)
  GET  /metrics    -> Prometheus exposition of the same aggregate
  GET  /health

HONESTY — read this before showing any number to a user:
  These reports are SELF-DECLARED by operators. NOTHING here is cryptographically proven: a node can
  claim any GPU it likes. We cross-check the node_id against the ON-CHAIN miner registry and expose
  `registered_onchain` so the reader can tell a staked identity from an anonymous claim, but the
  HARDWARE ITSELF IS NOT VERIFIABLE. Never present this as "the network's proven power" — it is an
  inventory, not a proof. (Contrast: The Proof feed IS on-chain state.)

PERSISTENCE: on DISK, written atomically. An in-memory registry loses everything on restart, and a
component that silently forgets its state puts every peer depending on it at risk — so this one does
not forget.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("DENDRA_CAPACITY_HOST", "0.0.0.0")
PORT = int(os.environ.get("DENDRA_CAPACITY_PORT", "8092"))
DB = Path(os.environ.get("DENDRA_CAPACITY_DB", "/data/capacity.json"))
CORS = os.environ.get("DENDRA_CAPACITY_CORS", "")
STALE_S = int(os.environ.get("DENDRA_CAPACITY_STALE", str(24 * 3600)))    # not counted as live beyond this
PURGE_S = int(os.environ.get("DENDRA_CAPACITY_PURGE", str(7 * 24 * 3600)))  # dropped from the registry
import re as _re
_RE_MODEL = _re.compile(r"[^A-Za-z0-9._:+-]")
MAX_BODY = 8192
RATE_N, RATE_W = 30, 60.0          # max POSTs per IP per window
NODE_CACHE_S = 60.0                # on-chain miner list refresh

_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}
_MINERS = {"t": 0.0, "ids": set()}


def _load() -> dict:
    try:
        return json.loads(DB.read_text("utf-8"))
    except Exception:
        return {}


def _save(store: dict) -> None:
    """Atomic write: temp file + replace, so a crash mid-write cannot truncate the registry."""
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        tmp = DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(store), "utf-8")
        os.replace(tmp, DB)
    except Exception as e:
        print(f"[capacity] WARN persist failed: {type(e).__name__}", flush=True)


def _onchain_miner_ids() -> set:
    """Miner ids actually registered on-chain — lets the UI separate a staked identity from a claim."""
    if time.time() - _MINERS["t"] < NODE_CACHE_S:
        return _MINERS["ids"]
    ids, ok = set(), False
    try:
        node = os.environ.get("DENDRA_NODE", "")
        cmd = ["dendrad", "query", "jobs", "list-miner", "--output", "json"]
        if node:
            cmd += ["--node", node]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
        for m in json.loads(out).get("miner", []):
            if m.get("miner_id"):
                ids.add(m["miner_id"])
        ok = True
    except Exception:
        ok = False
    if not ok:
        # Chain unreachable -> KEEP the last known set and do NOT refresh the timestamp (retry at once).
        # Overwriting it with an empty set would flip every node to "unregistered" on a public page for
        # the duration of an RPC hiccup: a false statement produced by our own outage. An EMPTY-but-
        # successful answer is different and IS recorded (a fresh genesis legitimately has no miners).
        return _MINERS["ids"]
    _MINERS["t"], _MINERS["ids"] = time.time(), ids
    return ids


def _prom_label(v):
    """Echappe une valeur de LABEL Prometheus (format d'exposition texte).

    Sans cela, une valeur controlee par le client qui contient un guillemet et un saut de ligne
    FERME le label puis OUVRE une nouvelle ligne : l'appelant ecrit alors la metrique qu'il veut
    (`dendra_capacity_gpus 999999`), et tout ce qui lit ces metriques — alerting, page publique —
    affiche un chiffre choisi par un anonyme. L'endpoint /capacity n'est pas authentifie.

    On echappe ici, au moment d'ECRIRE, plutot qu'a l'entree seule : c'est le seul endroit qui voit
    la totalite des labels, donc le seul ou l'oubli d'un champ futur est impossible.
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")

def _clean(rep: dict) -> dict | None:
    """Keep only known fields, bounded — an open endpoint must never store arbitrary operator input."""
    def s(k, n=64):
        v = rep.get(k)
        return str(v)[:n] if v is not None else ""

    def i(k, hi):
        try:
            return max(0, min(int(rep.get(k) or 0), hi))
        except Exception:
            return 0

    node_id = s("node_id")
    if not node_id:
        return None
    # miner_ids: the ON-CHAIN identities this box actually runs. Needed because node_id is a PRIVACY
    # PSEUDONYM (hashed machine id) and can never match a registry entry on its own — without this the
    # `registered_onchain` flag read "unregistered" for every honest node, which is worse than useless:
    # it is a false statement on a public page.
    raw = rep.get("miner_ids")
    miner_ids = [str(m)[:32] for m in raw[:16] if m] if isinstance(raw, list) else []
    return {
        "node_id": node_id, "miner_ids": miner_ids,
        "machine": s("machine", 64), "backend": s("backend", 8),
        "gpu": s("gpu", 80), "gpu_count": i("gpu_count", 64),
        "vram_mb": i("vram_mb", 2_000_000), "ram_mb": i("ram_mb", 8_000_000),
        "cpu_cores": i("cpu_cores", 1024), "tier": i("tier", 9),
        # Jeu de caracteres restreint DES L'ENTREE (ceinture ; l'echappement a l'emission est la bretelle).
        # Un identifiant de modele reel ressemble a "llama3.1:8b-instruct-q4_K_M" : rien d'autre n'a
        # de raison d'entrer, et ce qui n'entre pas ne peut pas ressortir dans une metrique.
        "model": _RE_MODEL.sub("", s("model", 80)), "can_judge": bool(rep.get("can_judge")),
        "ts": int(time.time()),
    }


def aggregate(store: dict) -> dict:
    now = time.time()
    onchain = _onchain_miner_ids()
    nodes, machines, models, tiers = [], set(), {}, {}
    vram = ram = cores = gpus = 0
    judges = live = 0
    # TOTAUX RESTREINTS AUX IDENTITÉS ON-CHAIN.
    # L'endpoint est public et non authentifié : n'importe qui peut POSTer `gpu_count: 5000`. Les
    # bornes par champ empêchent les valeurs absurdes, mais RIEN n'empêche d'additionner des nœuds
    # qui n'existent pas — donc le total déclaré est, par construction, choisi par l'anonyme le plus
    # motivé. On calcule en parallèle un total ne comptant QUE les nœuds dont une identité figure au
    # registre de mineurs on-chain : gonfler celui-là exige d'abord de staker. Le total déclaré reste
    # exposé (on ne cache rien), mais ce n'est plus lui le chiffre à mettre en avant.
    v_vram = v_ram = v_cores = v_gpus = 0
    v_judges = v_live = 0
    v_machines = set()
    for rep in store.values():
        stale = (now - rep.get("ts", 0)) > STALE_S
        row = dict(rep)
        row["stale"] = stale
        # Declared identities that REALLY exist in the on-chain miner registry. Still says nothing about
        # the hardware — only that the operator runs staked identities rather than an anonymous claim.
        matched = sorted(set(rep.get("miner_ids") or []) & onchain)
        row["onchain_miners"] = matched
        row["registered_onchain"] = bool(matched) or (rep.get("node_id") in onchain)
        nodes.append(row)
        if stale:
            continue
        live += 1
        machines.add(rep.get("machine") or rep.get("node_id"))
        models[rep.get("model", "?")] = models.get(rep.get("model", "?"), 0) + 1
        tiers[str(rep.get("tier", 0))] = tiers.get(str(rep.get("tier", 0)), 0) + 1
        vram += rep.get("vram_mb", 0)
        ram += rep.get("ram_mb", 0)
        cores += rep.get("cpu_cores", 0)
        gpus += rep.get("gpu_count", 0)
        judges += 1 if rep.get("can_judge") else 0
        if row["registered_onchain"]:
            v_live += 1
            v_machines.add(rep.get("machine") or rep.get("node_id"))
            v_vram += rep.get("vram_mb", 0)
            v_ram += rep.get("ram_mb", 0)
            v_cores += rep.get("cpu_cores", 0)
            v_gpus += rep.get("gpu_count", 0)
            v_judges += 1 if rep.get("can_judge") else 0
    nodes.sort(key=lambda r: (-r.get("tier", 0), r.get("node_id", "")))
    return {
        "generated_at": int(now),
        "live_nodes": live, "known_nodes": len(nodes), "machines": len(machines),
        "gpus": gpus, "vram_total_mb": vram, "ram_total_mb": ram, "cpu_cores_total": cores,
        "judge_capable": judges, "distinct_models": len(models),
        "models": models, "tiers": tiers, "nodes": nodes,
        # Sous-total STAKÉ : mêmes grandeurs, restreintes aux nœuds dont une identité existe au
        # registre on-chain. C'est ce bloc que les surfaces publiques doivent afficher.
        "verified": {
            "live_nodes": v_live, "machines": len(v_machines), "gpus": v_gpus,
            "vram_total_mb": v_vram, "ram_total_mb": v_ram, "cpu_cores_total": v_cores,
            "judge_capable": v_judges,
        },
        "_provenance": {
            "declarative": True,
            "claim": "Operator-declared hardware inventory. NOT proven on-chain: a node can claim any "
                     "GPU. `registered_onchain` only tells you the id exists in the miner registry.",
            "verified_subset": "The `verified` block sums ONLY nodes whose declared identity exists in "
                               "the on-chain miner registry. Inflating it requires staking first, so it "
                               "is the figure public surfaces should display. The top-level totals are "
                               "unauthenticated declarations and are kept for transparency, not display.",
            "proven_elsewhere": "On-chain truth (jobs, slashes, VRF) is served by The Proof feed.",
        },
    }


def _rate_ok(ip: str) -> bool:
    """Rate limit per IP. Guarded by the lock and SELF-PURGING: this map is fed by a public unauthenticated
    endpoint, so an unbounded dict keyed by remote IP is a slow memory leak anyone can drive."""
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > RATE_W]:
            if k != ip:
                _RATE.pop(k, None)
        q = _RATE.setdefault(ip, [])
        while q and now - q[0] > RATE_W:
            q.pop(0)
        if len(q) >= RATE_N:
            return False
        q.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if CORS:
            self.send_header("Access-Control-Allow-Origin", CORS)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/capacity", "/"):
            with _LOCK:
                agg = aggregate(_load())
            self._send(200, json.dumps(agg).encode())
        elif path == "/metrics":
            with _LOCK:
                a = aggregate(_load())
            lines = [
                f'dendra_capacity_live_nodes {a["live_nodes"]}',
                f'dendra_capacity_machines {a["machines"]}',
                f'dendra_capacity_gpus {a["gpus"]}',
                f'dendra_capacity_vram_total_mb {a["vram_total_mb"]}',
                f'dendra_capacity_judge_capable {a["judge_capable"]}',
                f'dendra_capacity_distinct_models {a["distinct_models"]}',
            ]
            lines += [f'dendra_capacity_model_nodes{{model="{_prom_label(m)}"}} {int(n)}' for m, n in a["models"].items()]
            self._send(200, ("\n".join(lines) + "\n").encode())
        elif path == "/health":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?")[0] != "/capacity":
            return self._send(404, b'{"error":"not found"}')
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok(ip):
            return self._send(429, b'{"error":"rate limited"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, b'{"error":"body too large"}')
            rep = _clean(json.loads(self.rfile.read(n).decode("utf-8")))
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        if not rep:
            return self._send(400, b'{"error":"node_id required"}')
        key = f'{rep["machine"] or "?"}::{rep["node_id"]}'
        with _LOCK:
            store = _load()
            store[key] = rep
            cutoff = time.time() - PURGE_S
            store = {k: v for k, v in store.items() if v.get("ts", 0) >= cutoff}
            _save(store)
        print(f'[capacity] {rep["node_id"]} tier={rep["tier"]} model={rep["model"]} '
              f'vram={rep["vram_mb"]}MB judge={rep["can_judge"]}', flush=True)
        self._send(200, b'{"status":"ok"}')


if __name__ == "__main__":
    print(f"[capacity] registry on http://{HOST}:{PORT}  (db {DB}; reports are DECLARATIVE, not proven)",
          flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
