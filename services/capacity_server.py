#!/usr/bin/env python3
"""capacity_server.py — network capacity registry (what hardware and which models the network runs).

  POST /capacity   <- a node posts its deploy/hw_probe.sh report (JSON)
  GET  /capacity   -> aggregate + per-node list (feeds the /network page and Grafana)
  GET  /metrics    -> Prometheus exposition of the same aggregate
  GET  /health

Read this before showing any number to a user: these reports are declared by operators, and nothing
here is cryptographically proven, so a node can claim any GPU. The node_id is checked against the
on-chain miner registry and `registered_onchain` lets the reader tell a staked identity from an
anonymous claim, but the hardware itself cannot be verified. This is an inventory, not "the network's
proven power" (the Proof feed, by contrast, is on-chain state).

One machine, one row: a node reports unsigned when it joins and signed once its miner is registered; the
declared twin of a signed report (same machine, same identity) is folded into it (`declared_folded_keys`),
so one machine is listed and counted once.

The `onchain` block is not declared by anyone: it is read from the chain by this service (registered miners,
and those with a presence proof the chain accepted in the current or previous availability window). A read
that fails is published as `measured: false` with its reason, never as a zero (`onchain_block`).

Persistence: on disk, written atomically, since an in-memory registry would lose everything at each
restart.
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
# The `onchain` block: how long one reading of the chain is served before the next one, and how many pages
# (100 proofs each) the presence search may read before it refuses. A search that does not end is refused,
# never cut: a prefix of the proofs would publish fewer present miners than the chain holds.
ONCHAIN_CACHE_S = float(os.environ.get("DENDRA_CAPACITY_ONCHAIN_CACHE_S", "60"))
ONCHAIN_MAX_PAGES = int(os.environ.get("DENDRA_CAPACITY_ONCHAIN_MAX_PAGES", "50"))
ONCHAIN_CLI_TIMEOUT_S = float(os.environ.get("DENDRA_CAPACITY_ONCHAIN_CLI_TIMEOUT_S", "20"))
# One budget for the whole reading, not one per call. A reading runs in the thread of the GET that found the
# cache expired, and per-call timeouts alone could add up to fifty pages of sixty seconds, while every other
# GET aged past its cached block into `measured: false`. Every CLI call takes what is left of this budget as
# its timeout, and a step that finds it spent is not started. The two status reads go through the season
# reader, which bounds each one itself, so a reading ends within this budget plus at most one of them: below
# 2 * ONCHAIN_CACHE_S, the age up to which the other GETs keep the previous block.
ONCHAIN_BUDGET_S = float(os.environ.get("DENDRA_CAPACITY_ONCHAIN_BUDGET_S", "30"))

_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}
_MINERS = {"t": 0.0, "ids": set(), "ops": {}}
_ONCHAIN = {"t": 0.0, "block": None}
_ONCHAIN_LOCK = threading.Lock()


def _load() -> dict:
    try:
        store = json.loads(DB.read_text("utf-8"))
    except Exception:
        return {}
    return _migrate_proven_prefix(store)


def _migrate_proven_prefix(store):
    """Moves the proven records of a store written by an older server under the current prefix.

    An older server spelled the proven namespace `_KEY_PROVEN_BEFORE_2026_10_10`. Left in place, a record proven
    under that prefix would stay on disk for `PURGE_S` while its next signed report lands under KEY_PROVEN, and
    the identity would be counted twice in `verified`: an over-count, which this file refuses everywhere. When
    both keys exist, the record received last wins."""
    if not isinstance(store, dict):
        return store
    old_prefix = _KEY_PROVEN_BEFORE_2026_10_10
    for old in [k for k in store if isinstance(k, str) and k.startswith(old_prefix)]:
        rec = store.pop(old)
        new = KEY_PROVEN + old[len(old_prefix):]
        cur = store.get(new)
        if not isinstance(cur, dict) or (isinstance(rec, dict) and rec.get("ts", 0) > cur.get("ts", 0)):
            store[new] = rec
    return store


def _save(store: dict) -> None:
    """Atomic write: temp file + replace, so a crash mid-write cannot truncate the registry."""
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        tmp = DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(store), "utf-8")
        os.replace(tmp, DB)
    except Exception as e:
        print(f"[capacity] WARNING: persist failed: {type(e).__name__}", flush=True)


CAPACITY_KIND = "capacity"

# Two keyspaces, and the prefix that separates them is written here, never by the sender. The declared key
# is built from `machine` and `node_id`, two free strings of the posted body; if the proven namespace were
# reserved by convention only, an unsigned poster could spell the exact key of a staked miner's proven
# record and replace it (`do_POST` assigns), emptying the `verified` block that
# deploy/testnet-miner/publish-capacity.sh reads back and the public chat gate relies on. Both branches
# therefore carry a constant taken from this file, so neither can name the other, with no list of forbidden
# words and no assumption about the characters of a miner id
# (tests/test_capacity_declared_identity.py).
KEY_PROVEN = "signed::"
KEY_DECLARED = "decl::"
# The proven prefix as an older store spells it, in French. It is data:
# `_load()` moves those records under KEY_PROVEN, and nothing writes under it any more.
_KEY_PROVEN_BEFORE_2026_10_10 = "signe::"


def _declared_tail(rep: dict) -> str:
    """The sender-chosen half of a declared key. Both components come from the report body."""
    return "%s::%s" % (rep.get("machine") or "?", rep.get("node_id") or "?")


def storage_key(rep: dict) -> str:
    """The key a report is stored under, which follows the proof (ADR-045, item 10).

    A key made of `machine::node_id` alone, two strings the sender chooses, would let one party hold any
    number of entries naming the same staked miner, all summed in the "verified" block. A proven identity
    occupies one key, its own, so duplicating it costs one operator key per entry.

    An unproven report keeps a key built from what it declares: it stays accepted and visible, but it
    cannot enter the trusted block nor reach into it.

    What this does not cover: inside the declared namespace, two senders that pick the same `machine` and
    `node_id` land on one entry, and the later deposit replaces the earlier one. Telling them apart takes a
    signature, which is what the proven namespace is, and unsigned deposits stay accepted (ADR-045, item 10:
    no new requirement for an operator that was running yesterday). This is a decision, and it costs
    nothing that is published as proven: declared totals are presented as unauthenticated declarations,
    and `verified` is out of reach from that namespace."""
    proven = rep.get("_proven") or ""
    if proven:
        return KEY_PROVEN + proven
    return KEY_DECLARED + _declared_tail(rep)


def superseded_keys(rep: dict) -> tuple:
    """Keys this same record occupied before the namespaces above existed, so a deposit retires them.

    Otherwise the pre-namespace entry would stay on disk for `PURGE_S` and count as live for `STALE_S`,
    and every node already reporting would be summed twice in the declared totals for a day: an
    over-count, which this file refuses everywhere (see `_band_down`: understating is honest,
    overstating is a claim that cannot be backed).

    It never retires a key in a namespace this file owns, and that guard is essential: `machine` = "decl"
    turns the tail into `decl::<node_id>`, the shape of a current declared key, so an unguarded sweep would
    bring back the eviction the namespaces remove. The test is derived from the two constants, never from
    a second list. It grants nobody anything else: the retired key was already writable by the same
    unauthenticated POST."""
    old = _declared_tail(rep)
    if old.startswith((KEY_PROVEN, KEY_DECLARED, _KEY_PROVEN_BEFORE_2026_10_10)):
        return ()
    return (old,)


def declared_folded_keys(store: dict) -> set:
    """Keys of the declared reports that a signed report of the same identity supersedes.

    `deploy/join.sh` posts an unsigned report right after the containers start, before the miner exists on
    chain, and once the miner is registered publish-capacity.sh posts a signed one. The two land in two
    namespaces (`storage_key`) on purpose, so without folding every signed machine would also be listed and
    counted as its own unsigned twin: an over-count, which this file refuses everywhere (see `_band_down`).

    The rule: a declared report is folded (left out of `nodes` and of every top-level total) when a signed
    report on the same `machine` exists for its `node_id`, or proves one of the miner ids it lists. The
    signed report is kept, fresh or stale: an unsigned deposit, which anyone can make, must not change how a
    signed identity reads (the reason `storage_key` keeps the namespaces apart).

    It takes both the identity and the machine:
      * the identity alone is not enough: `node_id` is a free string, even under a signature, and it is
        published in every row, so folding on it would let any registered miner hide another machine's
        unsigned row by signing a report with that `node_id`, and a single signed report named `node` (the
        fallback deploy/join.sh uses without a miner id) would fold every machine carrying that name. The
        `machine` key is never published (`_public_row`), so a signer cannot copy it from the page, and both
        twins carry the same one, since join.sh and publish-capacity.sh run the same probe on the same host
        (deploy/hw_probe.sh::MACHINE_KEY, hashed from /etc/machine-id);
      * the machine alone is not enough either: every card of a multi-GPU machine is its own identity, with
        its own `node_id` and the same `machine` (deploy/join.sh --gpus), and folding by machine would hide a
        card whose own signature has not landed yet behind its neighbour's.
    An empty `machine` names no machine: a report without one is never folded and folds nothing.

    What it cannot do: hide a signed report (only declared keys are returned), or touch `verified`, which
    never counted a declared report. A declared report with no signed counterpart stays listed: it is a
    kit older than the signature, or a miner not registered yet."""
    signed_nodes, signed_miners = set(), set()
    for rep in store.values():
        proven = rep.get("_proven") or ""
        machine = rep.get("machine") or ""
        if not (proven and machine):
            continue
        signed_nodes.add((machine, rep.get("node_id") or ""))
        signed_miners.add((machine, proven))
    folded = set()
    for key, rep in store.items():
        machine = rep.get("machine") or ""
        if rep.get("_proven") or not machine:
            continue
        named = rep.get("miner_ids") if isinstance(rep.get("miner_ids"), list) else []
        if (machine, rep.get("node_id") or "") in signed_nodes or any((machine, m) in signed_miners for m in named):
            folded.add(key)
    return folded


def _proven_miner_of(headers, body: bytes) -> str:
    """Returns the miner_id proven by the signature, or "" when the report carries none (or when it does
    not hold).

    "" means "no proof" and covers two different cases: an unsigned report, which stays accepted (ADR-045,
    item 10: no new requirement for an operator that was running yesterday), and a false signature, which
    proves nothing. Both stay outside the "verified" block, and neither makes the deposit fail.

    The proof goes all the way to the operator. A verified signature alone proves that a key signed, not
    that the key belongs to the miner named; without the last step anyone could sign a report naming
    somebody else's staked miner."""
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
        # The headers carry base64, and every consumer below takes raw bytes (`verifier` calls
        # `bytes(pubkey)`, and `address_from_pubkey` wants 33 bytes). Passing the strings would fail inside
        # the try and read as "no proof" for everyone. `relay.py` decodes at the same place.
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
    """{miner_id: operator address} -- the only table that lets a report be proven to come from the
    miner it names. It shares the cache and the fallback of `_onchain_miner_ids`: an RPC hiccup must not
    turn everyone "unproven", a false statement caused by a local outage."""

    _onchain_miner_ids()
    return _MINERS.get("ops") or {}


def _onchain_miner_ids() -> set:
    """Miner ids actually registered on-chain — lets the UI separate a staked identity from a claim."""
    if time.time() - _MINERS["t"] < NODE_CACHE_S:
        return _MINERS["ids"]
    ids, ops, ok = set(), {}, False
    try:
        # Pagination matters here. `ListMiner` goes through `query.CollectionPaginate`, and the SDK applies
        # `DefaultLimit = 100` when the request carries no limit, so a single call answers a prefix beyond 100
        # registered miners, with no error. `registered_onchain` is the field that separates a staked
        # identity from an anonymous hardware claim on the public /network page and in Prometheus: every
        # miner past the 100th would be shown as not registered, more of them as the network grows.
        #
        # The known set is never overwritten during an RPC outage, and an incomplete read is handled the same
        # way: keep the previous set, do not refresh the timestamp, retry on the next pass. Never a partial set.
        node = os.environ.get("DENDRA_NODE", "")
        key, pages = "", 0
        deadline = time.time() + PAGE_BUDGET_S
        while pages < MAX_PAGES:
            cmd = ["dendrad", "query", "jobs", "list-miner",
                   "--page-limit", str(PAGE_LIMIT), "--output", "json"]
            if key:
                cmd += ["--page-key", key]
            if node:
                cmd += ["--node", node]
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("pagination budget exhausted")
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=min(8, max(1, remaining))).stdout
            d = json.loads(out)
            for m in (d.get("miner") or []):
                if m.get("miner_id"):
                    ids.add(m["miner_id"])
                    # The operator is what allows an identity to be proven (ADR-045, item 10): without it
                    # a report could only be taken at its word.
                    ops[m["miner_id"]] = m.get("operator") or ""
            pag = d.get("pagination") or {}
            next_key = pag.get("next_key") or pag.get("nextKey") or ""
            pages += 1
            if not next_key:
                ok = True
                break
            if next_key == key:
                # The same key returned twice: the server is not making progress. Looping would hold the
                # lock and burn the budget, and stopping as if complete would give a partial set, so the
                # read fails and the previous set is kept.
                raise ValueError("stalled page_key -> incomplete read")
            key = next_key
        if not ok:
            raise ValueError(f"{MAX_PAGES} pages without reaching the end of the list -> incomplete read")
    except Exception:
        ok = False
    if not ok:
        # Chain unreachable: keep the last known set and do not refresh the timestamp, so the next call
        # retries. An empty set would turn every node "unregistered" on a public page during an RPC hiccup.
        # A successful empty answer is different and is recorded (a fresh genesis has no miners).
        return _MINERS["ids"]
    _MINERS["t"], _MINERS["ids"], _MINERS["ops"] = time.time(), ids, ops
    return ids


# ---------------------------------------------------------------------------------------------
# The `onchain` block: what the chain says, read by this service, beside what operators declare.
# The public chat counts miners registered and present. Those two numbers are chain state, so they are
# read from the chain and never inferred from the reports above, which anyone can post.
#   registered  the miners in the on-chain registry -- the same read the `registered_onchain` flag of
#               every row comes from (`_onchain_miner_ids`): one source, never a second registry read;
#   present     the registered miners with at least one presence proof (MsgProveAvailability) that the
#               chain accepted at a height from `window_start` to `height`: the current availability window
#               or the one before it, the two windows the chain's draws accept
#               (x/jobs/keeper/presence.go::minerPresentAt). The window length, `avail_epoch_blocks`, is
#               read on chain, never assumed;
#   window_start  the first height whose proof counts.
# A reading that fails is not a zero. Every step that cannot be read returns `measured: false` and the
# reason, and no count: "nobody is present" and "the node did not answer" must never look alike. The status,
# the index check and the CLI are final_season_chain's (`status`, `require_index_from`, `_cli`), the ones the
# season pays from. The proof search is `_present_ids` below rather than the season's `presence_proofs`, which
# gives each page the CLI's own sixty seconds with no overall deadline and stops on an empty page even when the
# index counted more. Same query and same rules for which proof counts; the bench pins the two to the same
# answer (test_the_presence_reader_agrees_with_the_seasons).
# What `present` does not count: a miner registered in this window or the previous one is drawable before
# its first proof (the newcomer's grace in minerPresentAt). Counted here is a proof, not a grace.


def _unmeasured(why: str) -> dict:
    """The only shape a failed reading takes: no count at all, and the reason."""
    return {"measured": False, "why": why}


def _left(deadline: float) -> float:
    return deadline - time.monotonic()


def _spent(deadline: float) -> bool:
    """True once the reading's budget is gone: the next step is not started."""
    return _left(deadline) <= 0


def _call_timeout(deadline: float) -> float:
    """The timeout of one CLI call: what is left of the reading's budget, never the CLI's own minute."""
    return min(ONCHAIN_CLI_TIMEOUT_S, max(0.5, _left(deadline)))


def _search_total(d: dict) -> int:
    """The search's own count of matches. proto3 omits a zero, so an absent count is 0 -- and that cannot
    end a search early: `_present_ids` refuses any reading whose distinct transactions differ from this
    count, so a page that holds proofs under an absent count is refused, never read as complete."""
    v = d.get("total_count", 0)
    n = int(v)                      # a count that is present and not an integer raises: unreadable
    if n < 0:
        raise ValueError("negative total_count")
    return n


def _present_ids(fsc, node: str, first: int, last: int, deadline: float) -> set:
    """Miner ids with a presence proof (MsgProveAvailability) the chain accepted at a height in [first, last],
    read to the end within `deadline`, or an exception -- never a prefix.

    Which proof counts -- the season's rules (final_season_chain.py::presence_proofs), kept identical: an
    accepted transaction (`code` absent is 0, proto3), at a height inside the range (the query bounds it, and
    it is checked again here), and only messages of the availability type: other messages name a miner too,
    and one transaction can hold several.

    What makes a reading whole, each one a refusal and never a cut:
      * the count, read on the first page, fits in ONCHAIN_MAX_PAGES -- otherwise refused at once, rather
        than after reading every page it allows;
      * every page carries the same count: an index that moved during the reading served two different sets;
      * the distinct transactions read equal that count, by hash. An empty page before the count is reached
        ends the loop and then fails this test; so does a page that repeats an earlier one, which a raw
        tally would have taken for new proofs;
      * every call takes what is left of the reading's budget as its timeout."""
    query = "message.action='%s' AND tx.height>=%d AND tx.height<=%d" % (fsc.AVAIL_ACTION, int(first), int(last))
    seen, hashes, total, page = set(), set(), 0, 0
    while page < ONCHAIN_MAX_PAGES:
        if _spent(deadline):
            raise TimeoutError("the reading's budget ran out during the presence search")
        page += 1
        d = fsc._cli(["query", "txs", "--query", query, "--page", str(page), "--limit", "100"], node,
                     timeout=_call_timeout(deadline))
        count = _search_total(d)
        if page == 1:
            total = count
            if total > ONCHAIN_MAX_PAGES * 100:
                raise ValueError("%d proofs to read, more than %d pages of 100" % (total, ONCHAIN_MAX_PAGES))
        elif count != total:
            raise ValueError("the index changed while it was read (%d, then %d)" % (total, count))
        txs = d.get("txs") or []
        for t in txs:
            txhash = t.get("txhash")
            if not isinstance(txhash, str) or not txhash:
                raise ValueError("a transaction without a hash: the reading cannot be checked whole")
            hashes.add(txhash)
            if int(t.get("code", 0) or 0) != 0:
                continue                      # a refused proof proved nothing
            h = int(t.get("height", 0) or 0)
            if not first <= h <= last:
                continue
            for m in ((t.get("tx") or {}).get("body") or {}).get("messages") or []:
                if m.get("@type") != fsc.AVAIL_ACTION:
                    continue
                mid = m.get("miner_id") or m.get("minerId")
                if mid:
                    seen.add(mid)
        if not txs or len(hashes) >= total:
            break
    if len(hashes) != total:
        raise ValueError("%d distinct transactions read where the index counts %d: refused, never cut"
                         % (len(hashes), total))
    return seen


def _onchain_log(stage: str, e: Exception) -> None:
    # The detail goes to the operator's log; the public block carries a fixed sentence per stage. An error
    # message can hold an internal address or a path, and this endpoint is public.
    print(f"[capacity] onchain {stage}: {type(e).__name__}: {str(e)[:200]}", flush=True)


def _read_onchain() -> dict:
    """One reading of the chain for the `onchain` block (see above). Never raises, and ends within
    ONCHAIN_BUDGET_S plus at most one bounded status read (see ONCHAIN_BUDGET_S)."""
    node = os.environ.get("DENDRA_NODE", "")
    if not node:
        return _unmeasured("no node is configured for this service (DENDRA_NODE), so the chain is not read")
    try:
        import final_season_chain as fsc
    except Exception as e:  # noqa: BLE001
        _onchain_log("reader", e)
        return _unmeasured("the chain reader is not installed beside this service")
    deadline = time.monotonic() + ONCHAIN_BUDGET_S
    over_budget = _unmeasured("the chain did not answer within the reading's budget (%d seconds)"
                              % int(ONCHAIN_BUDGET_S))
    # 1. The registry, from the read the rows use. `_onchain_miner_ids` keeps its last set on a failed read
    # (so a local outage does not flip every row to "unregistered"), which is right for a flag and wrong for
    # a count: the count is published only while the last successful read is recent.
    ids = set(_onchain_miner_ids())
    read_at = _MINERS.get("t") or 0.0
    if not read_at or time.time() - read_at > 2 * NODE_CACHE_S:
        return _unmeasured("the on-chain miner registry could not be read")
    rpc = fsc.rpc_url(node)
    # 2. The height, from a node that says it is not catching up. `catching_up` is a bool whose expected value
    # is false, so an absent flag is not read as false: it is not known.
    if _spent(deadline):
        return over_budget
    try:
        si = fsc.status(rpc).get("sync_info")
        height = int(si["latest_block_height"])
        catching_up = si["catching_up"]
    except Exception as e:  # noqa: BLE001
        _onchain_log("status", e)
        return _unmeasured("the node's status could not be read")
    if catching_up is not False:
        return _unmeasured("the node is catching up (or did not say): its latest block is not the network's")
    # 3. The window length. proto3 omits a zero field, so absent from a parameters answer that was read is 0
    # -- and 0 means the chain refuses every presence proof: there is nothing to count, which is not zero.
    if _spent(deadline):
        return over_budget
    try:
        params = fsc._cli(["query", "jobs", "params"], node, timeout=_call_timeout(deadline))["params"]
        if not isinstance(params, dict):
            raise ValueError("params is not an object")
        eb = fsc.param_int(params, "avail_epoch_blocks")
    except Exception as e:  # noqa: BLE001
        _onchain_log("params", e)
        return _unmeasured("the chain's parameters could not be read")
    if eb <= 0:
        return _unmeasured("avail_epoch_blocks is 0 on this chain: it refuses every presence proof, "
                           "so presence cannot be counted")
    window = height // eb
    first = max(0, (window - 1) * eb)
    # 4. The index must reach back to the window start: a node that pruned it, or was state-synced past it,
    # answers "no proof found" with every sign of success.
    if _spent(deadline):
        return over_budget
    try:
        fsc.require_index_from(rpc, first)
    except Exception as e:  # noqa: BLE001
        _onchain_log("index", e)
        return _unmeasured("the node's transaction index does not reach back to the window start")
    # 5. The proofs the chain accepted in the two windows, read to the end within the budget, or refused.
    try:
        proven = _present_ids(fsc, node, first, height, deadline)
    except Exception as e:  # noqa: BLE001
        _onchain_log("proofs", e)
        return _unmeasured("the presence proofs could not be read whole from the node's transaction index")
    present = len(proven & ids)
    return {"measured": True, "registered": len(ids), "present": present, "window_start": first,
            "height": height, "avail_epoch_blocks": eb}


def onchain_block() -> dict:
    """The `onchain` block, read at most once per ONCHAIN_CACHE_S whatever the number of GETs.

    Called outside the registry lock: a slow node must not hold every deposit and every GET behind it. One
    request refreshes, within ONCHAIN_BUDGET_S plus at most one bounded status read; a request arriving
    during that refresh is served the previous reading while it is recent (2 * ONCHAIN_CACHE_S, longer than
    a refresh can last), and a `measured: false` otherwise -- never a block built in a hurry. A failed
    reading is cached too: it is a true statement about the chain for that period, and retrying it on every
    GET would only hammer a node that is already not answering."""
    now = time.time()
    cached = _ONCHAIN["block"]
    if cached is not None and now - _ONCHAIN["t"] < ONCHAIN_CACHE_S:
        return cached
    if not _ONCHAIN_LOCK.acquire(blocking=False):
        if cached is not None and now - _ONCHAIN["t"] < 2 * ONCHAIN_CACHE_S:
            return cached
        return _unmeasured("the chain is being read by another request")
    try:
        try:
            block = _read_onchain()
        except Exception as e:  # noqa: BLE001
            _onchain_log("unexpected", e)
            block = _unmeasured("the chain could not be read")
        _ONCHAIN["t"], _ONCHAIN["block"] = time.time(), block
        return block
    finally:
        _ONCHAIN_LOCK.release()


# The judge role a report declares -- and why it is not `can_judge`.
# The chain knows no judge role: it draws every present miner into a jury. Only a miner started with the role
# (DENDRA_MINER_JUDGE=1) runs a judge worker, and only a worker whose engine holds the model it judges with posts
# a verdict; any other juror is drawn all the same and says nothing. `can_judge` says a machine has the RAM to
# judge; it says nothing of whether anything judges there. The role is read inside the miner container
# (miner_selftest.py --judge-role) and carried in the report, under the signature when there is one:
#   active   the role is requested, the worker runs, the engine holds the model that worker judges with, and that
#            model is the chain's pin (or the operator's explicit choice)
#   mute     the role is requested, and the worker or the model is measured absent: a seat that says nothing
#   off      the role is not requested
#   unknown  the role could not be read
# absent is unknown, never off. Every report from a kit older than this field carries none, and reading that
# silence as "no judge here" would be a statement nobody made. A word that is not one of the four is unknown too.
JUDGE_ROLES = ("active", "mute", "off", "unknown")


def _judge_role_of(rep: dict) -> str:
    v = rep.get("judge_role")
    return v if isinstance(v, str) and v in JUDGE_ROLES else "unknown"


def _prom_label(v):
    """Escape a Prometheus label value (text exposition format).

    A client-controlled value containing a quote and a newline would close the label and open a new line,
    letting an anonymous caller of the unauthenticated /capacity endpoint write any metric it likes
    (`dendra_capacity_gpus 999999`) for alerting and the public page to display.

    Escaping happens here, at write time, and not only at input: this is the one place that sees every
    label, so a future field cannot be forgotten.
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")

def _clean(rep: dict) -> dict | None:
    """Keep only known fields, bounded: an open endpoint must never store arbitrary operator input."""
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
    # miner_ids: the on-chain identities this box actually runs. node_id is a privacy pseudonym (a hashed
    # machine id) and never matches a registry entry on its own, so without this list `registered_onchain`
    # would read "unregistered" for every honest node, a false statement on a public page.
    raw = rep.get("miner_ids")
    miner_ids = [str(m)[:32] for m in raw[:16] if m] if isinstance(raw, list) else []
    return {
        "node_id": node_id, "miner_ids": miner_ids,
        "machine": s("machine", 64), "backend": s("backend", 8),
        "gpu": s("gpu", 80), "gpu_count": i("gpu_count", 64),
        "vram_mb": i("vram_mb", 2_000_000), "ram_mb": i("ram_mb", 8_000_000),
        "cpu_cores": i("cpu_cores", 1024), "tier": i("tier", 9),
        # A restricted character set at input, in addition to escaping at output. A real model identifier
        # looks like "llama3.1:8b-instruct-q4_K_M", and what does not get in cannot come back out in a metric.
        "model": _RE_MODEL.sub("", s("model", 80)), "can_judge": bool(rep.get("can_judge")),
        # A capability means little without the resource that grants it: `can_judge` is true on a card with
        # enough VRAM and on a machine with enough RAM judging on the CPU, and the VRAM figure of a CPU judge
        # would mislead a reader. Kept at ingest, so that publication has a true value to carry.
        "judge_backend": s("judge_backend", 8),
        # The role, not the capability (see JUDGE_ROLES): absent or unrecognised is "unknown", never "off".
        "judge_role": _judge_role_of(rep),
        # One identity per card (deploy/join.sh --gpus): every card of a machine publishes its own report,
        # and each carries the same machine's RAM and cores. The extra slots say so ("secondary"), so the
        # machine's RAM and cores are counted once. Anything else -- absent included, which is what every
        # older kit sends -- is "primary", counted as before. Kept, never published (see _public_row).
        "host_share": "secondary" if rep.get("host_share") == "secondary" else "primary",
        "ts": int(time.time()),
    }


# ---------------------------------------------------------------------------------------------
# Coarsening before publication: exact hardware is a fingerprint, not an inventory. A precise GPU model with
# exact VRAM, RAM and core counts identifies one machine among nearly all of them, and links it to anything
# its operator ever wrote about that rig. The page needs the network's capacity, never one box's exact
# figures.
#
# The totals are coarsened by the same function, and summed from the coarsened values: exact totals would
# give a node's exact fingerprint by subtracting two snapshots taken before and after it joins (and with one
# node, the total is the node). The page already prints (v/1024).toFixed(1), so 8151 and 8192 both read
# "8.0 GB": nothing visible changes.
_CPU_BANDS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


def _band_down(n: int, bands=_CPU_BANDS) -> int:
    """Snap down to a band floor. Downwards on purpose: understating capacity is honest, overstating it
    is a claim that cannot be backed."""
    lo = bands[0]
    for b in bands:
        if n >= b:
            lo = b
    return lo if n > 0 else 0


# Sizes hardware actually ships in. Snapping to the nearest of these, rather than to the nearest GiB,
# merges anonymity sets: 32046, 32768 and 31500 all become "32 GB", while the nearest GiB would turn 32046
# into 31 GB, still unique.
_SIZES_MB = tuple(g * 1024 for g in (1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024))


def _round_mb(mb: int) -> int:
    """Snap to the nearest standard size. The page reads the same; the exact value is gone."""
    if mb <= 0:
        return 0
    return min(_SIZES_MB, key=lambda s: abs(s - mb))


def _gpu_family(name: str) -> str:
    """SKU -> family. The family is what tells you what the network can run; the SKU is what tells you
    whose machine it is. Unknown vendors collapse to the first word rather than passing through: an
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
    """An allow-list, never `dict(rep)`, which would publish every field a future contributor adds to the
    stored record. Deliberately dropped, and why:
      machine        48 stable bits per box, a join key that survives every id change;
      miner_ids /
      onchain_miners they tie the hardware fingerprint to the on-chain identity and its address, the link
                     this function exists to cut; the boolean `registered_onchain` carries the only fact the
                     page uses;
      gpu_count      a distinguisher on a page where nearly every box has exactly one;
      host_share     "secondary" says this identity shares a machine with another, which would tie
                     identities to one box, as `machine` would;
      ts (second)    a per-second clock leaks the time zone and the uptime rhythm; `stale` is what is read."""
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
        # The judge role this identity declared (JUDGE_ROLES). Read again from the stored record, so a record
        # stored before the field existed is published "unknown", never "off".
        "judge_role": _judge_role_of(rep),
        "stale": stale,
        "registered_onchain": registered,
    }


def aggregate(store: dict, chain: dict | None = None) -> dict:
    """The public document. `chain` is the `onchain` block when the caller read it already (do_GET reads it
    outside the registry lock); otherwise it is read here."""
    now = time.time()
    if chain is None:
        chain = onchain_block()
    onchain = _onchain_miner_ids()
    # One machine, one row (`declared_folded_keys`): the declared twin of a signed report is left out of the
    # rows and of every total below, the top-level ones included. `verified` never counted it.
    folded = declared_folded_keys(store)
    nodes, machines, models, tiers = [], set(), {}, {}
    vram = ram = cores = gpus = 0
    judges = live = 0
    # Totals restricted to on-chain identities. The endpoint is public and unauthenticated, so anyone can
    # POST `gpu_count: 5000`: the per-field bounds stop absurd values, but nothing stops summing nodes that do
    # not exist, and the declared total is whatever the most motivated anonymous party wants. A parallel
    # total counts only proven identities (see below). The declared total stays exposed, but it is not the
    # figure to display.
    v_vram = v_ram = v_cores = v_gpus = 0
    v_judges = v_live = 0
    v_machines = set()
    # The judge roles declared, every value counted: a count of "active" alone would hide the silent seats, which
    # the chain draws like the others. Live reports only, like every other total; `verified` is restricted to the
    # proven reports, like every other verified total.
    roles = {r: 0 for r in JUDGE_ROLES}
    v_roles = {r: 0 for r in JUDGE_ROLES}
    for key, rep in store.items():
        if key in folded:
            continue
        stale = (now - rep.get("ts", 0)) > STALE_S
        # The match is computed but never emitted: only the boolean leaves this process, since publishing
        # `matched` would republish the on-chain identities the coarsening unlinks. Declaring is not enough
        # (ADR-045, item 10): `miner_ids` is written by the reporting node and `node_id` is a name the
        # operator chooses, so only a signature that goes back to the miner's operator counts here.
        proven = rep.get("_proven") or ""
        matched = [proven] if (proven and proven in onchain) else []
        registered = bool(matched)
        row = _public_row(rep, stale, registered)
        nodes.append(row)
        if stale:
            continue
        live += 1
        # `machine` is neither published nor counted: the count of distinct boxes would be one more equation
        # for whoever compares two snapshots. node_id is the unit here.
        machines.add(rep.get("node_id"))
        models[rep.get("model", "?")] = models.get(rep.get("model", "?"), 0) + 1
        tiers[str(rep.get("tier", 0))] = tiers.get(str(rep.get("tier", 0)), 0) + 1
        # Summed from the coarsened row, never from `rep`: a total built on raw values would give a newcomer's
        # exact fingerprint by subtraction.
        # A secondary report (one more card of a machine already reporting) adds its card (its VRAM, and one
        # to gpu_nodes, which then approaches a count of cards), not the machine's RAM and cores, which its
        # primary report already carries. Absent means primary (older kits).
        secondary = rep.get("host_share") == "secondary"
        vram += row["vram_mb"]
        if not secondary:
            ram += row["ram_mb"]
            cores += row["cpu_cores"]
        gpus += 1
        judges += 1 if row["can_judge"] else 0
        roles[row["judge_role"]] += 1
        if registered:
            v_roles[row["judge_role"]] += 1
            v_live += 1
            v_machines.add(rep.get("node_id"))
            v_vram += row["vram_mb"]
            if not secondary:
                v_ram += row["ram_mb"]
                v_cores += row["cpu_cores"]
            v_gpus += 1
            v_judges += 1 if row["can_judge"] else 0
    nodes.sort(key=lambda r: (-r.get("tier", 0), r.get("node_id", "")))
    return {
        "generated_at": int(now),
        "live_nodes": live, "known_nodes": len(nodes), "machines": len(machines),
        # `gpus` counts nodes, not cards: `gpu_count` is deliberately dropped from the published row (on a
        # page where nearly every box has exactly one, it is a distinguisher), so this total increments once
        # per node. `gpu_nodes` is the accurate name; `gpus` is still served with the same value until the
        # surfaces that read it have migrated, so that no consumer breaks.
        "gpu_nodes": gpus, "gpus": gpus,
        "vram_total_mb": vram, "ram_total_mb": ram, "cpu_cores_total": cores,
        "judge_capable": judges, "distinct_models": len(models),
        # Identities that declare an active judge role, and every role declared (see JUDGE_ROLES and
        # `_provenance.judges_declared`). Not `judge_capable`, which counts machines that could judge.
        "judges_declared": roles["active"], "judge_roles": dict(roles),
        # A count of live nodes means nothing without the window that defines "live". A node is counted
        # until its last report is older than this many seconds, so a box that stopped reporting is still
        # counted for the rest of the window, and after a chain restart the on-chain flag of each row
        # updates at once while the hardware reports do not. Publishing the window tells a reader how far
        # apart the two halves of this document can be.
        "stale_threshold_s": STALE_S,
        # Declared reports folded into the signed report of the same identity, so left out of `nodes` and
        # of the totals above (see `_provenance.folding`). Said, rather than dropped without a trace.
        "declared_folded": len(folded),
        "models": models, "tiers": tiers, "nodes": nodes,
        # Read from the chain, not declared (see `_provenance.onchain`): `measured: false` carries no count.
        "onchain": chain,
        # Staked sub-total: the same quantities, restricted to nodes whose identity exists in the
        # on-chain registry. This is the block public surfaces should display.
        "verified": {
            "live_nodes": v_live, "machines": len(v_machines),
            "gpu_nodes": v_gpus, "gpus": v_gpus,   # see the block above: nodes, never cards
            "vram_total_mb": v_vram, "ram_total_mb": v_ram, "cpu_cores_total": v_cores,
            "judge_capable": v_judges,
            "judges_declared": v_roles["active"], "judge_roles": dict(v_roles),
        },
        "_provenance": {
            "declarative": True,
            "claim": "Operator-declared hardware inventory. Not proven on-chain: a node can claim any "
                     "GPU. `registered_onchain` only tells you the id exists in the miner registry.",
            "verified_subset": "The `verified` block sums only nodes that proved the miner identity "
                               "they report under: the deposit carries a signature, and the signing "
                               "address must be the operator that the on-chain registry records for "
                               "that miner id. Naming a staked id is not enough, and neither is a "
                               "chosen `node_id` that looks like one. Inflating this block therefore "
                               "requires the operator key of a staked miner, which is what makes it "
                               "the figure public surfaces should display. Unsigned reports are still "
                               "accepted and still appear in the top-level totals -- those are "
                               "unauthenticated declarations, kept for transparency, not for display.",
            "judges_declared": "Declared, not proven. The chain knows no judge role: it draws every present "
                               "miner into a jury, and only a miner started with the judge role, whose judge "
                               "worker runs and whose judge engine holds the model it judges with, posts a "
                               "verdict. `judges_declared` counts the live reports whose miner container read "
                               "exactly that when the report was made, with that model being the one the "
                               "chain pins (`judge_role` = active); a model the chain did not confirm is "
                               "`unknown`. `judge_roles` "
                               "counts every word: `mute` = the role is requested but the worker or the model "
                               "is missing, so the identity is drawn and says nothing; `off` = not requested; "
                               "`unknown` = not readable, which includes every report from a kit older than "
                               "this field and is never counted as `off`. It is not `judge_capable`, which "
                               "only says a machine has the RAM to judge. It counts identities (jury seats), "
                               "not machines: every identity of a multi-GPU machine judges through one shared "
                               "CPU engine. In `verified`, only reports signed by the on-chain operator count. "
                               "The only proof that a judge works is a verdict it posted on chain (a "
                               "`<job>__verdict__<miner>` commit), served by The Proof feed.",
            "folding": "One machine, one row. A node posts an unsigned report when it joins, before its miner "
                       "exists on chain, and signed reports once the miner is registered. A declared report is "
                       "left out of `nodes` and of every top-level total when a signed report from the same "
                       "machine exists for the same `node_id`, or proves one of the miner ids the declared report "
                       "lists; `declared_folded` counts them. The machine is the probe's hashed machine key, "
                       "never published, so a signer cannot copy it from this page: the `node_id` alone, which "
                       "every row shows, folds nothing. The signed report is the one kept, fresh or stale: an "
                       "unsigned deposit cannot change how a signed identity reads. Folding goes by `node_id` "
                       "and machine together, never by machine alone: each card of a multi-GPU machine is its "
                       "own identity and keeps its own row. A declared report with no signed counterpart stays "
                       "listed. The `verified` block is unchanged: it never counted a declared report.",
            "onchain": "Read from the chain by this service, not declared by anyone. `registered` = miners in "
                       "the on-chain registry (the same read as each row's `registered_onchain`). `present` = "
                       "registered miners with at least one presence proof (MsgProveAvailability) the chain "
                       "accepted at a height from `window_start` to `height`: the current availability window "
                       "(`avail_epoch_blocks`, read on chain) or the previous one, the two windows the chain's "
                       "draws accept. A miner registered within those two windows is drawable before its first "
                       "proof; it is not counted here until it has proven. `measured: false` comes with `why` "
                       "and no count: a reading that failed is not a zero, and neither is a search that could "
                       "not be read whole (every transaction the index counts, within %d seconds). Read at most "
                       "once every %d seconds." % (int(ONCHAIN_BUDGET_S), int(ONCHAIN_CACHE_S)),
            "proven_elsewhere": "On-chain truth (jobs, slashes, VRF) is served by The Proof feed.",
        },
    }


def _rate_ok(ip: str) -> bool:
    """Rate limit per IP, under the lock and purging itself: this map is fed by a public, unauthenticated
    endpoint, and an unbounded dict keyed by remote IP would be a memory leak anyone can drive."""
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
            chain = onchain_block()          # outside the lock: a slow node must not hold the deposits
            with _LOCK:
                agg = aggregate(_load(), chain)
            self._send(200, json.dumps(agg).encode())
        elif path == "/metrics":
            chain = onchain_block()
            with _LOCK:
                a = aggregate(_load(), chain)
            lines = [
                f'dendra_capacity_live_nodes {a["live_nodes"]}',
                f'dendra_capacity_machines {a["machines"]}',
                f'dendra_capacity_gpus {a["gpus"]}',
                f'dendra_capacity_vram_total_mb {a["vram_total_mb"]}',
                f'dendra_capacity_judge_capable {a["judge_capable"]}',
                f'dendra_capacity_judges_declared {a["judges_declared"]}',
                f'dendra_capacity_distinct_models {a["distinct_models"]}',
            ]
            # One line per declared role, every word: the role names are this file's constants, never the sender's.
            lines += [f'dendra_capacity_judge_role_nodes{{role="{_prom_label(r)}"}} {int(a["judge_roles"].get(r, 0))}'
                      for r in JUDGE_ROLES]
            lines += [f'dendra_capacity_model_nodes{{model="{_prom_label(m)}"}} {int(n)}' for m, n in a["models"].items()]
            lines.append(f'dendra_capacity_declared_folded {int(a["declared_folded"])}')
            # The chain's counts are emitted only when they were read. A series that is absent is "not known" to
            # Prometheus; a 0 would be a measurement, and an alert on "no miner present" would fire on an RPC
            # hiccup. `measured` says which of the two a scrape saw.
            oc = a.get("onchain") if isinstance(a.get("onchain"), dict) else {}
            measured = oc.get("measured") is True
            lines.append(f'dendra_capacity_onchain_measured {1 if measured else 0}')
            if measured:
                lines += [f'dendra_capacity_onchain_registered_miners {int(oc["registered"])}',
                          f'dendra_capacity_onchain_present_miners {int(oc["present"])}',
                          f'dendra_capacity_onchain_window_start {int(oc["window_start"])}']
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
        # ADR-045, item 10. The signature is optional and the deposit is never refused over it: it only
        # changes what the report may assert. An unsigned report stays visible but cannot count as staked.
        rep["_proven"] = _proven_miner_of(self.headers, raw)
        # The storage key follows the proof, in a namespace the sender cannot spell (see storage_key): a
        # proven identity occupies one key, its own, and only a proof reaches that namespace.
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
              f'vram={rep["vram_mb"]}MB judge={rep["can_judge"]} judge_role={rep["judge_role"]}', flush=True)
        self._send(200, b'{"status":"ok"}')


if __name__ == "__main__":
    print(f"[capacity] registry on http://{HOST}:{PORT}  (db {DB}; reports are declarative, not proven)",
          flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
