#!/usr/bin/env python3
"""
c3_gate.py — honest C3 readiness report (decision support for a public announcement).

The script never prints `GO`. It informs the announcement decision and leaves it to people: the C3 `GO`
is an irreversible governance decision, and a label that claimed it would claim more than the script
knows. The highest verdict is `ATTESTED_PENDING_SIGNOFF`: everything is green and attested, and only the
announcement decision remains. No verdict exits 0, because automation reads a 0 as "go ahead".

Principle:
  - Criteria measurable on-chain: the report reads them and decides (PASS/FAIL).
  - Criteria that no counter can measure — ">=100 honest miners", ">=2 judge models including >=1 MoE",
    ">=2 genuinely distinct operators" — are never ticked from a counter: they are REQUIRES_ATTESTATION,
    resolved only by an explicit, named and refutable operator attestation (--attest-*). The number of
    on-chain identities is not the number of distinct humans.
  - The preamble applies to the report itself: without `pockets_measured >= 3` (the codec omits zeros, so
    "absent" is indistinguishable from a binary predating the measurement) or with a missing query, the
    verdict is `NO-GO`, fail closed.
  - A failing measurable forces `NO-GO` even when all attestations are present: an attestation never covers
    a measurement.

Self-honesty: on an N=1 chain (contributors=1, resolved_by_quorum=0, distinct_voters<3) the verdict is `NO-GO`.

Usage:
  python3 c3_gate.py --node tcp://HOST:26657 [--rpc http://HOST:26657] [--min-quorum-resolved N] [--max-deferred M]
      [--attest-operators "opA=key,opB=key"] [--attest-models "qwen3:30b-a3b,mistral-nemo"]
      [--attest-honest "<n> honest, run <date>"]
  python3 c3_gate.py --from-json state.json            # evaluate a captured state (offline / test)
  python3 c3_gate.py --node ... --to-json state.json   # capture the on-chain state without deciding

Exit codes: 10 = `NO-GO`; 20 = REQUIRES_ATTESTATION; 30 = ATTESTED_PENDING_SIGNOFF; 2 = usage.
Never 0 for a verdict (see above).
"""
import argparse
import json
import os
import re
import subprocess
import sys

NO_GO = "NO-GO"
NEEDS_ATTEST = "REQUIRES_ATTESTATION"
ATTESTED = "ATTESTED_PENDING_SIGNOFF"
# No verdict exits 0: the C3 `GO` cannot come out of a shell exit code, it is a governance decision.
EXIT = {ATTESTED: 30, NEEDS_ATTEST: 20, NO_GO: 10}
# Operators of the node's event query language, which only accepts them in capitals.
Q_EXISTS = "EXISTS"
Q_AND = "AND"

MOE_MARKERS = ("a3b", "30b")  # MoE judge-model markers (qwen3:30b-a3b...)


def _g(d, *keys, default=None):
    """Read a key tolerating both snake_case and camelCase (dendrad -o json = snake; REST = camel)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --- Named and refutable attestations ---------------------------------------------------------------
# An unverifiable `--attest` boolean is the human version of "the measurement that cannot contradict".
# Naming what is attested is required, so that a third party can refute it after the fact.

def _attest_operators(val):
    if not val:
        return False, "not provided"
    names = sorted(set(x.split("=")[0].strip() for x in val.split(",") if x.split("=")[0].strip()))
    if len(names) >= 2:
        return True, f"attested: {len(names)} named operators ({', '.join(names)})"
    return False, f"provided but insufficient ({len(names)} operator named, need >=2 distinct)"


def _attest_models(val):
    if not val:
        return False, "not provided"
    items = sorted(set(x.strip() for x in val.split(",") if x.strip()))
    moe = any(any(m in it.lower() for m in MOE_MARKERS) for it in items)
    if len(items) >= 2 and moe:
        return True, f"attested: {len(items)} named models ({', '.join(items)}), MoE present"
    if len(items) < 2:
        return False, f"provided but insufficient ({len(items)} model, need >=2)"
    return False, f"provided but no MoE named among ({', '.join(items)}) — need >=1 marker in {MOE_MARKERS}"


def _attest_honest(val):
    if not val:
        return False, "not provided"
    m = re.search(r"\d+", val)
    if not m:
        return False, f"provided but without a verifiable count ('{val}')"
    n = int(m.group())
    if n >= 100:
        return True, f"attested: {n} honest miners declared ('{val}')"
    return False, f"provided but < 100 ({n})"


# ------------------------------------------------------------------ partition core
# The feed (the_proof.py) imports this section, so the explorer and this report classify with the same
# functions. It lives here because this gate must run with the standard library alone, also as a lone
# copy on a host, and the feed's own module imports the node client.

def state_markers(state):
    """The markers of a job state ("open+paid+optimistic+disputed" -> {"open", "paid", ...})."""
    return {m for m in (state or "").split("+") if m}


# The partition classes of jobs that carry `+disputed`, in the order they are published.
DISPUTED_CLASSES = ("audit_pending", "resolved_by_quorum", "resolved_no_quorum", "resolved_by_adjudication",
                    "resolved_unwound", "resolved_unmarked")
UNDISPUTED_UNWOUND = "undisputed_unwound"


def audit_class(markers):
    """The partition class of one job from its state markers: one of DISPUTED_CLASSES, UNDISPUTED_UNWOUND
    (split later by origin), or None for a job no audit touched. the_proof.py publishes with this same
    function, so the feed and the readiness report cannot count differently."""
    if "disputed" in markers:
        if "resolved" not in markers:
            return "audit_pending"
        # The paths are mutually exclusive on chain: jobIsResolved forbids a double resolution.
        if "noquorum" in markers:
            return "resolved_no_quorum"
        if "quorum" in markers:
            return "resolved_by_quorum"
        if "adjudicated" in markers:
            return "resolved_by_adjudication"
        if "unwound" in markers:
            # audit_unwind.go::unwindStrandedRetention: refunded, no jury ruled.
            return "resolved_unwound"
        return "resolved_unmarked"  # jobs older than the markers, or an unknown state: kept visible
    if "resolved" in markers and "unwound" in markers:
        # Unwound without `+disputed`: either the lottery selected the job, found the jury below the floor
        # and unwound it, at once or after deferrals (audit_jury_floor.go::deferAuditForSmallJury; counted
        # in audits_opened, the marker never saved), or the no-seed loop unwound a job never drawn (not
        # counted). `unwind_origin` tells them apart.
        return UNDISPUTED_UNWOUND
    return None


def audit_partition(jobs, deferred, unwind_origin, chain_moved):
    """The partition of the opened audits (ADR-034), checked against the chain's own counter. Pure.

    An audit conducted to a quorum, one that expired without quorum, one adjudicated by a redo committee
    and one unwound with no verdict are different things, and a monitoring gate counts only the quorum
    class. `deferred` and `unwind_origin` are described in the_proof.py::build_proof. `chain_moved` is
    the answer of the function of that name: True, the reads straddle a change of the chain; None, a
    counter read failed. Neither gives a verdict. No argument has a default: an omitted `chain_moved` would
    read as "the chain did not move", a verdict nobody measured."""
    counts = dict.fromkeys(DISPUTED_CLASSES, 0)
    disputed_ids, undisputed_unwound_ids = set(), set()
    for j in jobs:
        cls = audit_class(state_markers(j.get("state")))
        if cls == UNDISPUTED_UNWOUND:
            undisputed_unwound_ids.add(j.get("id", ""))
        elif cls is not None:
            counts[cls] += 1
            disputed_ids.add(j.get("id", ""))
    # `deferred_no_jury`: selected then deferred (on-chain query), deduplicated against the disputed set,
    # since a job selected, deferred and then disputed by a person carries both marks and must land in one
    # class only.
    if deferred is None:
        deferred_no_jury = opened = None
    else:
        # An unwind clears AuditSelected (miner_vitality.go::clearAuditCommittee), so an unwound id cannot
        # be listed here; the exclusion only keeps a job in one class if that ever changes.
        deferred_no_jury = len([d for d in (deferred.get("job_ids") or [])
                                if d and d not in disputed_ids and d not in undisputed_unwound_ids])
        opened = int(deferred.get("audits_opened") or 0)
    # Undisputed unwound jobs, split by origin. Without any, there is nothing to tell apart and both
    # classes are a measured zero. With some, an unread origin, or one job whose origin could not be
    # queried, makes both null, never assumed.
    unknown = set()
    if unwind_origin is not None:
        unknown = set(unwind_origin.get("unknown") or []) & undisputed_unwound_ids
    if not undisputed_unwound_ids:
        after_deferral = never_drawn = 0
    elif unwind_origin is None or unknown:
        after_deferral = never_drawn = None
    else:
        noseed = set(unwind_origin["noseed_unwound"])
        drawn = set(unwind_origin["drawn"])
        never_drawn = len([i for i in undisputed_unwound_ids if i in noseed and i not in drawn])
        after_deferral = len(undisputed_unwound_ids) - never_drawn
    # The partition is checked against an independent source: comparing the sum of the classes with
    # `audited` would compare two numbers counted by this same loop, true by construction (ADR-033,
    # question 6). The other side is `audits_opened`, counted by the chain when audits open. When it, or
    # the origin of an undisputed unwind, is not readable, `partition_ok` is null, never true by default.
    reason = None
    if opened is None:
        partition_ok = bounds_ok = None
        reason = "audits_opened unreadable"
    elif chain_moved is None:
        # `deferred` is the read after the listing and it answered, so the read before it did not: an
        # unknown move is not "no move".
        partition_ok = bounds_ok = None
        reason = "counter unread before the listing"
    elif chain_moved:
        # An audit opened or unwound between two reads: the counts disagree for one refresh without any
        # state hiding, so neither check gives a verdict.
        partition_ok = bounds_ok = None
        reason = "the chain moved during the read"
    else:
        base = sum(counts.values()) + deferred_no_jury
        # The weaker check needs no event: every undisputed unwind counts at most once. It can still fail.
        bounds_ok = base <= opened <= base + len(undisputed_unwound_ids)
        if after_deferral is None:
            partition_ok = None
            reason = (f"unwind origin unreadable for {len(unknown)} job id(s)" if unknown
                      else "unwind origin unreadable")
        else:
            partition_ok = (base + after_deferral) == opened
    return {"audits_opened": opened, **counts,
            "unwound_after_deferral": after_deferral, "unwound_never_drawn": never_drawn,
            "deferred_no_jury": deferred_no_jury, "partition_ok": partition_ok,
            "partition_bounds_ok": bounds_ok, "partition_unmeasured_reason": reason}


def chain_moved(before, after):
    """Whether the reads a partition is built from straddle a change of the chain: True, False, or None
    when either read failed, since an unread counter cannot show that nothing moved.

    `before` and `after` are audit_deferred read before the listing and after the event search. An audit
    that opens, a selection, or an unwind of a selected job between them changes the counter or the
    selected ids, and would publish partition_ok false for one refresh with no state hiding. A no-seed
    unwind of a job never selected changes neither, and moves no job between partition classes."""
    if before is None or after is None:
        return None
    if int(before.get("audits_opened") or 0) != int(after.get("audits_opened") or 0):
        return True
    return set(before.get("job_ids") or []) != set(after.get("job_ids") or [])


def rpc_reader(base, timeout=30):
    """`rpc(path, params)` for the CometBFT RPC at `base`: a read-only GET that returns the `result`
    object and raises on any failure."""
    from urllib.parse import urlencode
    from urllib.request import urlopen
    base = base.rstrip("/")

    def rpc(path, params):
        url = base + path + ("?" + urlencode(params) if params else "")
        with urlopen(url, timeout=timeout) as r:
            d = json.load(r)
        if not isinstance(d, dict) or d.get("error") is not None or not isinstance(d.get("result"), dict):
            raise ValueError("rpc answer without a result")
        return d["result"]
    return rpc


def _search_heights(rpc, query, per_page=100, max_pages=100):
    """The heights a CometBFT block_search returns for `query`, ascending, and whether the walk reached
    the end. A walk stopped at `max_pages` is still complete up to its last height, since the answer is
    sorted by height, so the caller can resume from there. Raises when the answer does not add up."""
    heights, page = [], 1
    while True:
        res = rpc("/block_search", {"query": f'"{query}"', "page": str(page), "per_page": str(per_page),
                                    "order_by": '"asc"'})
        total = int(res["total_count"])
        got = [int(b["block"]["header"]["height"]) for b in (res.get("blocks") or [])]
        if got != sorted(set(got)) or (heights and got and got[0] <= heights[-1]):
            raise ValueError("block_search answer out of height order")
        heights += got
        # The node refuses a page past the last one, so the walk stops as soon as the count is reached.
        if len(heights) >= total or not got:
            break
        if page >= max_pages:
            return heights, False
        page += 1
    if len(heights) != total:
        raise ValueError("block_search walk incomplete")
    return heights, True


def _count(rpc, query):
    """How many blocks a block_search matches."""
    return int(rpc("/block_search", {"query": f'"{query}"', "page": "1", "per_page": "1"})["total_count"])


def _unquotable(jid):
    """True for a job id an event query cannot carry. A quote or a backslash would end or escape the
    quoted value, and a control character has no place in it. Every other character is passed as is."""
    return any(c in (chr(39), chr(34), chr(92)) or ord(c) < 32 or ord(c) == 127 for c in jid)


# Indexed block events never change, so what the reader learned stays learned between refreshes: the jobs
# unwound by the no-seed loop up to `scanned_to`, and whether each of them was ever drawn. A refresh reads
# only the blocks above `scanned_to`. Both sets hold ids of jobs unwound on chain, each at most once, so
# they grow with the unwound jobs and never with the blocks. The cap only guards against an index that
# answers nonsense: past it, the cache starts over.
ORIGIN_CACHE_MAX_IDS = 200000


def new_origin_cache():
    return {"chain": None, "scanned_to": 0, "noseed": set(), "drawn": {}}


def read_unwind_origin(jobs, rpc, heights=None, cache=None, max_pages=100):
    """Where each unwound job without `+disputed` came from, read from the block events, or None.

    `rpc(path, params)` returns the `result` object of a CometBFT RPC call and raises on failure. The
    no-seed loop of audit_sampling.go::runOptimisticAudit emits `audit_draw_deferred` with an `unwound`
    count and no job id; every unwind emits `audit_retention_unwound` with its job id; a lottery deferral
    (audit_jury_floor.go::deferAuditForSmallJury) emits `audit_draw_deferred` with the job id. A job was
    never drawn when it was unwound in a no-seed block and no deferral event names it.

    `heights` = (height read before the listing, height read after it); None takes the node's latest for
    both, and a height that could not be read makes the answer None. The index check stops at the first, since every unwind at or below it is in the listing. The
    event search stops at the second, so an unwind later than the listing is never counted. `cache` =
    new_origin_cache(), kept by the caller across refreshes; None starts from nothing.
    -> {"noseed_unwound": [...], "drawn": [...], "unknown": [ids no event query can carry]}, or None
    when anything needed could not be read."""
    cache = new_origin_cache() if cache is None else cache
    try:
        unwound = [j for j in jobs if {"resolved", "unwound"} <= state_markers(j.get("state"))]
        undisputed = {j.get("id", "") for j in unwound if "disputed" not in state_markers(j.get("state"))}
        if not undisputed:
            return {"noseed_unwound": [], "drawn": [], "unknown": []}
        # An empty search answer means "none" only on a node that keeps and indexes the whole history.
        status = rpc("/status", {})
        sync = status["sync_info"]
        if str(sync["earliest_block_height"]) != "1":
            return None
        latest = int(sync["latest_block_height"])
        h_listed, h_top = heights if heights is not None else (latest, latest)
        if None in (h_listed, h_top) or not 0 < h_listed <= h_top <= latest:
            return None
        # What was learned on another chain, or above the top of this one (a relaunch), is dropped.
        chain = (status["node_info"]["network"], sync["earliest_block_hash"])
        if (cache["chain"] != chain or cache["scanned_to"] > h_top
                or len(cache["noseed"]) + len(cache["drawn"]) > ORIGIN_CACHE_MAX_IDS):
            cache.clear()
            cache.update(new_origin_cache(), chain=chain)
        # The index is written after the block: below the top it answers "none" for what it has not read.
        if _count(rpc, f"block.height = {h_top}") != 1:
            return None
        # Positive control: the index must see the unwinds the listing shows (one or more per block).
        seen = _count(rpc, f"audit_retention_unwound.job_id {Q_EXISTS} {Q_AND} block.height <= {h_listed}")
        if not 1 <= seen <= len(unwound):
            return None
        # The numeric condition lets the index skip the blocks where the no-seed loop unwound nothing. It
        # is used only when it gives the known answer on an attribute every unwind carries; otherwise
        # every no-seed block is read and its count filtered below.
        numeric = _count(rpc, f"audit_retention_unwound.age_blocks >= 0 AND block.height <= {h_listed}") == seen
        cond = "audit_draw_deferred.unwound > 0" if numeric else "audit_draw_deferred.unwound EXISTS"
        found, complete = _search_heights(
            rpc, f"{cond} AND block.height > {cache['scanned_to']} AND block.height <= {h_top}",
            max_pages=max_pages)
        for h in found:
            res = rpc("/block_results", {"height": str(h)})
            events = res.get("finalize_block_events") or []
            counts = [int(a["value"]) for e in events if e.get("type") == "audit_draw_deferred"
                      for a in (e.get("attributes") or []) if a.get("key") == "unwound"]
            if not counts:
                return None  # the search found this block but its events do not read as expected
            ids = [a["value"] for e in events if e.get("type") == "audit_retention_unwound"
                   for a in (e.get("attributes") or []) if a.get("key") == "job_id"]
            if sum(counts) > 0 and len(ids) < sum(counts):
                return None
            if sum(counts) > 0:
                cache["noseed"].update(ids)
            cache["scanned_to"] = h
        if not complete:
            return None  # the next call resumes above the last block read
        cache["scanned_to"] = h_top
        drawn, unknown = [], []
        for jid in sorted(cache["noseed"] & undisputed):
            if _unquotable(jid):
                unknown.append(jid)
                continue
            if jid not in cache["drawn"]:
                # Final once read: the job is already unwound, so no later deferral can name it.
                cache["drawn"][jid] = _count(
                    rpc, f"audit_draw_deferred.job_id='{jid}' AND block.height <= {h_top}") > 0
            if cache["drawn"][jid]:
                drawn.append(jid)
        return {"noseed_unwound": sorted(cache["noseed"]), "drawn": drawn, "unknown": unknown}
    except Exception as e:  # noqa: BLE001 -- any failure is "not read", published as null
        print(f"unwind origin unreadable: {type(e).__name__}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ partition

def _feed_rows(jobs):
    """The listing in the shape the feed reads (`id`, `state`), whatever case its source used."""
    rows = []
    for j in jobs or []:
        row = dict(j) if isinstance(j, dict) else {}
        row["id"] = str(_g(j, "id", "jobId", "job_id", default="") or "")
        row["state"] = _g(j, "state", "State", default="") or ""
        rows.append(row)
    return rows


def _deferred(d):
    """audit_deferred in the feed's shape, or None when the query did not answer. An absent counter is a
    zero the codec omitted: the query answered, so it is a measured zero."""
    if d is None:
        return None
    return {"job_ids": list(_g(d, "job_ids", "jobIds", default=[]) or []),
            "audits_opened": _int(_g(d, "audits_opened", "auditsOpened"))}


def classify_jobs(jobs, deferred, unwind_origin, moved):
    """The partition of the opened audits, computed by the function the feed publishes
    (audit_partition), so that every class the explorer publishes is counted here too, the undisputed
    unwound audits included. Adds `vindicated` (closed by quorum without a clawback) and
    `disputed_total`. `deferred` = audit_deferred read after the listing, in the feed's shape (see
    _deferred), or None. `moved` = chain_moved(), None when it is unknown: then no verdict is drawn.
    -> (partition, the disputed jobs that are resolved)."""
    rows = _feed_rows(jobs)
    part = audit_partition(rows, deferred, unwind_origin, moved)
    part["vindicated"] = part["disputed_total"] = 0
    resolved_jobs = []
    for j in rows:
        mk = state_markers(j["state"])
        cls = audit_class(mk)
        if cls not in DISPUTED_CLASSES:
            continue
        part["disputed_total"] += 1
        if cls != "audit_pending":
            resolved_jobs.append(j)
        if cls == "resolved_by_quorum" and "vindicated" in mk:
            part["vindicated"] += 1
    return part, resolved_jobs


def _shown(v):
    """A count as printed: "?" when it was not measured, never 0."""
    return "?" if v is None else str(v)


def partition_sum(part):
    """The sum of the partition classes, or None when one of them is not measured."""
    vals = [part[c] for c in DISPUTED_CLASSES] + [part["deferred_no_jury"], part["unwound_after_deferral"]]
    return None if any(v is None for v in vals) else sum(vals)


def operator_share_bps(job):
    """Largest operator share = audit_max_operator_votes / audit_voters, in bps (0..10000)."""
    voters = _int(_g(job, "audit_voters", "auditVoters"))
    mx = _int(_g(job, "audit_max_operator_votes", "auditMaxOperatorVotes"))
    if voters <= 0:
        return None  # no data -> undecidable (counts as not measured)
    return mx * 10000 // voters


# ------------------------------------------------------------------ evaluation

def evaluate(state, cfg):
    """Pure: takes the parsed on-chain state plus the config, returns the structured report. No I/O."""
    crit = []

    def add(key, kind, status, observed, need="", detail=""):
        crit.append({"key": key, "kind": kind, "status": status,
                     "observed": observed, "need": need, "detail": detail})

    # --- Preamble: the report must prove it measured, otherwise `NO-GO` -----------------------------
    missing = [q for q in ("seed_health", "audit_deferred", "held_summary", "params", "jobs")
               if state.get(q) is None]
    held = state.get("held_summary") or {}
    pockets = _g(held, "pockets_measured", "pocketsMeasured")
    preamble_ok = True
    if missing:
        preamble_ok = False
        add("preamble_queries", "preamble", "FAIL", f"missing: {','.join(missing)}",
            "every query answered", "without the data, a zero read would be indistinguishable from an absent field")
    if pockets is None or _int(pockets) < 3:
        preamble_ok = False
        add("pockets_measured", "preamble", "FAIL",
            "absent" if pockets is None else str(pockets), ">= 3",
            "sentinel absent/insufficient -> the binary does not prove it measured the zeros")
    else:
        add("pockets_measured", "preamble", "PASS", str(pockets), ">= 3",
            "sentinel present -> a zero pocket is a measured zero, not an omitted field")

    seed = state.get("seed_health") or {}
    params = (state.get("params") or {}).get("params") or state.get("params") or {}
    jobs = state.get("jobs") or []
    # The counter is read before the listing and again after it, as the feed reads it: a move in between
    # makes the partition not compared, never a divergence. A capture without the first read is unknown.
    after = _deferred(state.get("audit_deferred"))
    moved = chain_moved(_deferred(state.get("audit_deferred_before")), after)
    part, resolved_jobs = classify_jobs(jobs, after, state.get("unwind_origin"), moved)

    # --- Measurable on-chain (the report decides) --------------------------------------------------
    contributors = _int(_g(seed, "latest_contributors", "latestContributors"))
    add("contributors", "measurable", "PASS" if contributors >= cfg["min_contributors"] else "FAIL",
        str(contributors), f">= {cfg['min_contributors']}",
        "decentralized VRF seed: below 2, no adversarial audit is possible (the network serves without verifying)")

    rbq = part["resolved_by_quorum"]
    add("resolved_by_quorum", "measurable", "PASS" if rbq >= cfg["min_quorum_resolved"] else "FAIL",
        str(rbq), f">= {cfg['min_quorum_resolved']}", "audits actually closed by a quorum of seats")

    add("vindicated", "measurable", "PASS" if part["vindicated"] > 0 else "FAIL",
        str(part["vindicated"]), "> 0", "at least one honest primary cleared by quorum (the path works)")

    dnj = part["deferred_no_jury"]
    add("deferred_no_jury", "measurable", "PASS" if dnj is not None and dnj <= cfg["max_deferred"] else "FAIL",
        _shown(dnj), f"<= {cfg['max_deferred']}", "a backlog of jury-less audits is a network that does not "
        "resolve (strict; not measured without the audit-deferred query)")

    if resolved_jobs:
        dv_min = min(_int(_g(j, "audit_distinct_voters", "auditDistinctVoters")) for j in resolved_jobs)
        add("distinct_voters_min", "measurable", "PASS" if dv_min >= cfg["distinct_voters_min"] else "FAIL",
            str(dv_min), f">= {cfg['distinct_voters_min']}", "real plurality of voters (not a quorum in form only)")
    else:
        add("distinct_voters_min", "measurable", "FAIL", "n/a (0 resolved job)",
            f">= {cfg['distinct_voters_min']}", "no resolved audit -> plurality not measurable")

    shares = [s for s in (operator_share_bps(j) for j in resolved_jobs) if s is not None]
    if shares:
        mshare = max(shares)
        add("max_operator_share_bps", "measurable", "PASS" if mshare < 5000 else "FAIL",
            str(mshare), "< 5000", "no single operator held a majority of the votes in an audit")
    else:
        add("max_operator_share_bps", "measurable", "FAIL", "n/a (0 resolved job with voters)",
            "< 5000", "no resolved audit with voters -> majority share not measurable")

    # --- jobs_unresolved: the third member of the triplet ------------------------------------------
    # Readiness requires a 0/0/0 triplet, and `jobs_unresolved = 0` is part of it. Watching only
    # `deferred_no_jury` (audits without a jury) lets audits with a jury that never conclude slip
    # through: the gate would report "every measurable OK" on a chain where 20 audits out of 38 stayed
    # open, with growing held funds and locked jurors. An open audit that never closes is literally
    # "the network has not finished verifying" — the very thing C3 must exclude.
    unres = part["audit_pending"]
    add("jobs_unresolved", "measurable", "PASS" if unres <= cfg.get("max_unresolved", 0) else "FAIL",
        str(unres), f"<= {cfg.get('max_unresolved', 0)}",
        "open audits that never close (jury seated but under the bar) — third member of the triplet; "
        "distinct from deferred_no_jury, which only counts audits without a jury")

    # --- Alarm (never blocking) -------------------------------------------------------------------
    # `miner_prune_blocks` is dormant by default (0 = the chain falls back on the compiled constant in
    # miner_vitality.go). A `prune > 0 and ...` test can therefore never fire at 0, while displaying
    # "OK" next to "expected <= 0" — a dead alarm presenting itself as a healthy one (a guard that
    # cannot bite plus a label that lies). Apply the same fallback as the chain and name the source of
    # the threshold.
    PRUNE_COMPILED = 604800  # miner_vitality.go: minerPruneBlocks (~7 days at 1 block/s)
    prune_param = _int(_g(params, "miner_prune_blocks", "minerPruneBlocks"))
    prune_eff = prune_param if prune_param > 0 else PRUNE_COMPILED
    src_prune = "governed" if prune_param > 0 else f"compiled constant ({PRUNE_COMPILED})"
    oldest_held = _int(_g(held, "oldest_age_blocks", "oldestAgeBlocks"))
    oldest_lock = _int(_g(held, "oldest_lock_age_blocks", "oldestLockAgeBlocks"))
    warn = oldest_held > prune_eff or oldest_lock > prune_eff
    add("held_lock_age", "warning", "WARN" if warn else "OK",
        f"held={oldest_held} lock={oldest_lock} threshold={prune_eff} [{src_prune}]",
        f"<= {prune_eff} (alarm, not a block)",
        "held funds older than the eviction window raise an alarm to clear (it does not block the "
        "gate). The threshold is never 0: at 0 the alarm could never fire. "
        "Both ages are lower bounds during the first blocks after a restart from a fresh genesis, "
        "and the error is on the quiet side: ages travel through genesis as elapsed blocks and are "
        "re-anchored as `since = height - elapsed`, clamped at 0, so an age never exceeds the chain "
        "height. Measured: a fee exported at age 60 and read 29 blocks later shows 30 at height 1, "
        "59 at height 30, and the true 89 only at height 200. An OK here proves nothing while the "
        "height is below the ages carried over; compare the age with the height before trusting the "
        "silence. A uint64 anchor cannot express '60 blocks old' at height 1, so the loss lives in "
        "the state shape (ADR-045, entry 13: accepted and declared rather than migrated).")

    # --- Not measurable -> named, refutable attestation, never a pass derived from a counter --------
    for key, (ok, shown), detail in (
        ("distinct_operators_ge_2", _attest_operators(cfg.get("attest_operators")),
         "on-chain identities are not distinct human operators: name them (--attest-operators \"opA=key,opB=key\")"),
        ("judge_models_ge_2_incl_moe", _attest_models(cfg.get("attest_models")),
         "a declared model_id is not a physically distinct model; >=2 models including >=1 MoE, to be named"),
        ("honest_miners_ge_100", _attest_honest(cfg.get("attest_honest")),
         "registered is not honest; >=100 honest miners in the run, to be declared with a verifiable count"),
    ):
        add(key, "attestation", "ATTESTED" if ok else NEEDS_ATTEST, shown,
            "named operator attestation (refutable)", detail)

    # --- Verdict (the script never prints `GO`) -----------------------------------------------------
    measurable = [c for c in crit if c["kind"] == "measurable"]
    attest = [c for c in crit if c["kind"] == "attestation"]
    meas_fail = [c for c in measurable if c["status"] == "FAIL"]

    if not preamble_ok:
        verdict = NO_GO
        why = "Preamble failed: the report cannot prove that it measured (see preamble)."
    elif meas_fail:
        verdict = NO_GO
        why = "measurable criteria failed: " + ", ".join(c["key"] for c in meas_fail)
    elif any(c["status"] == NEEDS_ATTEST for c in attest):
        verdict = NEEDS_ATTEST
        why = "measurables OK; named attestation missing/insufficient: " + \
              ", ".join(c["key"] for c in attest if c["status"] == NEEDS_ATTEST)
    else:
        verdict = ATTESTED
        why = ("every measurable PASSes and the 3 named attestations are provided — only the "
               "announcement decision remains, and it is a human one. The script does not pronounce `GO`.")

    return {
        "verdict": verdict, "why": why,
        "partition": part, "audits_opened": part["audits_opened"],
        "partition_sum_vs_opened": {
            "sum_classes": partition_sum(part),
            "audits_opened": part["audits_opened"],
            # The feed's verdict, null when one side is not measured (the query did not answer, or the
            # origin of an undisputed unwind is unread): nothing to compare is not a divergence.
            "partition_ok": part["partition_ok"],
            "partition_bounds_ok": part["partition_bounds_ok"],
            "reason": part["partition_unmeasured_reason"],
            "chain_moved": moved,
            "comparable": part["partition_ok"] is not None},
        "criteria": crit,
        "config": cfg,
        # Provenance is part of the verdict: a `NO-GO` read through a stale client does not say the same
        # thing as a `NO-GO` read through the node's own gateway. Never return one for the other.
        "source": state.get("_source", "unknown"),
    }


# ------------------------------------------------------------------ I/O (dendrad)

def _rpc(node, args, timeout=60):
    try:
        r = subprocess.run(["dendrad", "query", "jobs", *args, "--node", node, "-o", "json"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def rpc_all(node, cmd, field, timeout=180, max_pages=200):
    """Complete list, page by page. Returns None if any page fails (fail closed).

    Without paging, `dendrad query jobs list-job` stops at 100 entries: `query.CollectionPaginate`
    receives a PageRequest with Limit=0 and the SDK applies `DefaultLimit = 100`
    (cosmos-sdk/types/query/pagination.go). A C3 run over 150 jobs would show only 100 — silently, and
    always in the reassuring direction (fewer jobs read = fewer disputes, fewer deferrals, fewer false
    slashes). A truncation that can only produce good news is the most dangerous form of the
    measurement that cannot contradict.
    """
    out, key, pages = [], None, 0
    while pages < max_pages:
        args = [cmd, "--page-limit", "500"] + (["--page-key", key] if key else [])
        d = _rpc(node, args, timeout=timeout)
        if d is None:
            return None
        out.extend(_g(d, field, field.capitalize(), default=[]) or [])
        key = _g(d.get("pagination") or {}, "next_key", "nextKey")
        pages += 1
        if not key:
            return out
    return None  # page bound reached: do not claim to have read everything


def gather(node, rpc=None):
    """Query the chain. A failing query stays None -> the preamble returns `NO-GO` (fail closed). `rpc`
    overrides the CometBFT RPC derived from `node`."""
    seed = _rpc(node, ["committee-seed-health"])
    before, jobs, deferred, origin = read_partition_inputs(
        lambda: _rpc(node, ["audit-deferred"]), lambda: rpc_all(node, "list-job", "job"), rpc or rpc_url(node))
    held = _rpc(node, ["held-summary"])
    params = _rpc(node, ["params"])
    miners = rpc_all(node, "list-miner", "miner", timeout=60)
    return {"seed_health": seed, "audit_deferred_before": before, "audit_deferred": deferred,
            "held_summary": held, "params": params, "jobs": jobs, "miners": miners,
            "unwind_origin": origin, "_source": "cli"}


def rpc_url(node):
    """The CometBFT RPC of a `--node` address (tcp://HOST:26657 -> http://HOST:26657)."""
    return node.replace("tcp://", "http://", 1) if node.startswith("tcp://") else node


def _latest_height(rpc):
    """The node's latest height through its CometBFT RPC, or None when it cannot be read."""
    if not rpc:
        return None
    try:
        return int(rpc_reader(rpc)("/status", {})["sync_info"]["latest_block_height"])
    except Exception:  # noqa: BLE001 -- unread, and read_unwind_origin answers None for it
        return None


def read_partition_inputs(read_deferred, read_jobs, rpc):
    """The counter, the listing and the block events, read in the feed's order
    (the_proof.py::_read_inputs): the counter before the listing and again after the event search, and
    the event search bounded by the heights read around the listing. When the counter moved in between,
    the reads are made once more; a second move is left for evaluate() to report as not compared.
    -> (audit_deferred before, listing, audit_deferred after, unwind origin)."""
    for _attempt in range(2):
        before = read_deferred()
        h_listed = _latest_height(rpc)
        jobs = read_jobs()
        h_top = _latest_height(rpc)
        origin = _origin(jobs, rpc, (h_listed, h_top))
        after = read_deferred()
        if chain_moved(_deferred(before), _deferred(after)) is not True:
            break
    return before, jobs, after, origin


def _origin(jobs, rpc, heights):
    """Where the undisputed unwound audits came from, read from the block events by the feed's reader.
    None when the listing or the events are unread, or when no RPC address is known."""
    if jobs is None or not rpc:
        return None
    return read_unwind_origin(_feed_rows(jobs), rpc_reader(rpc), heights=heights)


# --- Alternate source: the node's own REST gateway (:1317) -----------------------------------------
# A workstation's `dendrad` CLI decodes responses with its protos and exposes only its subcommands. A
# client lagging behind the node therefore produces exactly the picture of a node that lags: subcommands
# reported "absent", recent fields read as 0 — and the chain gets blamed for a workstation defect. REST
# is served by the node: it is a witness whose version is the chain's, not the workstation's. Same case
# convention (snake_case), same uint64-as-string encoding.

def _rest(base, path, timeout=20):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/dendra/jobs/v1/{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _rest_all(base, path, field, max_pages=400):
    out, key, pages = [], None, 0
    while pages < max_pages:
        q = f"{path}?pagination.limit=500" + (f"&pagination.key={key}" if key else "")
        d = _rest(base, q, timeout=120)
        if d is None:
            return None
        out.extend(_g(d, field, field.capitalize(), default=[]) or [])
        key = _g(d.get("pagination") or {}, "next_key", "nextKey")
        pages += 1
        if not key:
            return out
    return None


def gather_rest(base, rpc=None):
    """Same state, read through the node's REST gateway — independent of the local CLI version. `rpc` is
    the node's CometBFT RPC, for the block events; without it the origin of an undisputed unwind is
    unread and the partition is not compared."""
    before, jobs, deferred, origin = read_partition_inputs(
        lambda: _rest(base, "audit_deferred"), lambda: _rest_all(base, "job", "job"), rpc)
    return {"seed_health": _rest(base, "committee_seed_health"),
            "audit_deferred_before": before,
            "audit_deferred": deferred,
            "held_summary": _rest(base, "held_summary"),
            "params": _rest(base, "params"),
            "jobs": jobs,
            "miners": _rest_all(base, "miner", "miner"),
            "unwind_origin": origin,
            "_source": f"rest {base}"}


# ------------------------------------------------------------------ rendering

def render(report):
    C = report["criteria"]
    lines = []
    lines.append("=" * 78)
    lines.append("  C3 report — announcement decision support (on-chain read; never pronounces `GO`)")
    lines.append("=" * 78)
    for kind, title in (("preamble", "preamble (proof of measurement)"),
                        ("measurable", "measurable on-chain (the report decides)"),
                        ("warning", "alarm (never blocking)"),
                        ("attestation", "not measurable — named operator attestation required")):
        rows = [c for c in C if c["kind"] == kind]
        if not rows:
            continue
        lines.append("")
        lines.append(f"-- {title}")
        for c in rows:
            mark = {"PASS": "[x] PASS", "FAIL": "[!] FAIL", "OK": "[.] OK", "WARN": "[!] WARN",
                    "ATTESTED": "[x] ATTESTED", NEEDS_ATTEST: "[?] to attest"}[c["status"]]
            lines.append(f"   {mark:<14} {c['key']}")
            lines.append(f"       observed={c['observed']}  expected={c['need']}")
            if c["detail"]:
                lines.append(f"       -> {c['detail']}")
    p = report["partition"]
    lines.append("")
    lines.append("-- partition of the opened audits (the feed's classes, c3_gate.py::audit_partition; "
                 "checked against audits_opened)")
    lines.append(f"   pending={p['audit_pending']} quorum={p['resolved_by_quorum']} (vindicated={p['vindicated']}) "
                 f"no_quorum={p['resolved_no_quorum']} adjudication={p['resolved_by_adjudication']} "
                 f"unwound={p['resolved_unwound']} unmarked={p['resolved_unmarked']}")
    lines.append(f"   unwound_after_deferral={_shown(p['unwound_after_deferral'])} "
                 f"deferred={_shown(p['deferred_no_jury'])} | outside the partition: "
                 f"unwound_never_drawn={_shown(p['unwound_never_drawn'])} | disputed={p['disputed_total']}")
    s = report["partition_sum_vs_opened"]
    if s["partition_ok"] is None:
        lines.append(f"   sum_classes={_shown(s['sum_classes'])}  audits_opened={_shown(s['audits_opened'])}  "
                     f"not compared ({s['reason']}): nothing to compare, not a divergence")
    else:
        flag = "" if s["partition_ok"] else "  ! divergence (investigate)"
        lines.append(f"   sum_classes={s['sum_classes']}  audits_opened={s['audits_opened']}{flag}")
    lines.append("")
    lines.append("-- Not measured by this gate (and part of the triplet)")
    lines.append("   `hard_slash_at_quorum_rate` and the false-slash rate on honest miners require")
    lines.append("   knowing who is declared a cheater: a legitimate slash of a cheater is not a false")
    lines.append("   slash. This gate reads the whole chain history without roles, so it cannot decide")
    lines.append("   them. They are produced by `c3_run_measure.py --cheaters \"...\"` over a dated run.")
    lines.append("")
    lines.append("-" * 78)
    lines.append(f"  source  : {report.get('source', 'unknown')}")
    lines.append(f"  verdict : {report['verdict']}")
    lines.append(f"  reason  : {report['why']}")
    lines.append("  (the script never pronounces `GO` — ceiling = ATTESTED_PENDING_SIGNOFF; the C3 `GO` is")
    lines.append("   a human governance decision, never an output of this script.)")
    lines.append("-" * 78)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Honest C3 report — never pronounces `GO`.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--node", help="tcp://HOST:26657 — query the chain through the local dendrad CLI")
    src.add_argument("--rest", metavar="http://HOST:1317",
                     help="the node's REST gateway — use it when the local CLI lags behind the chain "
                          "(its subcommands and protos are the workstation's, not the chain's)")
    src.add_argument("--from-json", help="evaluate a captured state (offline / test)")
    ap.add_argument("--to-json", help="with --node: write the gathered state and exit (no verdict)")
    ap.add_argument("--rpc", metavar="http://HOST:26657", default="",
                    help="the node's CometBFT RPC, for the block events that tell where an undisputed unwind "
                         "came from (default: derived from --node; none with --rest)")
    ap.add_argument("--min-quorum-resolved", type=int, default=1,
                    help="floor for resolved_by_quorum (default 1; calibrate on real data)")
    ap.add_argument("--max-deferred", type=int, default=0, help="tolerated deferred_no_jury (strict, default 0)")
    ap.add_argument("--max-unresolved", type=int, default=0,
                    help="tolerated jobs_unresolved (third member of the triplet; strict, default 0)")
    ap.add_argument("--min-contributors", type=int, default=2, help="floor for contributors (default 2)")
    ap.add_argument("--distinct-voters-min", type=int, default=3, help="floor for distinct_voters (default 3)")
    ap.add_argument("--attest-operators", metavar='"opA=key,opB=key"', default="",
                    help="attest, by naming them, >=2 genuinely distinct operators (refutable afterwards)")
    ap.add_argument("--attest-models", metavar='"m1,m2(MoE)"', default="",
                    help="attest >=2 named judge models, including >=1 MoE (markers a3b/30b)")
    ap.add_argument("--attest-honest", metavar='"<n> honest, run <date>"', default="",
                    help="declare the number (>=100) of honest miners in the run")
    ap.add_argument("--json", action="store_true", help="raw JSON output instead of the text rendering")
    a = ap.parse_args(argv)

    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = gather_rest(a.rest, a.rpc or None) if a.rest else gather(a.node, a.rpc or None)
        if a.to_json:
            with open(a.to_json, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            print(f"state written -> {a.to_json}")
            return 0  # a capture, not a gate verdict

    cfg = {"min_quorum_resolved": a.min_quorum_resolved, "max_deferred": a.max_deferred,
           "max_unresolved": a.max_unresolved,
           "min_contributors": a.min_contributors, "distinct_voters_min": a.distinct_voters_min,
           "attest_operators": a.attest_operators, "attest_models": a.attest_models,
           "attest_honest": a.attest_honest}
    report = evaluate(state, cfg)
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json else render(report))
    return EXIT[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
