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

import base64
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
_MINERS = {"t": 0.0, "ids": set(), "ops": {}}


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


CAPACITY_KIND = "capacity"

# ⛔ TWO KEYSPACES, AND THE PREFIX THAT SEPARATES THEM IS WRITTEN HERE, NOT BY THE SENDER.
# `signe::` used to be reserved by convention only, while the declared key was `<machine>::<node_id>`
# -- and BOTH halves of that one are free strings lifted from the posted body. So an UNSIGNED poster
# sending `machine` = "signe" with `node_id` set to a staked miner id spelled the EXACT key of that
# miner's PROVEN record, and `do_POST` assigns: the proven record was replaced by an anonymous one.
# Measured cost of that single unauthenticated POST: the whole `verified` block falls to zero. That is
# the figure deploy/testnet-miner/publish-capacity.sh reads back to tell an operator they are counted,
# and the one the public chat gate refuses to open below -- so the eviction is silent at both ends and
# looks like a network with no miners.
# A reserved namespace the other branch can spell is not reserved. Both branches now carry a constant
# taken from this file, so neither can name the other. No list of forbidden words to keep up to date,
# and no assumption about which characters a miner id may contain.
KEY_PROVEN = "signe::"
KEY_DECLARED = "decl::"


def _declared_tail(rep: dict) -> str:
    """The sender-chosen half of a declared key. Both components come from the report body."""
    return "%s::%s" % (rep.get("machine") or "?", rep.get("node_id") or "?")


def storage_key(rep: dict) -> str:
    """The key a report is stored under -- and it FOLLOWS THE PROOF (ADR-045, 10).

    It was `machine::node_id`, two strings chosen by the sender AT BOTH ENDS: one party could
    therefore hold an unbounded number of entries all naming the same staked miner, and the
    "verified" block SUMMED them. A PROVEN identity occupies one key, its own: duplicating then costs
    as many operator keys as entries wanted.

    An unproven report keeps a key built from what it declares. It stays accepted and visible -- it
    simply cannot enter the trusted block, and it can no longer reach into it either.

    ⛔ WHAT THIS DOES NOT BUY, written here rather than left to be assumed. INSIDE the declared
    namespace, two different senders that pick the same `machine` and `node_id` still land on one
    entry, and the later deposit replaces the earlier one. Telling those two apart takes a signature
    -- which is precisely what the proven namespace is -- and unsigned deposits stay ACCEPTED
    (ADR-045, 10: one does not add a requirement to an operator that was running yesterday). So this
    is arbitrated, not overlooked, and it costs nothing that is put forward: declared totals are
    published as unauthenticated declarations, and `verified` is out of reach from that namespace."""
    proven = rep.get("_proven") or ""
    if proven:
        return KEY_PROVEN + proven
    return KEY_DECLARED + _declared_tail(rep)


def superseded_keys(rep: dict) -> tuple:
    """Keys this same record occupied BEFORE the namespaces above existed, so a deposit retires them.

    Without this, the fix would publish a false number of its own. The pre-namespace entry stays on
    disk for `PURGE_S` and keeps counting as LIVE for `STALE_S`, so every node already reporting
    would be summed TWICE in the declared totals for a day: an OVER-count, the one direction this
    file refuses everywhere else (see `_band_down` -- understating is honest, overstating is a claim
    we cannot back).

    ⚠️ IT NEVER RETIRES A KEY IN A NAMESPACE THIS FILE OWNS, and that guard is the whole point rather
    than a precaution: `machine` = "decl" turns the tail into `decl::<node_id>`, which is the shape of
    a CURRENT declared key, so an unguarded sweep would hand back the very eviction primitive removed
    just above, one level up. The test is derived from the two constants, never from a second list.
    Beyond that it grants nobody anything: the retired key was already writable by the same
    unauthenticated POST that reaches this line."""
    old = _declared_tail(rep)
    if old.startswith(KEY_PROVEN) or old.startswith(KEY_DECLARED):
        return ()
    return (old,)


def _proven_miner_of(headers, body: bytes) -> str:
    """Returns the miner_id PROVEN by the signature, or "" when the report carries none (or when it
    pas).

    THREE STATES, NEVER TWO. "" means "no proof" and covers two very different cases: an UNSIGNED
    report, which stays accepted (ADR-045, 10: one does not add a requirement to an operator that was
    running yesterday), and a FALSE signature, which must prove nothing. Both stay outside the
    "verified" block; neither makes the deposit refuse.

    THE PROOF GOES ALL THE WAY TO THE OPERATOR. Verifying the signature alone would prove that a key
    signed, not that this key belongs to the miner named: without that last step anyone could sign a
    report naming somebody else's staked miner, which is the original defect with cryptography on top.
    par-dessus."""
    try:
        from modea import relay_signature as _rs, relay_canon, relay_carrier, cosmos_addr
    except Exception:
        return ""
    mid = (headers.get(_rs.HEADER_MINER) or "").strip()
    pub = (headers.get(_rs.HEADER_PUBKEY) or "").strip()
    sig = (headers.get(_rs.HEADER_SIG) or "").strip()
    acct = (headers.get(_rs.HEADER_ACCT) or "").strip()
    seq = (headers.get(_rs.HEADER_SEQ) or "").strip()
    height = (headers.get(_rs.HEADER_HEIGHT) or "").strip()
    if not (mid and pub and sig and acct and seq and height):
        return ""
    try:
        # ⛔ THE HEADERS CARRY BASE64; EVERY CONSUMER BELOW TAKES RAW BYTES. `verifier` does
        # `bytes(pubkey)`, which on a str raises, and `address_from_pubkey` wants 33 bytes. Passing the
        # strings therefore failed inside the try, the bare except read that as "no proof", and the
        # verified block could never leave zero FOR ANYONE -- a whole feature silent, with no error
        # anywhere. `relay.py` decodes at the same spot; this sibling read the same headers
        # without it. Decoding here keeps that single shape: header in, bytes onward.
        pub_b = base64.b64decode(pub, validate=True)
        sig_b = base64.b64decode(sig, validate=True)
        message = relay_canon.canonical_message(CAPACITY_KIND, mid, body, mid, int(height))
        verif = relay_carrier.verifier(acct, seq)
        if not verif(pub_b, message, sig_b):
            return ""
        address = cosmos_addr.address_from_pubkey(pub_b, "dendra")
    except Exception:
        return ""
    if not address:
        return ""
    return mid if _onchain_miner_operators().get(mid) == address else ""


def _onchain_miner_operators() -> dict:
    """{miner_id: operator address} -- the only table that lets a report be PROVEN to come from the
    miner it names. It shares the cache and the fallback of `_onchain_miner_ids`: an RPC hiccup must not
    flip everyone to "unproven", which would be a FALSE statement produced by a LOCAL outage."""

    _onchain_miner_ids()
    return _MINERS.get("ops") or {}


def _onchain_miner_ids() -> set:
    """Miner ids actually registered on-chain — lets the UI separate a staked identity from a claim."""
    if time.time() - _MINERS["t"] < NODE_CACHE_S:
        return _MINERS["ids"]
    ids, ops, ok = set(), {}, False
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
                    # The OPERATOR is what allows an identity to be PROVEN (ADR-045, 10): without it
                    # one can only take the report at its word, which is what the "verified" block did
                    # while announcing that inflating it "requires staking first".
                    ops[m["miner_id"]] = m.get("operator") or ""
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
    _MINERS["t"], _MINERS["ids"], _MINERS["ops"] = time.time(), ids, ops
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
        # A CAPABILITY IS UNREADABLE WITHOUT THE RESOURCE THAT GRANTS IT. `can_judge` is true on
        # two very different boxes: a card with enough VRAM, or a machine with enough RAM running
        # the judge on CPU. Publishing the capability while dropping the backend leaves a reader
        # to infer the requirement from the row next to it — and the VRAM figure of a CPU judge
        # is the wrong number to infer it from. Kept at ingest so publication has something true
        # to carry: a field added at the emission layer alone would serve "" for every node.
        "judge_backend": s("judge_backend", 8),
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
        # "gpu" or "cpu" — see the ingest note: the capability alone does not say what grants it.
        "judge_backend": str(rep.get("judge_backend", ""))[:8],
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
        # DECLARING IS NO LONGER ENOUGH (ADR-045, 10). `miner_ids` was written by the reporting node,
        # and `node_id` is a name the operator chooses: the second clause therefore hung
        # "registered_onchain" on a name rather than on an identity. Only a signature that goes back to
        # the miner OPERATOR is what counts here.
        proven = rep.get("_proven") or ""
        matched = [proven] if (proven and proven in onchain) else []
        registered = bool(matched)
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
        # ⛔ `gpus` COUNTS NODES, NOT CARDS, AND ITS NAME SAYS THE OPPOSITE.
        # `gpu_count` is DELIBERATELY dropped from the published row (de-anonymisation: on a page
        # where nearly every box has exactly one, it is a distinguisher). So this total CANNOT count
        # cards — it increments once per node. The arithmetic is right; the label lies, and the label
        # travelled: the public documentation presents this field as "how many GPUs are actually
        # serving". An operator running five cards reads 1 there, and concludes the network cannot
        # see them.
        # `gpu_nodes` is the honest name. `gpus` keeps being served, with the SAME value, until the
        # surfaces that read it have migrated: a consumer is not broken in the same pass that fixes a
        # label — that is how one correction opens the next.
        "gpu_nodes": gpus, "gpus": gpus,
        "vram_total_mb": vram, "ram_total_mb": ram, "cpu_cores_total": cores,
        "judge_capable": judges, "distinct_models": len(models),
        # A COUNT OF LIVE NODES MEANS NOTHING WITHOUT THE WINDOW THAT DEFINES "LIVE". A node is
        # counted until its last report ages past this many seconds, so a box that stopped
        # reporting keeps being counted for the rest of the window — and after a chain restart
        # the on-chain flag on each row updates at once while the hardware reports do not. The
        # two halves of this document can therefore describe two different moments. Publishing
        # the window is what lets a reader tell how wide that gap can be, instead of guessing.
        "stale_threshold_s": STALE_S,
        "models": models, "tiers": tiers, "nodes": nodes,
        # STAKED sub-total: the same quantities, restricted to nodes whose identity exists in the
        # on-chain registry. This is the block public surfaces should display.
        "verified": {
            "live_nodes": v_live, "machines": len(v_machines),
            "gpu_nodes": v_gpus, "gpus": v_gpus,   # see the block above: NODES, never cards
            "vram_total_mb": v_vram, "ram_total_mb": v_ram, "cpu_cores_total": v_cores,
            "judge_capable": v_judges,
        },
        "_provenance": {
            "declarative": True,
            "claim": "Operator-declared hardware inventory. NOT proven on-chain: a node can claim any "
                     "GPU. `registered_onchain` only tells you the id exists in the miner registry.",
            "verified_subset": "The `verified` block sums ONLY nodes that PROVED the miner identity "
                               "they report under: the deposit carries a signature, and the signing "
                               "address must be the OPERATOR that the on-chain registry records for "
                               "that miner id. Naming a staked id is not enough, and neither is a "
                               "chosen `node_id` that looks like one. Inflating this block therefore "
                               "requires the operator key of a staked miner, which is what makes it "
                               "the figure public surfaces should display. Unsigned reports are still "
                               "ACCEPTED and still appear in the top-level totals -- those are "
                               "unauthenticated declarations, kept for transparency, not for display.",
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
            raw = self.rfile.read(n)
            rep = _clean(json.loads(raw.decode("utf-8")))
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        if not rep:
            return self._send(400, b'{"error":"node_id required"}')
        # ADR-045 (10). The signature is OPTIONAL and the deposit never refuses over it: what changes is
        # what one is then allowed to ASSERT. An unsigned report stays visible; it simply can no longer
        # count itself as staked.
        rep["_proven"] = _proven_miner_of(self.headers, raw)
        # THE STORAGE KEY FOLLOWS THE PROOF, IN A NAMESPACE THE SENDER CANNOT SPELL. The declared half
        # is chosen by the sender AT BOTH ENDS: one party could hold an unbounded number of entries all
        # naming the same staked miner, and -- until the prefixes above -- could also write the key of
        # a PROVEN record and delete it. A proven identity occupies ONE key, its own, and only a proof
        # reaches that namespace.
        key = storage_key(rep)
        with _LOCK:
            store = _load()
            for retired in superseded_keys(rep):
                store.pop(retired, None)
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
