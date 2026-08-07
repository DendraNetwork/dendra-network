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
# `list-miner` pagination: without an explicit limit the SDK returns 100 rows (DefaultLimit) — see
# `_onchain_miner_ids`. The overall budget bounds the time spent under the lock as the registry grows.
PAGE_LIMIT = int(os.environ.get("DENDRA_CAPACITY_PAGE_LIMIT", "500"))
MAX_PAGES = int(os.environ.get("DENDRA_CAPACITY_MAX_PAGES", "200"))
PAGE_BUDGET_S = float(os.environ.get("DENDRA_CAPACITY_PAGE_BUDGET_S", "20"))

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
        # ⛔ PAGINATION IS SUBSTANTIVE HERE, not a matter of form.
        #
        # `ListMiner` goes through `query.CollectionPaginate`, and the SDK applies `DefaultLimit = 100`
        # when the request carries no limit: a single call with neither `--page-limit` nor `--page-key`
        # answers a PREFIX of reality beyond 100 registered miners, with no error at all.
        #
        # Concretely: `registered_onchain` is precisely the field that separates "on-chain staked
        # identity" from "anonymous hardware claim" on the public /network page and in Prometheus.
        # Every miner past the 100th would be shown as NOT REGISTERED — a false public statement born
        # of local truncation, on the exact axis this page exists to make trustworthy. And the
        # degradation is MONOTONE: the larger the network, the more honest operators are presented as
        # anonymous. A truncation whose effect is to report LESS reads like a clean measurement.
        #
        # ⚠️ The known set is never overwritten during an RPC outage, so that a local unavailability
        # cannot produce a false statement. Truncation is the same fault and takes the same treatment:
        # an INCOMPLETE read is handled exactly like an outage — keep the previous set, do not refresh
        # the timestamp, retry on the next pass. NEVER a partial set.
        node = os.environ.get("DENDRA_NODE", "")
        key, pages = "", 0
        fin = time.time() + PAGE_BUDGET_S
        while pages < MAX_PAGES:
            cmd = ["dendrad", "query", "jobs", "list-miner",
                   "--page-limit", str(PAGE_LIMIT), "--output", "json"]
            if key:
                cmd += ["--page-key", key]
            if node:
                cmd += ["--node", node]
            reste = fin - time.time()
            if reste <= 0:
                raise TimeoutError("pagination budget exhausted")
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=min(8, max(1, reste))).stdout
            d = json.loads(out)
            for m in (d.get("miner") or []):
                if m.get("miner_id"):
                    ids.add(m["miner_id"])
            pag = d.get("pagination") or {}
            suivant = pag.get("next_key") or pag.get("nextKey") or ""
            pages += 1
            if not suivant:
                ok = True
                break
            if suivant == key:
                # The same key returned twice: the server is not making progress. Looping forever would
                # hold the lock and burn the budget; stopping while claiming completion would yield a
                # partial set. Failing instead keeps the previous set.
                raise ValueError("stalled page_key -> incomplete read")
            key = suivant
        if not ok:
            raise ValueError(f"{MAX_PAGES} pages without reaching the end of the list -> incomplete read")
    except Exception:
        ok = False
    if not ok:
        # Chain unreachable -> KEEP the last known set and do NOT refresh the timestamp (retry at once).
        # Overwriting it with an empty set would flip every node to "unregistered" on a public page for
        # the duration of an RPC hiccup: a false statement produced by a LOCAL outage. An EMPTY-but-
        # successful answer is different and IS recorded (a fresh genesis legitimately has no miners).
        return _MINERS["ids"]
    _MINERS["t"], _MINERS["ids"] = time.time(), ids
    return ids


def _prom_label(v):
    """Escape a Prometheus LABEL value (text exposition format).

    Without this, a client-controlled value containing a quote and a newline CLOSES the label and then
    OPENS a new line: the caller can write whatever metric it likes (`dendra_capacity_gpus 999999`),
    and everything reading these metrics — alerting, the public page — displays a figure chosen by an
    anonymous party. The /capacity endpoint is unauthenticated.

    Escaping happens here, at WRITE time, rather than at input only: this is the single place that
    sees every label, hence the only one where forgetting a future field is impossible.
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
        # Restricted character set AT INPUT (the belt; escaping at emission time is the braces).
        # A real model identifier looks like "llama3.1:8b-instruct-q4_K_M": nothing else has any reason
        # to get in, and what does not get in cannot come back out inside a metric.
        "model": _RE_MODEL.sub("", s("model", 80)), "can_judge": bool(rep.get("can_judge")),
        "ts": int(time.time()),
    }


# ---------------------------------------------------------------------------------------------
# COARSENING BEFORE PUBLICATION. Exact hardware is a FINGERPRINT, not an inventory.
# A precise GPU model, together with an exact VRAM figure, an exact RAM figure and an exact core
# count, identifies one machine among approximately all of them — and it cross-references any public
# message its operator ever wrote about their own rig. The page needs to show how much capacity the
# network has; it never needs the exact figures of one box.
#
# ⚠️ COARSENING THE ROWS IS USELESS IF THE TOTALS STAY EXACT. Subtracting two snapshots taken before
# and after a node joins returns that node's exact fingerprint, with no search at all — and at N=1 the
# total simply IS the node. So the totals are coarsened by the same function, and the aggregates are
# summed FROM THE COARSENED VALUES, never from the raw ones.
# Rendering cost: ZERO. site/network/index.html already prints (v/1024).toFixed(1), so 8151 and 8192
# both read "8.0 GB". Nothing on the page changes; only the fingerprint disappears.
_CPU_BANDS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


def _band_down(n: int, bands=_CPU_BANDS) -> int:
    """Snap DOWN to a band floor. Downwards on purpose: understating capacity is honest, overstating
    it is a claim we cannot back."""
    lo = bands[0]
    for b in bands:
        if n >= b:
            lo = b
    return lo if n > 0 else 0


# Sizes hardware actually ships in. Snapping to the NEAREST of these, rather than to the nearest GiB,
# is what merges anonymity sets: 32046, 32768 and 31500 all become "32 GB", so three different boxes
# stop being three different numbers. Nearest-GiB would have turned 32046 into 31 GB — still unique,
# and wrong-looking for a machine everyone calls a 32 GB machine.
_SIZES_MB = tuple(g * 1024 for g in (1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024))


def _round_mb(mb: int) -> int:
    """Snap to the nearest standard size. The page reads the same; the exact value is gone."""
    if mb <= 0:
        return 0
    return min(_SIZES_MB, key=lambda s: abs(s - mb))


def _gpu_family(name: str) -> str:
    """SKU -> family. The family is what tells you what the network can run; the SKU is what tells you
    WHOSE machine it is. Unknown vendors collapse to the first word rather than passing through: an
    unrecognised string is exactly where a distinctive one hides."""
    n = (name or "").strip()
    if not n:
        return ""
    low = n.lower()
    for pat, fam in (
        ("rtx 50", "NVIDIA RTX 50 series"), ("rtx 40", "NVIDIA RTX 40 series"),
        ("rtx 30", "NVIDIA RTX 30 series"), ("rtx 20", "NVIDIA RTX 20 series"),
        ("gtx 16", "NVIDIA GTX 16 series"), ("h100", "NVIDIA datacenter"),
        ("a100", "NVIDIA datacenter"), ("l40", "NVIDIA datacenter"),
        ("radeon", "AMD Radeon"), ("apple", "Apple silicon"), ("intel", "Intel"),
    ):
        if pat in low:
            return fam
    return n.split()[0][:24]


def _public_row(rep: dict, stale: bool, registered: bool) -> dict:
    """ALLOW-LIST, never `dict(rep)`. A copy publishes every field a future contributor adds to the
    stored record, including one they never meant to expose. Deliberately DROPPED and why:
      machine      — 48 stable bits per box; it is the join key that survives every id change;
      miner_ids /
      onchain_miners — they tie the hardware fingerprint to the on-chain identity and its address,
                     which is precisely the link this whole change exists to cut. The boolean
                     `registered_onchain` carries the only fact the page actually uses;
      gpu_count    — a distinguisher on a page where nearly every box has exactly one;
      ts (second)  — a per-second clock leaks timezone and uptime rhythm. `stale` is what is read."""
    return {
        "node_id": rep.get("node_id", ""),
        "backend": rep.get("backend", ""),
        "gpu": _gpu_family(rep.get("gpu", "")),
        "vram_mb": _round_mb(int(rep.get("vram_mb", 0))),
        "ram_mb": _round_mb(int(rep.get("ram_mb", 0))),
        "cpu_cores": _band_down(int(rep.get("cpu_cores", 0))),
        "tier": int(rep.get("tier", 0)),
        "model": rep.get("model", ""),
        "can_judge": bool(rep.get("can_judge")),
        "stale": stale,
        "registered_onchain": registered,
    }


def aggregate(store: dict) -> dict:
    now = time.time()
    onchain = _onchain_miner_ids()
    nodes, machines, models, tiers = [], set(), {}, {}
    vram = ram = cores = gpus = 0
    judges = live = 0
    # TOTALS RESTRICTED TO ON-CHAIN IDENTITIES.
    # The endpoint is public and unauthenticated: anyone can POST `gpu_count: 5000`. The per-field
    # bounds prevent absurd values, but NOTHING prevents summing nodes that do not exist — so the
    # declared total is, by construction, chosen by the most motivated anonymous party. A parallel
    # total is computed counting ONLY nodes whose identity appears in the on-chain miner registry;
    # inflating that one requires staking first. The declared total stays exposed (nothing is hidden),
    # but it is no longer the figure to put forward.
    v_vram = v_ram = v_cores = v_gpus = 0
    v_judges = v_live = 0
    v_machines = set()
    for rep in store.values():
        stale = (now - rep.get("ts", 0)) > STALE_S
        # The match is computed but NEVER emitted: only the boolean leaves this process. Publishing
        # `matched` would republish the very on-chain identities the coarsening exists to unlink.
        matched = sorted(set(rep.get("miner_ids") or []) & onchain)
        registered = bool(matched) or (rep.get("node_id") in onchain)
        row = _public_row(rep, stale, registered)
        nodes.append(row)
        if stale:
            continue
        live += 1
        # `machine` is no longer published, and it is no longer counted either: the count of DISTINCT
        # boxes is one more equation for whoever differences two snapshots. node_id is the unit here.
        machines.add(rep.get("node_id"))
        models[rep.get("model", "?")] = models.get(rep.get("model", "?"), 0) + 1
        tiers[str(rep.get("tier", 0))] = tiers.get(str(rep.get("tier", 0)), 0) + 1
        # Summed from the COARSENED row, never from `rep`: a total built on raw values hands back the
        # exact fingerprint of the newcomer by simple subtraction.
        vram += row["vram_mb"]
        ram += row["ram_mb"]
        cores += row["cpu_cores"]
        gpus += 1
        judges += 1 if row["can_judge"] else 0
        if registered:
            v_live += 1
            v_machines.add(rep.get("node_id"))
            v_vram += row["vram_mb"]
            v_ram += row["ram_mb"]
            v_cores += row["cpu_cores"]
            v_gpus += 1
            v_judges += 1 if row["can_judge"] else 0
    nodes.sort(key=lambda r: (-r.get("tier", 0), r.get("node_id", "")))
    return {
        "generated_at": int(now),
        "live_nodes": live, "known_nodes": len(nodes), "machines": len(machines),
        "gpus": gpus, "vram_total_mb": vram, "ram_total_mb": ram, "cpu_cores_total": cores,
        "judge_capable": judges, "distinct_models": len(models),
        "models": models, "tiers": tiers, "nodes": nodes,
        # STAKED sub-total: the same quantities, restricted to nodes whose identity exists in the
        # on-chain registry. This is the block public surfaces should display.
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
