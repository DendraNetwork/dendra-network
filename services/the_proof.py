#!/usr/bin/env python3
"""the_proof.py, the Proof feed: a read-only HTTP facade over the real on-chain state, so that the
public site can show a feed nobody staged (audited jobs, slashes, VRF health).

No writes, no secrets, no content: the chain only sees metadata and hashes, and this facade only
exposes counters and public states that anyone can already query over RPC.

  GET /proof   -> JSON (see build_proof), the last payload a background thread published; a request never
                  starts a build. Builds are spaced from the end of one to the start of the next by
                  max(DENDRA_PROOF_REFRESH_S (default 15 s), 4 x the last build's duration).
                  `chain.read_at` is the epoch second of the status read the block age is measured at,
                  `refresh.build_s` the seconds the last refresh took, `refresh.gap_s` the gap in force
                  after it and `refresh.slow_build` true when it took longer than the refresh interval
                  (see `publish`). A second small thread reads the node status every
                  DENDRA_PROOF_CHAIN_REFRESH_S (default 30 s) and replaces only the `chain` block of the
                  published payload (`publish_chain`), so the block age stays fresh between heavy builds.
                  A payload whose chain read is STALE_BLOCK_S old or more is served without its verdicts
                  about now, with `chain.verdict_withdrawn` saying why (`serve_body`).
                  `audit_outcomes` groups the opened audits by how they ended and publishes its counts only when they
                  add up to `jobs.audits_opened` (`audit_outcomes`). The by-miner, by-judge and by-operator parts of
                  `transparency` carry a Nakamoto coefficient in the unit they count (`_concentration`). A height that
                  was not read is null, never 0 (`_known_height`). The commit listing is read by increment, so a build
                  reads the rows the chain gained since the last one (`read_commits_by_increment`).
  GET /health  -> {"status":"ok"}

Run: python3 the_proof.py   (binds 127.0.0.1:8090 by default -- put behind a reverse proxy for
public exposure; DENDRA_PROOF_HOST/PORT/CORS to adjust). Offline selftest: python3 the_proof.py --selftest
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure core (selftest without chain or network): builds the payload from already-fetched snapshots.
# State markers = on-chain predicates (job_state.go): paid/settled, disputed, resolved, clawed.
# ---------------------------------------------------------------------------


# Past this, a last block stops being proof of life. The value is an order of magnitude, not a derivation:
# no consensus parameter fixes the interval between blocks (it emerges from `timeout_commit` and from
# propagation), so no number on the chain turns into this threshold. It is published in the payload, so a
# reader can apply their own and see which one produced the verdict.
STALE_BLOCK_S_MIN, STALE_BLOCK_S_MAX = 60, 86400


def read_stale_s(raw):
    """The staleness threshold in whole seconds from the text of DENDRA_PROOF_STALE_S, or SystemExit with a
    message. A value is refused, never replaced by a default: STALE_BLOCK_S_MIN <= v <= STALE_BLOCK_S_MAX.
    Below the minimum a live chain would read as stopped between two blocks, above the maximum a stopped one
    would pass as live for a day."""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        v = None
    if v is None or not STALE_BLOCK_S_MIN <= v <= STALE_BLOCK_S_MAX:
        raise SystemExit(f"[the-proof] DENDRA_PROOF_STALE_S={raw!r} is refused: it must be a whole number of "
                         f"seconds with {STALE_BLOCK_S_MIN} <= value <= {STALE_BLOCK_S_MAX}")
    return v


STALE_BLOCK_S = read_stale_s(os.environ.get("DENDRA_PROOF_STALE_S", "300"))


# The partition core lives in c3_gate.py, which needs the standard library only: the readiness report
# runs alone on a host, and the feed imports it rather than the other way round.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from c3_gate import (DISPUTED_CLASSES, ORIGIN_CACHE_MAX_IDS, _unquotable, audit_class,  # noqa: E402
                     audit_partition, chain_moved, new_origin_cache, read_unwind_origin, rpc_reader,
                     state_markers)


def origin_partition(jobs, generator, gateway, height):
    """The jobs by who asked for them, read from the `client` of each job in the listing.

    `generator` is the address of the Final Testnet Season's request generator (the `generator` field of
    the season service's status) and `gateway` the address of the public chat gateway; None when it was not
    read. Three classes that must add up to the total: the generator's, the gateway's and every other
    client's. Each count is taken by its own test on the listing, so the sum can fail: an address shared by
    the two sources, or a listing that changed shape, makes `partition_ok` false, never a quiet total.
    A class whose address was not read is null (not measured), and so is `other_clients`: without both
    addresses nothing says what is left. `read_at_height` is the height of the reading the listing belongs to."""
    total = len(jobs)

    def addr(v):
        return v if isinstance(v, str) and v else None
    gen, gw = addr(generator), addr(gateway)
    out = {"read_at_height": _known_height(height), "total": total, "season_generator": None, "chat_gateway": None,
           "other_clients": None, "partition_ok": None, "unmeasured_reason": None}
    if any("client" not in j for j in jobs):
        out["unmeasured_reason"] = "a job row carries no client field"
        return out
    clients = [j["client"] for j in jobs]
    why = []
    if gen is None:
        why.append("the season generator's address was not read")
    else:
        out["season_generator"] = sum(1 for c in clients if c == gen)
    if gw is None:
        why.append("the chat gateway's address is not set")
    else:
        out["chat_gateway"] = sum(1 for c in clients if c == gw)
    if gen is not None and gw is not None:
        out["other_clients"] = sum(1 for c in clients if c != gen and c != gw)
        out["partition_ok"] = out["season_generator"] + out["chat_gateway"] + out["other_clients"] == total
    out["unmeasured_reason"] = "; ".join(why) if why else None
    return out


VERDICT_SEP = "__verdict__"
TOP_ROWS = 10


# A set of holders counts toward the Nakamoto coefficient when it holds more than this share of the counter's
# total (basis points): a strict majority.
NAKAMOTO_OVER_BPS = 5000


def nakamoto(counter, over_bps):
    """The smallest number of holders whose counts together exceed `over_bps` of the counter's total, or None
    when nothing was counted. Holders are the counter's keys: the caller says what a key is."""
    whole = sum(counter.values())
    if whole <= 0:
        return None
    held = 0
    for n, v in enumerate(sorted(counter.values(), reverse=True), 1):
        held += v
        if held * 10000 > over_bps * whole:
            return n
    return None


def _concentration(counter, top=TOP_ROWS, unit=None, grouped=None):
    """{total, distinct, top_share_bps, top: [{id, count, share_bps}]} of a counter, largest first. Shares are
    in basis points of the counter's own total; with nothing counted there is no share (null), not 0.

    With a `unit` (what one key of the counter is, "operator addresses" or "miner ids") the result also
    carries the Nakamoto coefficient (`nakamoto`, over `nakamoto_over_bps`) in that unit. An address is not a
    person: one entity can hold several, so the figure is an upper bound on how many parties it takes.
    `grouped` is a key that stands for several holders at once; when it is among the holders the figure needs,
    the number of holders is unknown, so `nakamoto` is null and `nakamoto_reason` says why."""
    whole = sum(counter.values())
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    out = {"total": whole, "distinct": len(ranked),
           "top_share_bps": (ranked[0][1] * 10000 // whole) if whole else None,
           "top": [{"id": k, "count": v, "share_bps": v * 10000 // whole} for k, v in ranked[:top]]}
    if unit is not None:
        k = nakamoto(counter, NAKAMOTO_OVER_BPS)
        reason = None
        if k is not None and grouped is not None and grouped in [key for key, _ in ranked[:k]]:
            k, reason = None, "the holders needed include several the registry no longer lists, counted as one"
        out.update({"nakamoto": k, "nakamoto_unit": unit, "nakamoto_over_bps": NAKAMOTO_OVER_BPS,
                    "nakamoto_reason": reason})
    return out


def _known_height(h):
    """`h` as an int, or None when the height was not read. `client.head()` answers 0 for a status it
    could not read, and no block a listing belongs to has height 0, so 0 and None both mean unknown. A
    published 0 would claim the genesis block carried the reading."""
    if h is None or isinstance(h, bool):
        return None
    n = int(h)
    return n if n > 0 else None


def transparency(jobs, miners, commits, audit_timing, height, top=TOP_ROWS):
    """How the work and the verdicts are spread, each part read from one listing of the chain at `height`.

    `miners` = [{id, operator}] (list-miner), `commits` = [{key, model_id}] (list-commit), `audit_timing` =
    {audits, timed, median_blocks}. Each is None when it could not be read, and its parts are then null: a
    part is never computed from a list that was not read. Shares are basis points of that part's own total.
      jobs_by_miner / jobs_by_operator   the jobs a miner served; the operator is the registry's, and a miner
                                         the registry no longer lists is counted under its own bucket
      verdicts_by_judge / ..._operator   the verdict commits (`<job>__verdict__<miner>`), by the miner that
                                         posted them
      requests_by_model                  the model the miner named in the commit of each job
      dispute_to_verdict                 the median number of blocks from the height an audit was opened to the
                                         height that resolved it, over the audits both heights were read for
    The unit of the last one is blocks: the interval between blocks emerges from the network and varies.

    The by-miner, by-judge and by-operator parts also carry the Nakamoto coefficient (`_concentration`),
    named in the unit of what it counts: `miner ids` for the first two, `operator addresses` for the others."""
    out = {"read_at_height": _known_height(height), "jobs_by_miner": None, "jobs_by_operator": None,
           "verdicts_by_judge": None, "verdicts_by_operator": None, "requests_by_model": None,
           "dispute_to_verdict": None}
    op_of = None if miners is None else {m["id"]: m["operator"] for m in miners}
    unreg = "(not in the registry)"

    def by_operator(counter):
        if op_of is None:
            return None
        ops = {}
        for mid, n in counter.items():
            op = op_of.get(mid) or unreg
            ops[op] = ops.get(op, 0) + n
        return _concentration(ops, top, "operator addresses", grouped=unreg)

    if all("miner_id" in j for j in jobs):
        served = {}
        for j in jobs:
            if j["miner_id"]:
                served[j["miner_id"]] = served.get(j["miner_id"], 0) + 1
        out["jobs_by_miner"] = _concentration(served, top, "miner ids")
        out["jobs_by_operator"] = by_operator(served)
    if commits is not None:
        votes, models = {}, {}
        ids = {j.get("id") for j in jobs}
        for c in commits:
            key = c["key"]
            if VERDICT_SEP in key:
                mid = key.partition(VERDICT_SEP)[2]
                votes[mid] = votes.get(mid, 0) + 1
            elif key in ids:
                m = c["model_id"] or "(none named)"
                models[m] = models.get(m, 0) + 1
        out["verdicts_by_judge"] = _concentration(votes, top, "miner ids")
        out["verdicts_by_operator"] = by_operator(votes)
        out["requests_by_model"] = _concentration(models, top)
    if audit_timing is not None:
        out["dispute_to_verdict"] = {"audits": audit_timing["audits"], "timed": audit_timing["timed"],
                                     "median_blocks": audit_timing["median_blocks"], "unit": "blocks"}
    return out


def chain_block(height, block_epoch, read_at):
    """The `chain` section of the payload, from one status read made at `read_at` (epoch seconds). The age
    and the verdict follow the rules written in `build_proof` above its call. The heavy build and the light
    chain reader (`publish_chain`) both make it, so the two cannot disagree on what stale means."""
    _read = int(read_at)
    _age = None if block_epoch is None else _read - int(block_epoch)
    if _age is not None and _age + STALE_BLOCK_S <= 0:
        _age = None
    return {
        "height": _known_height(height),
        "last_block_epoch": (None if block_epoch is None else int(block_epoch)),
        "last_block_age_s": _age,
        "stale": (None if _age is None else _age >= STALE_BLOCK_S),
        "stale_threshold_s": STALE_BLOCK_S,
        "read_at": _read,
    }


# The eight exclusive classes of `c3_gate.audit_partition`, grouped by how the audit ended. Each class is in
# exactly one group, so the groups add up to what the classes add up to (`audit_outcomes` checks that sum
# against the chain's own counter of opened audits).
#   verified              a jury concluded it: by quorum, or by a re-hearing (either verdict)
#   refunded_no_verdict   the client was refunded and no jury ruled: unwound after a dispute, or after the
#                         draw found the jury below its floor
#   no_quorum             the audit expired with too few votes
#   in_progress           still open: waiting for its jury, or drawn and waiting for a large enough one
#   unknown               resolved without any marker that says how
OUTCOME_CLASSES = {
    "verified": ("resolved_by_quorum", "resolved_by_adjudication"),
    "refunded_no_verdict": ("resolved_unwound", "unwound_after_deferral"),
    "no_quorum": ("resolved_no_quorum",),
    "in_progress": ("audit_pending", "deferred_no_jury"),
    "unknown": ("resolved_unmarked",),
}


def audit_outcomes(part, height):
    """The opened audits grouped by outcome (`OUTCOME_CLASSES`), from `part`, the answer of `audit_partition`.

    The five counts are published only when they add up to `audits_opened`, the chain's own counter, which
    is read by another query than the job listing: otherwise every count is null and `unmeasured_reason`
    says why (the counter or an origin unread, the chain moved during the read, or a sum that differs). `sum`
    is what the classes add up to, null while a class is unknown; `sum_ok` is true, false, or null when the
    comparison could not be made. A null is "not known", never zero. `read_at_height` is the height of the
    reading the listing belongs to."""
    out = {"read_at_height": _known_height(height), "audits_opened": part["audits_opened"],
           **dict.fromkeys(OUTCOME_CLASSES), "sum": None, "sum_ok": None, "unmeasured_reason": None}
    if part["audits_opened"] is None or part["partition_unmeasured_reason"] is not None:
        out["unmeasured_reason"] = part["partition_unmeasured_reason"] or "audits_opened unreadable"
        return out
    counts = {}
    for name, classes in OUTCOME_CLASSES.items():
        vals = [part[c] for c in classes]
        if any(v is None for v in vals):
            out["unmeasured_reason"] = "an audit class is not measured"
            return out
        counts[name] = sum(vals)
    out["sum"] = sum(counts.values())
    out["sum_ok"] = out["sum"] == part["audits_opened"]
    if out["sum_ok"]:
        out.update(counts)
    else:
        out["unmeasured_reason"] = "the classes do not add up to audits_opened"
    return out


def build_proof(jobs, pools, seed_health, height, deferred=None, held=None,
                prune_window_blocks=None, max_recent=10, block_epoch=None, now=None, unwind_origin=None,
                chain_moved=False, season_generator=None, gateway_address=None, miners=None, commits=None,
                audit_timing=None, read_at=None):
    """The Proof payload (pure). `jobs` = list_jobs_full() (id/state/miner_id/fee/audit_*/slashes).
    `block_epoch` and `read_at` come from the same status read: the block's time and the epoch second
    the read was made. None for `read_at` means the status was read at `now`.
    `deferred` = client.audit_deferred(): {"job_ids": [...], "audits_opened": N}, measured
    on-chain. `unwind_origin` = read_unwind_origin(): {"noseed_unwound": [ids unwound in a block where
    the no-seed loop unwound], "drawn": [ids carrying an audit_draw_deferred event], "unknown": [ids no
    event query can carry]}, read from the block events. `chain_moved` = chain_moved(). `held` =
    client.held_summary(): {"held_total", "held_count", "oldest_age_blocks",
    "bond_escrowed_*", ...}. `prune_window_blocks` = the age-warning threshold
    (dc.prune_window_blocks()). Each of them is None when not measurable on the running binary, and
    None is not zero.

    `jobs` is the one input that has no null form here: every count below is drawn from it, so a
    listing that could not be read would publish a network with no job, no audit and no slash. It
    raises on None; the caller keeps the last snapshot it published (see `_refresh`)."""
    if jobs is None:
        raise ValueError("jobs not measured (list-job unreadable): no payload is built from an unread listing")
    total = len(jobs)
    settled = audited = vindicated = clawed = 0
    distinct_min = None  # Min of distinct operators over the audits closed by quorum
    max_op_share_bps = None  # Max single-operator share (max_votes/voters, in bps) over those same audits
    slash_events = []
    recent_audits = []
    for j in jobs:
        st = j.get("state", "") or ""
        # The state is the chain's own marker list joined by '+': classes test markers, never substrings
        # ("noquorum" contains "quorum").
        mk = state_markers(st)
        cls = audit_class(mk)
        if ("paid" in mk) or ("settled" in mk):
            settled += 1
        if cls in DISPUTED_CLASSES:
            audited += 1
            if cls == "resolved_by_quorum":
                dv = int(j.get("audit_distinct_voters", 0) or 0)
                distinct_min = dv if distinct_min is None else min(distinct_min, dv)
                # The gate reads the share of the largest operator (below 50%), not a head count. The
                # worst case of the window is exposed, the maximum of those shares, since an average
                # would hide one captured audit among ten healthy ones.
                voters = int(j.get("audit_voters", 0) or 0)
                mov = int(j.get("audit_max_operator_votes", 0) or 0)
                if voters > 0:
                    share = mov * 10000 // voters
                    max_op_share_bps = share if max_op_share_bps is None else max(max_op_share_bps, share)
            if cls != "audit_pending":
                # `vindicated` keeps its published meaning (resolved without `clawed`, unwound included),
                # since the explorer labels it "closed without a clawback".
                if "clawed" in mk:
                    clawed += 1
                else:
                    vindicated += 1
            recent_audits.append({"job_id": j.get("id", ""), "state": st, "fee": int(j.get("fee", 0) or 0),
                                  "distinct_voters": int(j.get("audit_distinct_voters", 0) or 0)})
        for s in j.get("slashes", []) or []:
            if int(s.get("amount", 0) or 0) > 0:
                slash_events.append({"job_id": j.get("id", ""), "miner_id": s.get("miner_id", ""),
                                     "amount": int(s.get("amount", 0))})
    slashed_total_u = sum(e["amount"] for e in slash_events)
    part = audit_partition(jobs, deferred, unwind_origin, chain_moved)
    # The `held` section, precomputed. Pockets 2 and 3 preserve the client's None: an absent
    # `pockets_measured` sentinel means a binary older than that pocket, whose zeros are not
    # measurements but artifacts of the codec omitting zeros. A zero here is a measured zero.
    if held is None:
        held_out = None
    else:
        _lockage = (None if held.get("oldest_lock_age_blocks") is None
                    else int(held.get("oldest_lock_age_blocks") or 0))
        held_out = {
            "total_udndr": int(held.get("held_total") or 0),
            "count": int(held.get("held_count") or 0),
            "oldest_age_blocks": int(held.get("oldest_age_blocks") or 0),
            "bond_escrowed_udndr": (None if held.get("bond_escrowed_total") is None
                                    else int(held.get("bond_escrowed_total") or 0)),
            "bond_escrowed_count": (None if held.get("bond_escrowed_count") is None
                                    else int(held.get("bond_escrowed_count") or 0)),
            # The third pocket: jurors retained by the exit guard, the same kind of promise ("locked is not
            # seized"), so the same section. A `jurors_locked` that keeps growing means the guard is
            # immobilising the juror pool; an `oldest_lock_age` that only rises means honest jurors are paying
            # for other people's silence. These are the figures exit-after-vote should be decided on.
            "jurors_locked": (None if held.get("jurors_locked_count") is None
                              else int(held.get("jurors_locked_count") or 0)),
            "oldest_lock_age_blocks": _lockage,
            "oldest_lock_job_id": held.get("oldest_lock_job_id"),
            "age_warning": (prune_window_blocks is not None
                            and int(held.get("oldest_age_blocks") or 0) > int(prune_window_blocks or 0) > 0),
            # `lock_warning` is the twin of the held warning: the same debt (unbounded deferral) seen from the
            # stake side, so `age_warning_threshold_blocks` governs both. It is a signal, never a block and
            # never an automatic release. None when the age cannot be measured ("could not look" is not "it is
            # fine"); False when only the threshold is missing, since no warning is invented without one.
            "lock_warning": (None if _lockage is None
                             else (prune_window_blocks is not None
                                   and _lockage > int(prune_window_blocks or 0) > 0)),
            "age_warning_threshold_blocks": prune_window_blocks}
    # The age of the last block, the only field here that tells a live chain from a stopped one.
    # `generated_at` is the age of this document and is always fresh, so a feed regenerated every fifteen
    # seconds would look alive while the chain produces nothing; `height` stays high, only motionless.
    #
    # `stale` has three values: true when the last block is older than the threshold, false when it is not,
    # null when the timestamp could not be read, so that nothing is claimed. As for `held` and
    # `lock_warning` above, null means not measurable, never zero.
    #
    # The age is bounded on both sides by the same threshold. A block can only be ahead of the clock that
    # measures it when that clock is behind, and a clock behind by the threshold or more makes a live chain
    # and a stopped one look alike, so clamping at zero would turn it into "0 s old", the freshest reading
    # possible (`deploy/join.sh::_caught_up_line` has the same rule). With `age = read_at - last_block_epoch`:
    #   age >= threshold               -> stale (the equality counts, see the selftest);
    #   -threshold < age < threshold   -> not stale, and the age is published as measured, a negative
    #                                     one included: ordinary clock skew, which no zero may hide;
    #   age <= -threshold              -> this host's clock disagrees with the chain: the age and the
    #                                     verdict are both null -- not measured, never fresh.
    #
    # The age is taken at the status read, never at `now`: the reads after it (commits, audit timing) take
    # a minute on the live network, and measured at `now` that minute was published as the age of a chain
    # that makes a block every few seconds. `read_at` is published, so a reader can tell how old the reading is.
    _now = int(time.time()) if now is None else int(now)
    chain_out = chain_block(height, block_epoch, _now if read_at is None else read_at)
    return {
        "generated_at": _now,
        # null when the height was not read (`_known_height`), never the 0 of a block no listing belongs to
        "height": _known_height(height),
        # The pair (height, age) lives here together. `height` above stays where it is, so that a consumer
        # that reads it keeps working.
        "chain": chain_out,
        "jobs": {"total": total, "settled": settled, "audited": audited,
                 "vindicated": vindicated, "clawed": clawed, "audit_pending": part["audit_pending"],
                 # Partition of the opened audits, in 8 exclusive classes (audit_pending, the four
                 # resolved_*, resolved_unwound, unwound_after_deferral, deferred_no_jury). Either the sum
                 # equals audits_opened (an independent on-chain counter), or partition_ok is false,
                 # which means a state is hiding. unwound_never_drawn is listed, outside the partition.
                 "audits_opened": part["audits_opened"],
                 "resolved_by_quorum": part["resolved_by_quorum"],
                 "resolved_no_quorum": part["resolved_no_quorum"],
                 "resolved_by_adjudication": part["resolved_by_adjudication"],
                 "resolved_unmarked": part["resolved_unmarked"],
                 "resolved_unwound": part["resolved_unwound"],
                 "unwound_after_deferral": part["unwound_after_deferral"],
                 "unwound_never_drawn": part["unwound_never_drawn"],
                 "deferred_no_jury": part["deferred_no_jury"],
                 "partition_ok": part["partition_ok"],
                 "partition_bounds_ok": part["partition_bounds_ok"],
                 "partition_unmeasured_reason": part["partition_unmeasured_reason"],
                 # Min of the distinct operators behind the audits closed by quorum, over the whole
                 # life of the chain. null while no audit has been conducted. A quorum reached by 15
                 # seats all held by one operator is a quorum in form only, so the gate reads
                 # distinct_min >= 3 (a cheap floor) and max_operator_share < 50%, which is the real
                 # property: no operator held the majority of the counted votes.
                 "distinct_voters_min": distinct_min,
                 "max_operator_share_bps": max_op_share_bps},
        # The opened audits by how they ended, checked against `audits_opened` (see `audit_outcomes`).
        "audit_outcomes": audit_outcomes(part, height),
        # "Retained is not lost", made checkable for both pockets: the retained fee and the escrowed bond
        # (an invisible bond would be an orphaned fund). `age_warning` is a signal, never a block and never
        # a release, raised when the oldest retention outlives the window it takes to evict a dead miner
        # (miner_prune_blocks): funds retained longer than that mean the network is not healing.
        # null means not measurable on this binary, never an invented zero.
        "held": held_out,
        # Who asked for the jobs, and how the work and the verdicts are spread (see `origin_partition` and
        # `transparency`). Each is read from the same listings as `jobs`, at `height`.
        "origin": origin_partition(jobs, season_generator, gateway_address, height),
        "transparency": transparency(jobs, miners, commits, audit_timing, height),
        "slashes": {"events": len(slash_events), "total_udndr": slashed_total_u,
                    "recent": slash_events[-max_recent:]},
        "recent_audits": recent_audits[-max_recent:],
        # None when the seed-health query failed (client.committee_seed_health): published as null,
        # never as an empty object whose every field the explorer would read as a measured zero.
        "vrf": seed_health,
        "pools": pools or {},
        # Public feed: these strings are served as they are on the /proof endpoint, so they describe the
        # provenance in neutral terms.
        "_provenance": {
            "claim": "On-chain state, read-only, through the public RPC. No value is computed or "
                     "completed off-chain.",
            "note": "audited = audit opened with an anchored jury, or disputed; clawed = payment "
                    "reclaimed by an audit jury's conviction; vindicated = resolved with no clawed "
                    "marker, which includes jobs unwound to the client and jobs adjudicated by a "
                    "dispute jury (either outcome), so it is not a count of upheld verdicts; "
                    "resolved_by_quorum counts the audits a jury concluded; resolved_unwound counts "
                    "disputed audits refunded with no verdict; unwound_after_deferral counts audits "
                    "selected while the jury was below its floor, then refunded at once or after "
                    "deferrals, which audits_opened includes and audited does not; "
                    "unwound_never_drawn counts refunds of jobs no audit selected. "
                    "audit_outcomes groups the opened audits by how they ended (verified, "
                    "refunded_no_verdict, no_quorum, in_progress, unknown) and publishes the counts only "
                    "when they add up to audits_opened. A verified audit is one a jury concluded: it says "
                    "nothing about how strong that jury was. "
                    "silence_slash_bps has no effect: the only code that reads it, antievasion.go::clawbackPayment, "
                    "has no production caller, so a silent miner is not charged by that parameter. "
                    "A null field means not measurable, never zero. Public research testnet.",
        },
    }




class Unmeasured(Exception):
    """A read the payload depends on could not be made. Its message is served in the public payload,
    so it names what was not read, never a host, a path or a command line."""


# The fields `build_proof` computes against the clock (`now`, or the `read_at` of the status read),
# apart from `generated_at` and `chain.read_at`, which date the document and the reading. They are
# verdicts about the moment of the reading, not facts about the snapshot. The bench derives the same set
# from `build_proof` (two builds that differ only by the clock) and fails when a field that depends on the
# clock is added without being listed here.
NOW_VERDICTS = (("chain", "last_block_age_s"), ("chain", "stale"))


def kept_payload(doc, reason=None):
    """The last snapshot, as it is served after a failed refresh: its facts kept, its verdicts about
    now withdrawn (null = not measured now).

    Served as built, a snapshot taken five seconds after a block would keep answering "new blocks keep
    arriving" (`chain.stale` false, `chain.last_block_age_s` 5) for the whole outage of the node, on the one
    field a reader consults to know whether the chain runs. The kept age cannot be advanced either: a block
    newer than the kept one may exist, so `now - last_block_epoch` only bounds the real age from above. The
    facts (`height`, `chain.last_block_epoch`, `chain.read_at`, every count) stay, dated by
    `refresh.snapshot_generated_at`.
    With a `reason`, `chain.verdict_withdrawn` carries it, so that a null verdict reads as withdrawn and
    not as a block the feed could not date; a reason already there is kept, being the first cause.
    `doc` is not modified."""
    payload = dict(doc)
    for section, field in NOW_VERDICTS:
        part = dict(payload[section])
        part[field] = None
        if reason is not None and section == "chain":
            part["verdict_withdrawn"] = part.get("verdict_withdrawn") or reason
        payload[section] = part
    return payload


def withdrawal_reason(chain, now):
    """Why the verdicts about now of a payload's `chain` section cannot be served at `now`, or None when
    they can. The read is too far when `now - read_at` is `STALE_BLOCK_S` or more, or when the clock is
    `STALE_BLOCK_S` or more behind it (the same two-sided bound as the age in `build_proof`). The text is
    served in the payload, so it names no host and no figure of the network."""
    read_at = chain.get("read_at")
    if not isinstance(read_at, int) or isinstance(read_at, bool):
        return "the chain reading carries no usable time"
    gap = int(now) - read_at
    if gap >= STALE_BLOCK_S:
        return "the chain reading is older than stale_threshold_s"
    if gap <= -STALE_BLOCK_S:
        return "the clock of the feed is behind the chain reading by stale_threshold_s or more"
    return None


def serve_body(body, now):
    """The bytes a request is answered with: `body`, the published payload, or a copy of it without its
    verdicts about now (`NOW_VERDICTS`, null = not measured now, with `chain.verdict_withdrawn` saying why)
    once its chain read is too far from `now` (`withdrawal_reason`).

    A payload is dated by the chain read it came from, not by the moment it is served. The light chain
    reader (`publish_chain`) keeps that read recent between heavy builds; this strip is the net for a light
    reading that fails: served as built, one payload could answer "new blocks keep arriving"
    (`chain.stale` false) long after its read, with the chain halted in between. A payload with no `chain`
    section (before the first refresh) has no verdict to withdraw. `body` and the cached payload are never
    modified."""
    doc = json.loads(body)
    chain = doc.get("chain")
    if not isinstance(chain, dict):
        return body
    why = withdrawal_reason(chain, now)
    if why is None:
        return body
    return json.dumps(kept_payload(doc, why), ensure_ascii=False).encode()


NOT_YET_BUILT = "first refresh not completed"


def new_cache():
    """The served state before any refresh has completed. Requests never build: until the builder thread
    publishes, they are served this payload. It is marked, never `{}`, since an empty object would have no
    `refresh` field at all. Same shape as a refresh that failed with nothing to keep; `attempted_at` is
    null because no attempt has completed."""
    payload = {"refresh": {"current": False, "attempted_at": None, "failing_since": None,
                           "reason": NOT_YET_BUILT, "snapshot_generated_at": None, "build_s": None,
                           "gap_s": None, "slow_build": None}}
    return {"payload": json.dumps(payload, ensure_ascii=False).encode(), "doc": None,
            "failing_since": None, "gap_s": None, "chain": None, "lock": threading.Lock()}


def _swap_lock(cache):
    """The lock under which `publish` and `publish_chain` replace the served state of `cache`; a cache
    built by hand without one (a bench) is swapped unlocked, being single-threaded."""
    lock = cache.get("lock")
    return contextlib.nullcontext() if lock is None else lock


# The feed shares its host with the validator: a slow build is a loaded host, and waiting a multiple of
# the build gives the time back, so the build never takes more than 1/(1 + GAP_FACTOR) of the clock.
GAP_FACTOR = 4


def refresh_gap(refresh_s, build_s):
    """Seconds to wait, from the end of a build, before the next one starts."""
    return max(refresh_s, GAP_FACTOR * build_s)


def build_loop(cache, build, refresh_s, sleep, stop):
    """Run `build()` one at a time until `stop()` is true, waiting between two builds the gap that
    `publish` left in `cache["gap_s"]` (counted from the end of the build), or `refresh_s` when the
    build left none or raised. `sleep` and `stop` are injected so a bench can drive it with a fake clock.
    The loop is the only caller, so two builds never overlap; requests read `cache["payload"]` and
    never enter it."""
    while not stop():
        cache["gap_s"] = None
        try:
            build()
        except Exception as e:  # noqa: BLE001 -- a dead builder would freeze the feed without a word
            sys.stderr.write(f"[the-proof] build: {type(e).__name__}: {e}\n")
        # The wait has its own try: an error from it (an out-of-range timeout) must not end the thread.
        try:
            gap = cache.get("gap_s")
            sleep(refresh_s if gap is None else gap)
        except Exception as e:  # noqa: BLE001 -- the loop outlives a failed wait
            print(f"[the-proof] wait: {type(e).__name__}: {e}", file=sys.stderr)


def publish(cache, now, read, clock=None, refresh_s=None):
    """One refresh of the served payload; True when it was rebuilt. Pure apart from `read()` and `clock`
    (`time.monotonic` when None), which times the attempt.

    `read()` returns the keyword arguments of `build_proof`, or raises. On success the payload is
    rebuilt and marked current. On any failure the last payload built is kept and served, and it says
    so: `refresh.current` is false, with the time the failures began and their reason. Rebuilding from what
    could not be read would publish zeros (no job, no audit, no slash) as if measured, and serving the old
    snapshot without a mark would present it as fresh. A kept snapshot also loses its verdicts about now
    (`kept_payload`, with `chain.verdict_withdrawn` saying why), since a reader of `chain.stale` does not
    read the mark; unless the light chain reader (`publish_chain`) has taken a reading later than the
    snapshot's own, which is then served as it is. A rebuilt payload takes that later reading too
    (`_with_newest_chain`). The served state is swapped in under `cache["lock"]`.

    `refresh.current` has no third value: a payload either came from the last attempt or it did not. What
    is not known, because nothing was ever built, shows as the absence of every other field, with
    `snapshot_generated_at` null, never as a payload of zeros (see `new_cache` for the payload served
    before the first refresh completes).

    `refresh.build_s` is how long the attempt dated by `attempted_at` took, failed or not. It grows with
    the number of jobs and commits of the season, since every refresh walks both listings, and a reader
    needs it to know how far apart two readings of the feed can be.

    With `refresh_s` (the interval the operator asked for), `refresh.gap_s` is the wait in force after
    this attempt (`refresh_gap`, counted from its end; also left in `cache["gap_s"]` for `build_loop`) and
    `refresh.slow_build` is true when the attempt took longer than `refresh_s`, the alert that the feed
    cannot keep the pace it was asked for. Without it both are null: not known, never a made-up pace."""
    clock = time.monotonic if clock is None else clock
    started = clock()

    def pace(elapsed):
        if refresh_s is None:
            return {"gap_s": None, "slow_build": None}
        # Rounded to a tenth for display, but never below the interval that was asked for.
        gap = max(round(refresh_gap(refresh_s, elapsed), 1), refresh_s)
        cache["gap_s"] = gap
        return {"gap_s": gap, "slow_build": elapsed > refresh_s}
    try:
        doc = build_proof(**read())
        elapsed = clock() - started
        refresh = {"current": True, "attempted_at": int(now), "failing_since": None,
                   "reason": None, "snapshot_generated_at": doc.get("generated_at"),
                   "build_s": round(elapsed, 1), **pace(elapsed)}
        with _swap_lock(cache):
            doc = _with_newest_chain(cache, doc)
            payload = dict(doc)
            payload["refresh"] = refresh
            body = json.dumps(payload, ensure_ascii=False).encode()
            cache["doc"] = doc
            cache["failing_since"] = None
            cache["payload"] = body
        return True
    except Unmeasured as e:
        reason = str(e)
    except Exception as e:  # noqa: BLE001 -- the facade never breaks: it serves the last snapshot
        sys.stderr.write(f"[the-proof] refresh: {type(e).__name__}: {e}\n")
        reason = f"refresh failed ({type(e).__name__})"
    elapsed = clock() - started
    with _swap_lock(cache):
        if cache.get("failing_since") is None:
            cache["failing_since"] = int(now)
        kept = cache.get("doc")
        payload = kept_payload(kept, "the last refresh failed") if kept is not None else {}
        light = cache.get("chain")
        # A light reading newer than the kept snapshot's own is a measurement of now, not a kept verdict.
        if kept is not None and light is not None and _read_at(light) >= _read_at(kept["chain"]):
            payload["chain"] = light
        payload["refresh"] = {"current": False, "attempted_at": int(now),
                              "failing_since": cache["failing_since"], "reason": reason,
                              "snapshot_generated_at": None if kept is None else kept.get("generated_at"),
                              "build_s": round(elapsed, 1), **pace(elapsed)}
        cache["payload"] = json.dumps(payload, ensure_ascii=False).encode()
    return False


def _read_at(chain):
    """The `read_at` of a `chain` section as an int; it is always one in a section this module built."""
    return int(chain["read_at"])


def _with_newest_chain(cache, doc):
    """`doc`, or a copy of it carrying the light reading in `cache["chain"]` when that one was made later
    than the status read the build took. The light reader keeps going while a build runs: publishing the
    build's older `chain` over it would age the block verdict back to the time of the build's status read.
    Called with the swap lock held."""
    light = cache.get("chain")
    if light is None or _read_at(light) <= _read_at(doc["chain"]):
        return doc
    return {**doc, "chain": light}


def publish_chain(cache, chain):
    """Replace only the `chain` section of the published payload with `chain`, a block made by
    `chain_block` from a light status read; True when the served payload changed.

    The heavy build takes its status read partway through and is spaced by its gap, so the block age it
    published goes old long before the next build. This keeps that one section current. Nothing else moves:
    the top-level `height` and every count stay those of the build, since they are read together and
    `chain.height` may now be ahead of them. The new bytes and the new `doc` are built beside the old ones
    and swapped in under the lock `publish` swaps under, so a handler serialising the previous payload keeps
    its own bytes. Before the first build there is no payload to attach it to; the reading is kept in
    `cache["chain"]` and `publish` takes it if it is newer than its own."""
    with _swap_lock(cache):
        cache["chain"] = chain
        doc = cache.get("doc")
        if doc is None or _read_at(chain) <= _read_at(doc["chain"]):
            return False
        served = json.loads(cache["payload"])
        served["chain"] = chain
        cache["doc"] = {**doc, "chain": chain}
        cache["payload"] = json.dumps(served, ensure_ascii=False).encode()
        return True


# ---------------------------------------------------------------------------
# The commits, read by increment
# ---------------------------------------------------------------------------
# A commit is written once and never replaced or deleted (msg_server_commit.go::CreateCommit refuses an
# existing key, and the delete message refuses by design), so a row read once stays true and the chain's
# listing, ordered by key, only ever gains rows. Where a new row lands depends on its key: a job id, a
# commit `<job>__<miner>` or a verdict `<job>__verdict__<miner>` lands near the end, and an epoch's reveal
# mark (`e<epoch>__reveals__<miner>`) lands in the middle. So a build does not trust the end of the order
# alone: it finds the first place where the chain holds a row the cache does not, by comparing the rows at
# a few offsets (one row each), reads a short window there, and repeats; when no row is missing below the
# newest cached key, it reads what follows that key.
#
# Rows read in the window at a gap, the gaps one build may patch before it reads everything again, and
# builds between two full reads. All three are orders of magnitude, not derivations: the window must cover
# a run of rows that arrive together, the patch count bounds the requests of a build, and the full read is
# a net for whatever the offsets of the node's listing could not show.
COMMIT_WINDOW = 100
COMMIT_PATCHES = 12
COMMIT_FULL_EVERY = 20


def new_commit_cache():
    """The commits kept between builds: `rows` {key: model_id} and the builds since the last full read."""
    return {"rows": {}, "since_full": 0}


def _listed(rows):
    return [{"key": k, "model_id": v} for k, v in sorted(rows.items())]


# What `_first_gap` answers when the chain has no row at an index the cache has one at: a chain that only
# gains rows cannot do that, so the cache belongs to another chain.
NOT_A_SUBSET = -1


def _first_gap(keys, rows, row_at):
    """The first index where the chain holds a row the cache lacks, or len(keys) when it holds none below
    the newest cached key; None when a row could not be read; NOT_A_SUBSET (see above).

    With the cache a subset of the chain and its keys distinct, the chain's row at index i is the cache's
    row at index i exactly when no uncached row sorts below it, so the test is true up to the gap and false
    after it. One read at the last index settles the common case (rows added at the end only); otherwise a
    bisection finds the gap. `row_at(i)` is the chain's row at offset i, False when the listing has no row
    there, or None when it could not be read."""
    def look(i):
        row = row_at(i)
        if row is None:
            return "unread"
        if row is False:
            return "absent"
        return "same" if row["key"] == keys[i] and row["model_id"] == rows[keys[i]] else "differs"

    last = len(keys) - 1
    seen = look(last)
    if seen == "same":
        return len(keys)
    lo, hi = 0, last                     # the test is false at hi
    while seen == "differs" and lo < hi:
        mid = (lo + hi) // 2
        seen = look(mid)
        if seen == "same":
            lo, seen = mid + 1, "differs"
        elif seen == "differs":
            hi = mid
    if seen == "unread":
        return None
    if seen == "absent":
        return NOT_A_SUBSET
    return lo


def read_commits_by_increment(cache, total, row_at, read_from, window, patches, full_every):
    """([{key, model_id}] in key order, or None) and how it was read, from `cache` and the chain.

    `total()` is the number of commits the chain holds, or None. `row_at(i)` is the row at offset i of the
    listing, False when there is none, or None when it could not be read. `read_from(start_key, max_rows)` is the rows from `start_key` (None = the first key;
    the key itself is included) to the end, or the first `max_rows` of them, or None when they could not
    be read. How it was read:
      full       every row was read again: nothing was kept, the count fell below what was kept, a full
                 read was due, a kept row is not on the chain as it was, or the patches did not settle
      cached     the count is the one kept and the newest kept row is where it was
      increment  rows were added to the kept ones, at the gaps found and after the newest kept key
      unread     a read failed; the cache is left as it was and the result is None
    The rows are trusted only after these checks: a window starts on the kept row it should start on, no
    row read again differs from the one kept, no key comes twice in a read, and at the end no gap was found
    below the newest kept key and the rows add up to at least the count read first. A failed check is a
    full read, never a smaller list. A row inserted in the middle between two reads of one build is found
    by the next build."""

    def keep(rows, how):
        cache["rows"] = rows
        cache["since_full"] = 0 if how == "full" else cache["since_full"] + 1
        return _listed(rows), how

    def full():
        got = read_from(None, None)
        if got is None:
            return None, "unread"
        rows = {r["key"]: r["model_id"] for r in got}
        if len(rows) != len(got):
            return None, "unread"          # a key twice: the pages overlapped, the listing is not a reading
        return keep(rows, "full")

    def merge(rows, got, start):
        """The rows after reading `got`, which must start on the kept `start` row (None: the first key), or
        None when a check fails."""
        if start is not None and (not got or got[0]["key"] != start or got[0]["model_id"] != rows[start]):
            return None
        fresh = {r["key"]: r["model_id"] for r in got}
        if len(fresh) != len(got) or any(k in rows and rows[k] != m for k, m in fresh.items()):
            return None
        return {**rows, **fresh}

    kept = cache["rows"]
    n = total()
    if n is None or not kept or cache["since_full"] >= full_every or n < len(kept):
        return full()
    rows = dict(kept)
    for _ in range(patches + 1):
        keys = sorted(rows)
        gap = _first_gap(keys, rows, row_at)
        if gap is None:
            return None, "unread"
        if gap == NOT_A_SUBSET:
            return full()
        if gap == len(keys):
            if n == len(kept) == len(rows):
                return keep(kept, "cached")          # the newest kept row is where it was: nothing was added
            got = read_from(keys[-1], None)
            if got is None:
                return None, "unread"
            rows = merge(rows, got, keys[-1])
            if rows is None or len(rows) < n:
                return full()
            return keep(rows, "increment")
        start = keys[gap - 1] if gap > 0 else None
        got = read_from(start, window + (0 if start is None else 1))
        if got is None:
            return None, "unread"
        rows = merge(rows, got, start)
        if rows is None:
            return full()
    return full()


def _selftest():
    jobs = [
        {"id": "j1", "state": "open+paid+optimistic", "fee": 100, "slashes": []},
        {"id": "j2", "state": "open+paid+optimistic+disputed", "fee": 100, "slashes": []},
        {"id": "j3", "state": "open+paid+optimistic+disputed+resolved", "fee": 100, "slashes": []},
        {"id": "j4", "state": "open+paid+optimistic+disputed+resolved+clawed", "fee": 100,
         # A neutral identifier: `slashes[].miner_id` travels as it is into the public feed.
         "slashes": [{"miner_id": "miner-c", "amount": 320000}]},
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
    chk("without the on-chain query (deferred=None): deferred/opened/partition_ok are null, never a true by default",
        p["jobs"]["deferred_no_jury"] is None and p["jobs"]["audits_opened"] is None
        and p["jobs"]["partition_ok"] is None)

    # Partition: the gate counts only the audits closed by quorum, and these cases exercise the
    # counters.
    jobs_part = [
        {"id": "q1", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "fee": 10,
         "audit_distinct_voters": 4, "audit_voters": 10, "audit_max_operator_votes": 6, "slashes": []},
        {"id": "q2", "state": "open+paid+optimistic+disputed+resolved+clawed+noquorum", "fee": 10, "slashes": []},
        {"id": "q3", "state": "open+paid+optimistic+disputed+resolved+vindicated+quorum", "fee": 10,
         "audit_distinct_voters": 3, "audit_voters": 10, "audit_max_operator_votes": 3, "slashes": []},
        {"id": "q4", "state": "open+paid+optimistic+disputed", "fee": 10, "slashes": []},
        {"id": "q5", "state": "open+paid+optimistic+disputed+resolved+adjudicated", "fee": 10,
         "audit_distinct_voters": 5, "slashes": []},
    ]
    # deferred: q6 was never opened (it counts), q4 was selected then disputed (deduplicated, so it
    # does not count twice).
    deferred = {"job_ids": ["q6", "q4"], "audits_opened": 6}
    pq = build_proof(jobs_part, {}, {}, 1, deferred=deferred)
    # "noquorum" contains "quorum": a naive classification would count q2 as a conducted audit.
    chk("partition: by_quorum=2, no_quorum=1, by_adjudication=1, pending=1 (an expired audit is not a conducted one)",
        pq["jobs"]["resolved_by_quorum"] == 2 and pq["jobs"]["resolved_no_quorum"] == 1
        and pq["jobs"]["resolved_by_adjudication"] == 1 and pq["jobs"]["audit_pending"] == 1)
    chk("deferred deduplicated against disputes: q4 (selected then disputed) counts once -> deferred=1",
        pq["jobs"]["deferred_no_jury"] == 1)
    # The partition test against the independent source: 5 classes + 1 deferred == 6 opened.
    chk("partition_ok: classes+deferred == audits_opened (an on-chain counter, not the same enumeration)",
        pq["jobs"]["partition_ok"] is True)
    # A version comparing two sides of the same loop would be true by construction: this case must be able
    # to return False.
    bad = build_proof(jobs_part, {}, {}, 1, deferred={"job_ids": ["q6", "q4"], "audits_opened": 99})
    chk("partition_ok can come out false (opened=99 != classes) - a sum that cannot fail verifies nothing",
        bad["jobs"]["partition_ok"] is False)

    # -- Unwound audits ---------------------------------------------------------------------------
    def _jobs(spec):
        return [{"id": f"{tag}{i}", "state": st, "fee": 1, "slashes": []}
                for tag, st, n in spec for i in range(n)]
    no_origin_needed = {"noseed_unwound": [], "drawn": []}
    ru = build_proof(_jobs([("u", "open+paid+optimistic+disputed+resolved+unwound", 1)]), {}, {}, 1,
                     deferred={"job_ids": [], "audits_opened": 1})
    chk("disputed then unwound -> resolved_unwound=1, no longer in resolved_unmarked; still in vindicated",
        ru["jobs"]["resolved_unwound"] == 1 and ru["jobs"]["resolved_unmarked"] == 0
        and ru["jobs"]["vindicated"] == 1 and ru["jobs"]["partition_ok"] is True)
    ua = build_proof(_jobs([("a", "open+paid+optimistic+resolved+unwound", 1)]), {}, {}, 1,
                     deferred={"job_ids": [], "audits_opened": 1}, unwind_origin=no_origin_needed)
    chk("unwound after a deferral (no +disputed) -> unwound_after_deferral=1, counted as opened, not audited",
        ua["jobs"]["unwound_after_deferral"] == 1 and ua["jobs"]["unwound_never_drawn"] == 0
        and ua["jobs"]["audited"] == 0 and ua["jobs"]["partition_ok"] is True)
    live_mix = [("p", "open+paid+optimistic", 1712), ("r", "open+paid+optimistic+disputed+resolved+unwound", 251),
                ("d", "open+paid+optimistic+disputed", 118), ("w", "open+paid+optimistic+resolved+unwound", 81),
                ("o", "open", 77)]
    lm = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450},
                     unwind_origin=no_origin_needed)["jobs"]
    chk("live mix (2239 jobs): 118 pending + 251 resolved_unwound + 81 after deferral == 450 -> partition_ok true",
        lm["total"] == 2239 and lm["audited"] == 369 and lm["audit_pending"] == 118
        and lm["resolved_unwound"] == 251 and lm["unwound_after_deferral"] == 81
        and lm["partition_ok"] is True and lm["partition_bounds_ok"] is True)
    lh = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 451},
                     unwind_origin=no_origin_needed)["jobs"]
    chk("live mix with one opened audit no state shows -> partition_ok false (a state is hiding)",
        lh["partition_ok"] is False)
    lu = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450})["jobs"]
    chk("unwind origin unreadable -> unwound classes and partition_ok null (never true), with the reason",
        lu["unwound_after_deferral"] is None and lu["unwound_never_drawn"] is None
        and lu["partition_ok"] is None and lu["partition_unmeasured_reason"] == "unwind origin unreadable")
    chk("... while the bounds check, which needs no event, still answers (369 <= 450 <= 450)",
        lu["partition_bounds_ok"] is True)
    lub = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 532})["jobs"]
    chk("... and the bounds check can fail (532 > 369 + 81)", lub["partition_bounds_ok"] is False)
    # A job the no-seed loop unwound without any draw is not an opened audit. Counting it would give 451
    # against the chain's 450, so equality holds only if it is left out.
    never = _jobs(live_mix) + [{"id": "x1", "state": "open+paid+optimistic+resolved+unwound", "fee": 1,
                                "slashes": []}]
    ln = build_proof(never, {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450},
                     unwind_origin={"noseed_unwound": ["x1"], "drawn": []})["jobs"]
    chk("a never-drawn unwind is listed apart (unwound_never_drawn=1) and the partition still holds at 450",
        ln["unwound_never_drawn"] == 1 and ln["unwound_after_deferral"] == 81 and ln["partition_ok"] is True)
    lm2 = build_proof(never, {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450},
                      unwind_origin=no_origin_needed)["jobs"]
    chk("... miscounted as opened, the same job breaks equality (451 != 450) -> partition_ok false",
        lm2["unwound_after_deferral"] == 82 and lm2["partition_ok"] is False)
    ld = build_proof(never, {}, {}, 1, deferred={"job_ids": [], "audits_opened": 451},
                     unwind_origin={"noseed_unwound": ["x1"], "drawn": ["x1"]})["jobs"]
    chk("unwound in a no-seed block but drawn and deferred before -> counted as opened",
        ld["unwound_after_deferral"] == 82 and ld["unwound_never_drawn"] == 0 and ld["partition_ok"] is True)
    chk("state markers are split on '+', never substring-matched",
        state_markers("open+paid+optimistic+disputed+resolved+clawed+noquorum") >= {"noquorum", "clawed"}
        and "quorum" not in state_markers("open+disputed+resolved+noquorum"))

    lb = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 368})["jobs"]
    chk("... and fails from below too (368 < 369 disputed classes)", lb["partition_bounds_ok"] is False)
    mv = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 451},
                     unwind_origin=no_origin_needed, chain_moved=True)["jobs"]
    chk("reads that straddle a change of the chain -> partition_ok and bounds null with the reason, never false",
        mv["partition_ok"] is None and mv["partition_bounds_ok"] is None
        and mv["partition_unmeasured_reason"] == "the chain moved during the read")

    # -- Did the chain move between the reads? ----------------------------------------------------
    d450 = {"job_ids": ["s1"], "audits_opened": 450}
    chk("chain_moved: same counter and same selections -> false",
        chain_moved(d450, dict(d450)) is False)
    chk("chain_moved: an audit opened between the reads -> true",
        chain_moved(d450, {"job_ids": ["s1"], "audits_opened": 451}) is True)
    chk("chain_moved: a selected job unwound between the reads (it leaves job_ids) -> true",
        chain_moved(d450, {"job_ids": [], "audits_opened": 450}) is True)
    chk("chain_moved: an unread counter, before or after, is unknown (None), never 'no move'",
        chain_moved(None, d450) is None and chain_moved(d450, None) is None)
    ub = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450},
                     unwind_origin=no_origin_needed, chain_moved=chain_moved(None, d450))["jobs"]
    chk("... and the partition and the bounds are null with the reason, never a verdict",
        ub["partition_ok"] is None and ub["partition_bounds_ok"] is None
        and ub["partition_unmeasured_reason"] == "counter unread before the listing")
    # A job no audit ever selected, unwound by the no-seed loop after the listing: it is in no partition
    # class on either side, so it must not keep the partition null during a seed outage.
    nl = [{"id": "w9", "state": "open+paid", "fee": 1, "slashes": []}]
    after9 = dict(d450)
    nm = build_proof(nl, {}, {}, 1, deferred=after9, unwind_origin={"noseed_unwound": ["w9"], "drawn": []},
                     chain_moved=chain_moved(d450, after9))["jobs"]
    chk("a never-selected job unwound by the no-seed loop after the listing is not a move",
        chain_moved(d450, after9) is False and nm["partition_unmeasured_reason"] is None
        and nm["partition_bounds_ok"] is not None)

    # -- The event reader, against a fake RPC -----------------------------------------------------
    def _ev(kind, **attrs):
        return {"type": kind, "attributes": [{"key": k, "value": v} for k, v in attrs.items()]}

    def _unw(jid):
        return _ev("audit_retention_unwound", job_id=jid, age_blocks="900", refunded="1")

    def _draw(jid):
        return _ev("audit_draw_deferred", job_id=jid, eligibles="0", floor="4")

    def _noseed(n):
        return _ev("audit_draw_deferred", jobs="0", unwound=str(n), dropped="0")

    def _fake_rpc(blocks, latest=100, earliest="1", network="t", page_size=100, numeric=True,
                  indexed_to=None, index_extra=(), index_drop=(), fail=False, calls=None):
        """A CometBFT RPC over `blocks` = {height: [events]}. block_search evaluates the query as the kv
        indexer does: a block matches a condition when one of its events meets it. `numeric=False` is an
        index that silently matches no numeric condition; `index_extra` heights are claimed without
        matching and `index_drop` heights are counted but never served; a page past the last one is an
        error, as on the node."""
        top = latest if indexed_to is None else indexed_to
        calls = {} if calls is None else calls

        def meets(h, cond):
            words = cond.split(" ")
            if words[0] == "block.height":
                v = int(words[2])
                return {"=": h == v, ">": h > v, "<=": h <= v}[words[1]]
            if len(words) == 2 and words[1] == "EXISTS":
                kind, key = words[0].split(".")
                return any(e["type"] == kind and any(a["key"] == key for a in e["attributes"])
                           for e in blocks.get(h, []))
            if len(words) == 3:
                kind, key = words[0].split(".")
                if not numeric:
                    return False
                v = int(words[2])
                return any(e["type"] == kind and any(a["key"] == key and {">": int(a["value"]) > v,
                                                                       ">=": int(a["value"]) >= v}[words[1]]
                                                     for a in e["attributes"])
                           for e in blocks.get(h, []))
            name, _, value = cond.partition("=")
            if value.count(chr(39)) != 2 or not (value.startswith(chr(39)) and value.endswith(chr(39))):
                raise ValueError("query parse error")
            kind, key = name.split(".")
            return any(e["type"] == kind and any(a["key"] == key and a["value"] == value[1:-1]
                                                 for a in e["attributes"])
                       for e in blocks.get(h, []))

        def rpc(path, params):
            calls[path] = calls.get(path, 0) + 1
            if fail:
                raise OSError("unreachable")
            if path == "/status":
                return {"node_info": {"network": network},
                        "sync_info": {"earliest_block_height": earliest, "latest_block_height": str(latest),
                                      "earliest_block_hash": "AB12"}}
            if path == "/block_results":
                return {"finalize_block_events": blocks.get(int(params["height"]), [])}
            q = params["query"]
            if not (q.startswith(chr(34)) and q.endswith(chr(34))) or chr(34) in q[1:-1]:
                raise ValueError("query parse error")
            conds = q[1:-1].split(" AND ")
            if "job_id=" in q:
                calls["drawn_search"] = calls.get("drawn_search", 0) + 1
            pool = range(1, top + 1) if all(c.startswith("block.height") for c in conds) else sorted(blocks)
            extra = set(index_extra) if "audit_draw_deferred.unwound" in q else set()
            hits = sorted({h for h in pool if h <= top and all(meets(h, c) for c in conds)} | extra)
            per = min(int(params["per_page"]), page_size)
            page = int(params["page"])
            if hits and (page - 1) * per >= len(hits):
                raise ValueError("page out of range")
            served = [h for h in hits[(page - 1) * per:page * per] if h not in index_drop]
            return {"total_count": str(len(hits)), "blocks": [{"block": {"header": {"height": str(h)}}}
                                                              for h in served]}
        return rpc

    wjobs = _jobs([("w", "open+paid+optimistic+resolved+unwound", 3)])
    empty = {"noseed_unwound": [], "drawn": [], "unknown": []}
    # w1 and w2 were drawn and deferred; w2 was unwound by a later draw; the no-seed loop unwound w0 and w1.
    base = {10: [_draw("w1")], 11: [_draw("w2")], 50: [_unw("w2")], 70: [_noseed(2), _unw("w0"), _unw("w1")]}
    want = {"noseed_unwound": ["w0", "w1"], "drawn": ["w1"], "unknown": []}
    chk("reader: no no-seed unwind indexed -> every undisputed unwind came from a draw",
        read_unwind_origin(wjobs, _fake_rpc({50: [_unw("w0")], 51: [_unw("w1")], 52: [_unw("w2")]})) == empty)
    chk("reader: a no-seed block unwinding w0 and w1, only w1 drawn before -> w0 is the never-drawn one",
        read_unwind_origin(wjobs, _fake_rpc(base)) == want)
    chk("reader: an index without numeric conditions gives the same answer (every no-seed block read)",
        read_unwind_origin(wjobs, _fake_rpc(base, numeric=False)) == want)
    # The block also carries an unwind of w2, an undisputed job: only the loop's own count says it is not one.
    zero = {50: [_unw("w0")], 51: [_unw("w1")], 71: [_noseed(0), _unw("w2")]}
    chk("reader: a no-seed block with unwound=0 adds nothing, even beside another unwind (numeric index)",
        read_unwind_origin(wjobs, _fake_rpc(zero)) == empty)
    chk("reader: ... and on an index that reads every no-seed block",
        read_unwind_origin(wjobs, _fake_rpc(zero, numeric=False)) == empty)
    nz = {}
    read_unwind_origin(wjobs, _fake_rpc(zero, calls=nz))
    chk("reader: on a numeric index, a block where the loop unwound nothing is never fetched",
        "/block_results" not in nz)
    chk("reader: RPC unreachable -> None, never an assumed origin",
        read_unwind_origin(wjobs, _fake_rpc(base, fail=True)) is None)
    chk("reader: node without the first blocks -> None (an empty search would mean nothing)",
        read_unwind_origin(wjobs, _fake_rpc(base, earliest="5000")) is None)
    chk("reader: index shows no unwind while the listing shows some -> None",
        read_unwind_origin(wjobs, _fake_rpc({})) is None)
    more = {**base, 80: [_unw("x8")], 81: [_unw("x9")]}
    chk("reader: index shows more unwind blocks than the listing has unwound jobs -> None",
        read_unwind_origin(wjobs, _fake_rpc(more)) is None)
    chk("reader: the index check stops at the height read before the listing (later unwinds are not in it)",
        read_unwind_origin(wjobs, _fake_rpc(more), heights=(60, 100)) == want)
    chk("reader: a block the search returns whose events do not read as expected -> None",
        read_unwind_origin(wjobs, _fake_rpc(base, index_extra=(72,))) is None)
    short = {10: [_draw("w1")], 50: [_unw("w2")], 51: [_unw("w1")], 70: [_noseed(2), _unw("w0")]}
    chk("reader: a no-seed block that counts 2 unwinds but names 1 -> None",
        read_unwind_origin(wjobs, _fake_rpc(short)) is None)
    chk("reader: the index has not reached the height asked -> None",
        read_unwind_origin(wjobs, _fake_rpc(base, indexed_to=99)) is None)
    chk("reader: heights that are not 0 < before <= after <= latest -> None",
        read_unwind_origin(wjobs, _fake_rpc(base), heights=(0, 100)) is None
        and read_unwind_origin(wjobs, _fake_rpc(base), heights=(90, 101)) is None
        and read_unwind_origin(wjobs, _fake_rpc(base), heights=(100, 60)) is None)
    w3 = wjobs + [{"id": "w3", "state": "open+paid+optimistic+resolved+unwound", "fee": 1, "slashes": []}]
    late = {**base, 120: [_noseed(1), _unw("w3")]}
    chk("reader: a no-seed unwind above the height read after the listing is not counted",
        read_unwind_origin(w3, _fake_rpc(late, latest=130), heights=(100, 100)) == want)
    # Ids are chosen by the client: only the characters that break the quoted value are refused, and
    # then for that one job only.
    odd = [{"id": i, "state": "open+paid+optimistic+resolved+unwound", "fee": 1, "slashes": []}
           for i in ("w0", "a-1", "w'2")]
    oddc = {10: [_draw("w0")], 70: [_noseed(3), _unw("w0"), _unw("a-1"), _unw("w'2")]}
    og = read_unwind_origin(odd, _fake_rpc(oddc))
    chk("reader: an id with a quote is marked unknown, an id with a dash is read like any other",
        og == {"noseed_unwound": ["a-1", "w'2", "w0"], "drawn": ["w0"], "unknown": ["w'2"]})
    oj = build_proof(odd, {}, {}, 1, deferred={"job_ids": [], "audits_opened": 1}, unwind_origin=og)["jobs"]
    chk("... the unknown job makes partition_ok null with the reason, while the bounds check still answers",
        oj["partition_ok"] is None and oj["unwound_after_deferral"] is None
        and oj["partition_unmeasured_reason"] == "unwind origin unreadable for 1 job id(s)"
        and oj["partition_bounds_ok"] is True)
    chk("... and the payload stays valid JSON", bool(json.dumps(oj)))
    pages = {70: [_noseed(1), _unw("w0")], 71: [_noseed(1), _unw("w1")], 72: [_noseed(1), _unw("w2")]}
    allp = {"noseed_unwound": ["w0", "w1", "w2"], "drawn": [], "unknown": []}
    chk("reader: the search is read past its first page",
        read_unwind_origin(wjobs, _fake_rpc(pages, page_size=2)) == allp)
    chk("reader: a search that serves fewer blocks than it counts -> None",
        read_unwind_origin(wjobs, _fake_rpc(pages, page_size=2, index_drop=(72,))) is None)
    two = _fake_rpc({70: [_noseed(1), _unw("w0")], 71: [_noseed(1), _unw("w1")]})

    def undercount(path, params):
        res = two(path, params)
        if path == "/block_search" and "unwound >" in params["query"]:
            res = dict(res, total_count="1")
        return res
    chk("reader: a search that serves more blocks than it counts -> None",
        read_unwind_origin(wjobs, undercount) is None)
    sorted_rpc = _fake_rpc(pages)

    def reversed_rpc(path, params):
        res = sorted_rpc(path, params)
        if path == "/block_search" and "unwound >" in params["query"]:
            res = dict(res, blocks=res["blocks"][::-1])
        return res
    chk("reader: a search answer out of height order -> None (the walk resumes from its last height)",
        read_unwind_origin(wjobs, reversed_rpc) is None)
    # Progress is kept: a walk cut by the page bound resumes above its last block at the next call.
    part_cache, steps = new_origin_cache(), []
    for _ in range(3):
        steps.append(read_unwind_origin(wjobs, _fake_rpc(pages, page_size=1), cache=part_cache, max_pages=1))
    chk("reader: a walk cut by the page bound is null until it is complete, and resumes where it stopped",
        steps[0] is None and steps[1] is None and steps[2] == allp and part_cache["scanned_to"] == 100)
    # The cache: past blocks are read once.
    cache, n1, n2, n3 = new_origin_cache(), {}, {}, {}
    first = read_unwind_origin(wjobs, _fake_rpc(base, calls=n1), cache=cache)
    again = read_unwind_origin(wjobs, _fake_rpc(base, calls=n2), cache=cache)
    chk("reader: a second refresh reads no past block and asks no draw again",
        first == want and again == want and n1.get("/block_results") == 1 and n1.get("drawn_search") == 2
        and "/block_results" not in n2 and "drawn_search" not in n2)
    grown = dict(late)
    third = read_unwind_origin(w3, _fake_rpc(grown, latest=130, calls=n3), cache=cache)
    chk("reader: a later no-seed block is the only one read, and only its new job is asked about",
        third == {"noseed_unwound": ["w0", "w1", "w3"], "drawn": ["w1"], "unknown": []}
        and n3.get("/block_results") == 1 and n3.get("drawn_search") == 1)
    plain = {50: [_unw("w0")], 51: [_unw("w1")], 52: [_unw("w2")]}
    chk("reader: a cache learned on another chain is dropped",
        read_unwind_origin(wjobs, _fake_rpc(plain, latest=130, network="other"), cache=cache) == empty
        and cache["chain"] == ("other", "AB12"))
    full = dict(new_origin_cache(), chain=("t", "AB12"), scanned_to=100,
                noseed={f"z{i}" for i in range(ORIGIN_CACHE_MAX_IDS + 1)})
    chk("reader: a cache past its cap starts over",
        read_unwind_origin(wjobs, _fake_rpc(base), cache=full) == want)
    ahead = dict(new_origin_cache(), chain=("t", "AB12"), scanned_to=500)
    chk("reader: a cache that reached past the top of the chain (a relaunch) is dropped",
        read_unwind_origin(wjobs, _fake_rpc(base), cache=ahead) == want)
    # A /block_results that fails is read again at the next refresh: the progress mark only passes a
    # block once it has been read.
    flaky_base, flaky = _fake_rpc(base), {"failed": False}

    def flaky_rpc(path, params):
        if path == "/block_results" and params["height"] == "70" and not flaky["failed"]:
            flaky["failed"] = True
            raise OSError("block_results unreachable")
        return flaky_base(path, params)
    fc = new_origin_cache()
    f1 = read_unwind_origin(wjobs, flaky_rpc, cache=fc)
    f2 = read_unwind_origin(wjobs, flaky_rpc, cache=fc)
    chk("reader: a block whose results failed to read is read again next time, never skipped",
        f1 is None and f2 == want)
    # What was learned about draws on one chain is dropped with the rest on another chain.
    other = {50: [_unw("w2")], 70: [_noseed(2), _unw("w0"), _unw("w1")]}
    dc6 = new_origin_cache()
    read_unwind_origin(wjobs, _fake_rpc(base), cache=dc6)
    chk("reader: draws learned on another chain are forgotten (w1 drawn there, never drawn here)",
        read_unwind_origin(wjobs, _fake_rpc(other, network="other"), cache=dc6)
        == {"noseed_unwound": ["w0", "w1"], "drawn": [], "unknown": []})
    chk("reader: the index must have reached the height read after the listing, not only the one before",
        read_unwind_origin(wjobs, _fake_rpc(base, indexed_to=99), heights=(60, 100)) is None)
    chk("reader: a height that could not be read -> None",
        read_unwind_origin(wjobs, _fake_rpc(base), heights=(None, 100)) is None)
    # A no-seed block can also carry an unwind from a draw: more ids than the loop's count is normal.
    mixed = {10: [_draw("w1")], 51: [_unw("w1")], 70: [_noseed(1), _unw("w0"), _draw("w2"), _unw("w2")]}
    chk("reader: a no-seed block naming more unwound ids than its count is read, not refused",
        read_unwind_origin(wjobs, _fake_rpc(mixed))
        == {"noseed_unwound": ["w0", "w2"], "drawn": ["w2"], "unknown": []})
    chk("ids an event query cannot carry: a quote, a double quote, a backslash, a control character; a space it can",
        all(_unquotable("a" + c + "b") for c in (chr(39), chr(34), chr(92), chr(1), chr(10), chr(127)))
        and not any(_unquotable(i) for i in ("a-1", "job_7", "x.y:z", "a b")))
    chk("reader: no undisputed unwind -> no RPC call needed",
        read_unwind_origin(_jobs([("d", "open+paid+optimistic+disputed", 2)]), _fake_rpc({}, fail=True))
        == empty)
    chk("distinct_voters_min = the minimum over the by-quorum audits only (3, not 4, and not the adjudicated 5)",
        pq["jobs"]["distinct_voters_min"] == 3)
    # Largest-operator share: q1 = 6/10 = 6000 bps (over 50%, so the gate would see it), q3 = 3/10 = 3000.
    # The worst case (the maximum) is exposed, not an average that would hide the captured audit.
    chk("max_operator_share_bps = the maximum share over the by-quorum audits (6000, not 3000)",
        pq["jobs"]["max_operator_share_bps"] == 6000)
    no_q = build_proof([{"id": "n1", "state": "open+paid+optimistic+disputed+resolved+clawed+noquorum",
                         "fee": 1, "slashes": []}], {}, {}, 1, deferred={"job_ids": [], "audits_opened": 1})
    chk("distinct_voters_min = null with no conducted audit (never 0: a false zero would pass the gate backwards)",
        no_q["jobs"]["distinct_voters_min"] is None)
    chk("max_operator_share_bps = null with no conducted audit (0 would claim 'no capture' without measuring)",
        no_q["jobs"]["max_operator_share_bps"] is None)
    # The held section: measured versus not measurable (None is not 0), two pockets (fee and bond),
    # and the age warning (a signal, never a block).
    ph = build_proof([], {}, {}, 1, held={"held_total": 160000, "held_count": 2, "oldest_age_blocks": 900,
                                          "bond_escrowed_total": 50000, "bond_escrowed_count": 1},
                     prune_window_blocks=604800)
    chk("held measured: total/count/age exposed (the two numbers that would contradict 'retained is not lost')",
        ph["held"]["total_udndr"] == 160000 and ph["held"]["count"] == 2 and ph["held"]["oldest_age_blocks"] == 900)
    chk("the escrowed bond is visible (the second pocket - an invisible bond is the archetypal orphaned fund)",
        ph["held"]["bond_escrowed_udndr"] == 50000 and ph["held"]["bond_escrowed_count"] == 1)
    # The third pocket: locked jurors. None is not 0 at field level (the client-side
    # `pockets_measured` sentinel), and `lock_warning` is the strict twin of the held warning, at the
    # same threshold.
    pj = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 15, "oldest_lock_age_blocks": 400000,
                                          "oldest_lock_job_id": "jLock"},
                     prune_window_blocks=604800)
    chk("locked jurors are visible (the third pocket - the evidence for exit-after-vote)",
        pj["held"]["jurors_locked"] == 15 and pj["held"]["oldest_lock_age_blocks"] == 400000
        and pj["held"]["oldest_lock_job_id"] == "jLock")
    chk("lock below the threshold -> lock_warning False (a measurement, not silence)",
        pj["held"]["lock_warning"] is False)
    chk("pocket absent -> None, never a false zero (ph has no lock fields: not measurable)",
        ph["held"]["jurors_locked"] is None and ph["held"]["lock_warning"] is None)
    pl = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 3, "oldest_lock_age_blocks": 604801,
                                          "oldest_lock_job_id": "jOld"},
                     prune_window_blocks=604800)
    chk("lock beyond the threshold -> lock_warning True while age_warning sleeps - the two faces are "
        "independent (small jobs, large committees: a silent held no longer hides the lock)",
        pl["held"]["lock_warning"] is True and pl["held"]["age_warning"] is False)
    pz = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 0, "oldest_lock_age_blocks": 0,
                                          "oldest_lock_job_id": ""},
                     prune_window_blocks=604800)
    chk("a present zero is a measured zero (0, not None) - the sentinel is what makes the claim verifiable",
        pz["held"]["jurors_locked"] == 0 and pz["held"]["lock_warning"] is False)
    pt = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 1, "oldest_lock_age_blocks": 999999,
                                          "oldest_lock_job_id": "jT"},
                     prune_window_blocks=None)
    chk("threshold not measurable -> lock_warning False, never invented (same rule as the held warning)",
        pt["held"]["lock_warning"] is False)
    chk("age warning silent below the threshold (900 <= 604800)",
        ph["held"]["age_warning"] is False)
    pw = build_proof([], {}, {}, 1, held={"held_total": 1, "held_count": 1, "oldest_age_blocks": 604801},
                     prune_window_blocks=604800)
    chk("age warning raised beyond miner_prune_blocks (older than the eviction of a dead miner)",
        pw["held"]["age_warning"] is True)
    pn = build_proof([], {}, {}, 1, held={"held_total": 1, "held_count": 1, "oldest_age_blocks": 999999},
                     prune_window_blocks=None)
    chk("threshold not measurable (params unreadable) -> warning never invented (False, null threshold)",
        pn["held"]["age_warning"] is False and pn["held"]["age_warning_threshold_blocks"] is None)
    chk("held = null when the query is missing (older binary) - never invented zeros",
        p["held"] is None)
    chk("slash events=1 total=320000", p["slashes"]["events"] == 1 and p["slashes"]["total_udndr"] == 320000)
    chk("recent_audits capped at 2", len(p["recent_audits"]) == 2)
    chk("height + vrf + pools present", p["height"] == 1234 and p["vrf"]["contributors"] == 2 and p["pools"]["treasury"] == 7)
    chk("payload is JSON-serializable", bool(json.dumps(p)))
    chk("a failed seed-health query is published as null, never as an empty object of zeros",
        build_proof([], {}, None, height=9)["vrf"] is None)

    # -- The age of the last block ---------------------------------------------------------------
    # A stopped chain must not look alive behind a fresh `generated_at` and a plausible `height`.
    live = build_proof([], {}, {}, height=9, block_epoch=1000, now=1030)["chain"]
    chk("live chain: the age of the last block is published", live["last_block_age_s"] == 30)
    chk("live chain: stale is False - a measurement, not silence", live["stale"] is False)
    chk("the top-level height is still served (a field that moves is a field a reader stops finding)",
        live["height"] == 9)

    dead = build_proof([], {}, {}, height=9, block_epoch=1000, now=1000 + 24128)["chain"]
    chk("stopped chain: the real outage measured on this network reads as stale", dead["stale"] is True)
    chk("stopped chain: the exact age is published, not just a flag", dead["last_block_age_s"] == 24128)

    # The threshold applies at equality: a strict comparison would let through the very value it must refuse.
    at = build_proof([], {}, {}, height=9, block_epoch=1000, now=1000 + STALE_BLOCK_S)["chain"]
    chk("at the threshold exactly: already stale", at["stale"] is True)
    under = build_proof([], {}, {}, height=9, block_epoch=1000, now=999 + STALE_BLOCK_S)["chain"]
    chk("one second under the threshold: not stale", under["stale"] is False)

    # A last block dated ahead of this host. Inside the threshold it is ordinary clock skew: the age is
    # published as measured, negative, and the chain is not stale. At the threshold or beyond, this clock
    # disagrees with the chain and both fields are null, never "0 s old, not stale".
    skew = build_proof([], {}, {}, height=9, block_epoch=1000, now=998)["chain"]
    chk("block 2 s ahead of this clock: the age is published as measured (-2), never clamped to 0",
        skew["last_block_age_s"] == -2)
    chk("block 2 s ahead of this clock: not stale (ordinary skew)", skew["stale"] is False)
    edge = build_proof([], {}, {}, height=9, block_epoch=1001 + STALE_BLOCK_S - 2, now=1000)["chain"]
    chk("one second inside the bound, ahead: still judged, age as measured, not stale",
        edge["last_block_age_s"] == 1 - STALE_BLOCK_S and edge["stale"] is False)
    ahead = build_proof([], {}, {}, height=9, block_epoch=1000 + STALE_BLOCK_S, now=1000)["chain"]
    chk("block dated a full threshold ahead: this clock disagrees -- the age is null, never 0",
        ahead["last_block_age_s"] is None)
    chk("block dated a full threshold ahead: stale is null, never False", ahead["stale"] is None)
    chk("block dated a full threshold ahead: the block's own date is still published",
        ahead["last_block_epoch"] == 1000 + STALE_BLOCK_S)

    unk = build_proof([], {}, {}, height=9, block_epoch=None, now=1000)["chain"]
    chk("unreadable timestamp: the age is null, never an invented zero",
        unk["last_block_age_s"] is None)
    # The key case: False here would answer "the chain is fine" without having looked, on the one field a
    # reader consults to know whether the network runs.
    chk("unreadable timestamp: stale is null, never False", unk["stale"] is None)
    chk("the threshold used is published next to the verdict",
        unk["stale_threshold_s"] == STALE_BLOCK_S)

    # A slow build: on the live network the reads after the status take a minute. The age is the one at
    # the status read, never inflated by the build's own duration.
    slow = build_proof([], {}, {}, height=9, block_epoch=1000, read_at=1003, now=1063)
    chk("slow build: the age is the one at the status read (3 s), never the build's minute (63 s)",
        slow["chain"]["last_block_age_s"] == 3 and slow["chain"]["stale"] is False)
    chk("slow build: the reading and the document are dated apart (read_at 1003, generated_at 1063)",
        slow["chain"]["read_at"] == 1003 and slow["generated_at"] == 1063)
    stopped = build_proof([], {}, {}, height=9, block_epoch=1000, read_at=1000 + STALE_BLOCK_S,
                          now=1060 + STALE_BLOCK_S)["chain"]
    chk("a chain already stopped at the read is stale, with its age at the read",
        stopped["stale"] is True and stopped["last_block_age_s"] == STALE_BLOCK_S)
    near = build_proof([], {}, {}, height=9, block_epoch=1000, read_at=999 + STALE_BLOCK_S,
                       now=1060 + STALE_BLOCK_S)["chain"]
    chk("one second under the threshold at the read: not stale, even when the build ends past it",
        near["stale"] is False and near["last_block_age_s"] == STALE_BLOCK_S - 1)
    ahead_read = build_proof([], {}, {}, height=9, block_epoch=1000 + STALE_BLOCK_S, read_at=1000,
                             now=1060 + STALE_BLOCK_S)["chain"]
    chk("a block a full threshold ahead of the read: null, even when the end of the build caught up",
        ahead_read["last_block_age_s"] is None and ahead_read["stale"] is None)
    unk_read = build_proof([], {}, {}, height=9, block_epoch=None, read_at=1003, now=1063)["chain"]
    chk("a status read with no timestamp: the read is dated, the age and the verdict are null",
        unk_read["read_at"] == 1003 and unk_read["last_block_age_s"] is None and unk_read["stale"] is None)
    chk("no read_at given: the status was read at now, and read_at says so",
        live["read_at"] == 1030)

    # -- An unread listing is not an empty network ----------------------------------------------
    try:
        build_proof(None, {}, {}, height=9)
        chk("jobs=None raises -- a payload is never built from an unread listing", False)
    except ValueError:
        chk("jobs=None raises -- a payload is never built from an unread listing", True)
    chk("an empty listing is a measurement: zero jobs, published",
        build_proof([], {}, {}, height=9)["jobs"]["total"] == 0)

    def _inputs(jobs_):
        return lambda: {"jobs": jobs_, "pools": {}, "seed_health": {}, "height": 9}

    def _unread():
        raise Unmeasured("list-job unreadable or incomplete")

    cache = {}
    first = publish(cache, 1000, _inputs(jobs))
    good = json.loads(cache["payload"])
    chk("a successful refresh is published and marked current",
        first is True and good["jobs"]["total"] == 5 and good["refresh"]["current"] is True
        and good["refresh"]["failing_since"] is None and good["refresh"]["reason"] is None)
    kept = publish(cache, 1030, _unread)
    served = json.loads(cache["payload"])
    chk("an unread listing keeps the last snapshot (5 jobs), never zeros",
        kept is False and served["jobs"]["total"] == 5 and served["generated_at"] == good["generated_at"])
    chk("... and says it: current=false, the reason, and when the failures began",
        served["refresh"]["current"] is False and served["refresh"]["failing_since"] == 1030
        and served["refresh"]["reason"] == "list-job unreadable or incomplete"
        and served["refresh"]["snapshot_generated_at"] == good["generated_at"])
    publish(cache, 1060, _unread)
    chk("a second failure keeps the start of the outage, not the latest attempt",
        json.loads(cache["payload"])["refresh"]["failing_since"] == 1030
        and json.loads(cache["payload"])["refresh"]["attempted_at"] == 1060)
    publish(cache, 1090, _inputs([]))
    healed = json.loads(cache["payload"])
    chk("a later empty listing that was read is published as zero, and the outage mark is cleared",
        healed["jobs"]["total"] == 0 and healed["refresh"]["current"] is True
        and healed["refresh"]["failing_since"] is None)
    blank = {}
    publish(blank, 5, _unread)
    nothing = json.loads(blank["payload"])
    chk("never built: no counts at all (absent, not zero), and the reason is served",
        "jobs" not in nothing and nothing["refresh"]["current"] is False
        and nothing["refresh"]["snapshot_generated_at"] is None)

    def _boom():
        raise OSError("connection refused by node.example:26657")

    publish(blank, 6, _boom)
    chk("an unexpected failure serves its type only, never the message (hosts, paths, commands)",
        json.loads(blank["payload"])["refresh"]["reason"] == "refresh failed (OSError)")

    # -- A kept snapshot says nothing about now -------------------------------------------------
    dated = {}
    publish(dated, 2000, lambda: {"jobs": jobs, "pools": {}, "seed_health": {}, "height": 100,
                                  "block_epoch": 1995, "now": 2000})
    built_chain = json.loads(dated["payload"])["chain"]
    publish(dated, 2030, _unread)
    kept_chain = json.loads(dated["payload"])["chain"]
    chk("a kept snapshot withdraws its verdicts about now: stale and age null, never the 'live' of its build",
        built_chain["stale"] is False and built_chain["last_block_age_s"] == 5
        and kept_chain["stale"] is None and kept_chain["last_block_age_s"] is None)
    chk("... and keeps its facts: height and the epoch of its last block",
        kept_chain["height"] == 100 and kept_chain["last_block_epoch"] == 1995
        and dated["doc"]["chain"]["stale"] is False)

    # A refresh that takes a minute: the age is the one at the status read, the duration is published,
    # and a later failure still withdraws the verdicts while keeping the date of the reading.
    ticks = iter([50.0, 110.4, 200.0, 203.0])
    timed = {}
    publish(timed, 2000, lambda: {"jobs": jobs, "pools": {}, "seed_health": {}, "height": 100,
                                  "block_epoch": 1998, "read_at": 2001, "now": 2060},
            clock=lambda: next(ticks))
    slow_doc = json.loads(timed["payload"])
    chk("a 60 s refresh publishes the age at the read (3 s), its read_at, and build_s 60.4",
        slow_doc["chain"]["last_block_age_s"] == 3 and slow_doc["chain"]["stale"] is False
        and slow_doc["chain"]["read_at"] == 2001 and slow_doc["generated_at"] == 2060
        and slow_doc["refresh"]["build_s"] == 60.4)
    publish(timed, 2100, _unread, clock=lambda: next(ticks))
    slow_kept = json.loads(timed["payload"])
    chk("... kept after a failure: age and stale null, read_at kept, build_s of the failed attempt",
        slow_kept["chain"]["last_block_age_s"] is None and slow_kept["chain"]["stale"] is None
        and slow_kept["chain"]["read_at"] == 2001 and slow_kept["refresh"]["build_s"] == 3.0)
    unbuilt = json.loads(new_cache()["payload"])
    chk("before the first refresh completes the payload is marked (current=false), never {}",
        unbuilt["refresh"]["current"] is False and unbuilt["refresh"]["reason"] == NOT_YET_BUILT
        and "jobs" not in unbuilt and unbuilt["refresh"]["build_s"] is None)

    # The gap after a build is counted from its end: max(interval, 4 x its duration); a build longer than
    # the interval raises the alert, a shorter one does not, and without an interval nothing is claimed.
    def _ok_read():
        return {"jobs": jobs, "pools": {}, "seed_health": {}, "height": 100, "block_epoch": 1998,
                "read_at": 2001, "now": 2060}
    for took, gap, alert in ((2.0, 15.0, False), (3.8, 15.2, False), (40.0, 160.0, True)):
        pace_ticks = iter([0.0, took])
        paced = {}
        publish(paced, 2000, _ok_read, clock=lambda: next(pace_ticks), refresh_s=15.0)
        pr = json.loads(paced["payload"])["refresh"]
        chk(f"a {took} s build against a 15 s interval: gap {gap} s, alert {alert}, gap left for the loop",
            pr["gap_s"] == gap and pr["slow_build"] is alert and paced["gap_s"] == gap)
    pace_ticks = iter([0.0, 30.0])
    failed = {}
    publish(failed, 2000, _unread, clock=lambda: next(pace_ticks), refresh_s=15.0)
    pr = json.loads(failed["payload"])["refresh"]
    chk("a failed 30 s attempt backs off like a built one: gap 120 s, alert",
        pr["current"] is False and pr["gap_s"] == 120.0 and pr["slow_build"] is True)
    pr = json.loads(timed["payload"])["refresh"]
    chk("without an interval the pace is unknown: null, never 0", pr["gap_s"] is None and pr["slow_build"] is None)

    # -- Origin of the jobs, and how work and verdicts are spread ---------------------------------
    oj = [{"id": "o1", "state": "open", "miner_id": "m1", "client": "dendra1gen"},
          {"id": "o2", "state": "open", "miner_id": "m1", "client": "dendra1gen"},
          {"id": "o3", "state": "open", "miner_id": "m2", "client": "dendra1gw"},
          {"id": "o4", "state": "open", "miner_id": "m3", "client": "dendra1ext"},
          {"id": "o5", "state": "open", "miner_id": "", "client": ""}]
    og = build_proof(oj, {}, {}, 77, season_generator="dendra1gen", gateway_address="dendra1gw")["origin"]
    chk("origin: 2 generator + 1 gateway + 2 others = 5, partition_ok, read at height 77",
        og["season_generator"] == 2 and og["chat_gateway"] == 1 and og["other_clients"] == 2
        and og["partition_ok"] is True and og["total"] == 5 and og["read_at_height"] == 77
        and og["unmeasured_reason"] is None)
    unread_h = build_proof(oj, {}, {}, None, season_generator="dendra1gen", gateway_address="dendra1gw")
    chk("origin and transparency: an unknown height reads null, never 0 (read_at_height 0 would pass for a reading)",
        unread_h["origin"]["read_at_height"] is None and unread_h["transparency"]["read_at_height"] is None
        and og["read_at_height"] == 77)
    both = build_proof(oj, {}, {}, 77, season_generator="dendra1gen", gateway_address="dendra1gen")["origin"]
    chk("origin: one address given to both sources double counts, the sum is not the total, partition_ok false",
        both["partition_ok"] is False)
    nogen = build_proof(oj, {}, {}, 77, gateway_address="dendra1gw")["origin"]
    chk("origin: generator address unread -> its count and the rest are null (not 0), partition_ok null, reason said",
        nogen["season_generator"] is None and nogen["other_clients"] is None and nogen["chat_gateway"] == 1
        and nogen["partition_ok"] is None and "generator" in nogen["unmeasured_reason"])
    noaddr = build_proof(oj, {}, {}, 77, season_generator="", gateway_address=None)["origin"]
    chk("origin: an empty address is no address (null counts), never a match on empty clients",
        noaddr["season_generator"] is None and noaddr["chat_gateway"] is None and noaddr["partition_ok"] is None)
    noclient = build_proof([{"id": "x", "state": "open"}], {}, {}, 1, season_generator="a", gateway_address="b")["origin"]
    chk("origin: a row with no client field makes the origin unmeasured, never a count of 'others'",
        noclient["partition_ok"] is None and noclient["other_clients"] is None and "client" in noclient["unmeasured_reason"])

    miners = [{"id": "m1", "operator": "opA"}, {"id": "m2", "operator": "opA"}, {"id": "m3", "operator": "opB"}]
    commits = [{"key": "o1", "model_id": "mod-x"}, {"key": "o2", "model_id": "mod-x"}, {"key": "o4", "model_id": ""},
               {"key": "o1__verdict__m2", "model_id": "judge-1"}, {"key": "o2__verdict__m2", "model_id": "judge-1"},
               {"key": "o1__verdict__m3", "model_id": "judge-1"}, {"key": "epoch-marker", "model_id": ""}]
    tr = build_proof(oj, {}, {}, 90, miners=miners, commits=commits,
                     audit_timing={"audits": 4, "timed": 3, "median_blocks": 12})["transparency"]
    chk("transparency: jobs by miner m1=2 of 4 served (5000 bps), by operator opA=3 of 4 (7500 bps)",
        tr["jobs_by_miner"]["total"] == 4 and tr["jobs_by_miner"]["top"][0] == {"id": "m1", "count": 2, "share_bps": 5000}
        and tr["jobs_by_operator"]["top"][0] == {"id": "opA", "count": 3, "share_bps": 7500})
    chk("transparency: 3 verdicts, m2 posted 2 (6666 bps), operator opA 2 and opB 1",
        tr["verdicts_by_judge"]["total"] == 3 and tr["verdicts_by_judge"]["top_share_bps"] == 6666
        and tr["verdicts_by_operator"]["distinct"] == 2)
    chk("transparency: requests by model counts the commit of each job; an unnamed model has its own bucket; "
        "a commit that is no job nor verdict counts for nothing",
        tr["requests_by_model"]["total"] == 3 and {r["id"] for r in tr["requests_by_model"]["top"]} == {"mod-x", "(none named)"})
    chk("transparency: dispute to verdict is blocks, with how many audits it covers, read at height 90",
        tr["dispute_to_verdict"] == {"audits": 4, "timed": 3, "median_blocks": 12, "unit": "blocks"}
        and tr["read_at_height"] == 90)
    tn = build_proof(oj, {}, {}, 90)["transparency"]
    chk("transparency: unread registry, commits and timing leave their parts null (jobs by miner stays: it needs the jobs only)",
        tn["jobs_by_operator"] is None and tn["verdicts_by_judge"] is None and tn["requests_by_model"] is None
        and tn["dispute_to_verdict"] is None and tn["jobs_by_miner"]["total"] == 4)
    te = build_proof(oj, {}, {}, 90, miners=miners, commits=[])["transparency"]
    chk("transparency: an empty commit list is a reading of nothing: no verdicts, no share (null), not a failure",
        te["verdicts_by_judge"]["total"] == 0 and te["verdicts_by_judge"]["top_share_bps"] is None)

    # -- Outcomes of the opened audits, the Nakamoto coefficient and an unread height ------------------
    ao = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 450},
                     unwind_origin=no_origin_needed)["audit_outcomes"]
    chk("outcomes: 118 in progress, 332 refunded with no verdict (251 + 81), the sum is checked against 450",
        ao["in_progress"] == 118 and ao["refunded_no_verdict"] == 332 and ao["verified"] == 0
        and ao["no_quorum"] == 0 and ao["unknown"] == 0 and ao["sum"] == 450 and ao["sum_ok"] is True)
    ah = build_proof(_jobs(live_mix), {}, {}, 1, deferred={"job_ids": [], "audits_opened": 451},
                     unwind_origin=no_origin_needed)["audit_outcomes"]
    chk("outcomes: a sum that differs from audits_opened publishes no count (null), with its reason",
        ah["sum_ok"] is False and ah["sum"] == 450 and ah["verified"] is None and ah["in_progress"] is None
        and ah["unmeasured_reason"] == "the classes do not add up to audits_opened")
    chk("outcomes: without the chain's counter nothing is counted",
        build_proof(_jobs(live_mix), {}, {}, 1)["audit_outcomes"]["sum_ok"] is None
        and build_proof(_jobs(live_mix), {}, {}, 1)["audit_outcomes"]["verified"] is None)
    chk("nakamoto: the fewest holders above half of the total, in the unit it is told in",
        nakamoto({"a": 5, "b": 3, "c": 2}, 5000) == 2 and nakamoto({"a": 6, "b": 2, "c": 2}, 5000) == 1
        and nakamoto({"a": 5, "b": 5}, 5000) == 2 and nakamoto({}, 5000) is None)
    cc = _concentration({"a": 6, "b": 2, "c": 2}, 10, "operator addresses")
    chk("concentration: the coefficient carries its unit and its threshold",
        cc["nakamoto"] == 1 and cc["nakamoto_unit"] == "operator addresses" and cc["nakamoto_over_bps"] == 5000)
    chk("an unread height is null in the payload, never 0",
        build_proof(jobs, {}, {}, 0)["height"] is None and build_proof(jobs, {}, {}, None)["chain"]["height"] is None
        and build_proof(jobs, {}, {}, 5)["height"] == 5)

    print("selftest the-proof", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # Before the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base64
import urllib.parse
import urllib.request

import client as dc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DENDRA_PROOF_HOST", "127.0.0.1")  # public = reverse proxy in front (no bare 0.0.0.0 bind)
PORT = int(os.environ.get("DENDRA_PROOF_PORT", "8090"))
CORS = os.environ.get("DENDRA_PROOF_CORS", "")           # comma-separated origins, or "*" (empty = no header)
REFRESH_S_MIN, REFRESH_S_MAX = 1, 86400
CHAIN_REFRESH_S_MIN, CHAIN_REFRESH_S_MAX = 5, 300


def _read_seconds(raw, name, low, high):
    """`raw` as a finite number of seconds with low <= v <= high, or SystemExit naming `name`. A value is
    refused, never replaced by a default: `nan` would be written into the JSON as a bare token and fails
    both comparisons, `inf` fails the upper one."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = None
    if v is None or not low <= v <= high:
        raise SystemExit(f"[the-proof] {name}={raw!r} is refused: "
                         f"it must be a number of seconds with {low} <= value <= {high}")
    return v


def read_refresh_s(raw):
    """The refresh interval in seconds from the text of DENDRA_PROOF_REFRESH_S, or SystemExit with a message.
    Bounded below so that builds, which walk every listing of the chain, never run back to back, and above
    so that the wait cannot overflow."""
    return _read_seconds(raw, "DENDRA_PROOF_REFRESH_S", REFRESH_S_MIN, REFRESH_S_MAX)


def read_chain_refresh_s(raw):
    """The interval of the light chain reading in seconds from the text of DENDRA_PROOF_CHAIN_REFRESH_S, or
    SystemExit with a message. One status read per interval; the threshold it is checked against is below."""
    return _read_seconds(raw, "DENDRA_PROOF_CHAIN_REFRESH_S", CHAIN_REFRESH_S_MIN, CHAIN_REFRESH_S_MAX)


REFRESH_S = read_refresh_s(os.environ.get("DENDRA_PROOF_REFRESH_S", "15"))
CHAIN_REFRESH_S = read_chain_refresh_s(os.environ.get("DENDRA_PROOF_CHAIN_REFRESH_S", "30"))
# A reading must be able to wait one more interval and still be younger than the threshold at which the
# payload is served without its verdicts; below that the feed would withdraw them between two readings.
if 2 * CHAIN_REFRESH_S > STALE_BLOCK_S:
    raise SystemExit(f"[the-proof] DENDRA_PROOF_CHAIN_REFRESH_S={CHAIN_REFRESH_S:g} is refused: twice the "
                     f"interval must not exceed DENDRA_PROOF_STALE_S={STALE_BLOCK_S}")
# The CometBFT RPC of the node the CLI reads (DENDRA_NODE, tcp:// -> http://), for the block events.
RPC_URL = (os.environ.get("DENDRA_PROOF_RPC")
           or (dc.NODE.replace("tcp://", "http://", 1) if dc.NODE else "http://127.0.0.1:26657")).rstrip("/")
# Written by the builder thread (the whole payload) and the chain thread (its `chain` section only), under
# the cache's own lock; read by every request. A read that keeps failing is retried at the gap in force,
# never on every request -- each attempt walks the whole job listing.
_CACHE = new_cache()
_REFRESH_LOCK = threading.Lock()
_STOP = threading.Event()
_BUILDER = None
_CHAIN_READER = None
_CHAIN_FAILING = [False]  # whether the last light reading failed, so that a long outage logs once
_NOW = time.time  # the clock a served payload is aged against; a bench replaces it
# What the event reader learned, kept across refreshes (see new_origin_cache).
_ORIGIN_CACHE = new_origin_cache()
_rpc_get = rpc_reader(RPC_URL)

# Who asked for the jobs (`origin_partition`). The season generator's address is read from the season
# service's status, field `generator`; the chat gateway's address is set by the operator, since no service
# this feed can read publishes it. Either one unread leaves its counts null.
SEASON_STATUS = os.environ.get("DENDRA_PROOF_SEASON_STATUS", "http://final-season:8093/final-season/v1/status")
GATEWAY_ADDRESS = os.environ.get("DENDRA_PROOF_GATEWAY_ADDRESS", "")
# Audits timed per refresh: each one costs a job read and an event search, so the median fills in over
# several refreshes and says how many audits it covers.
TIMING_BUDGET = int(os.environ.get("DENDRA_PROOF_TIMING_BUDGET", "12"))
# job id -> blocks from the height the audit opened to the height that resolved it; the ids with no such
# pair (a resolution that left no per-job record) are skipped for good. Heights of a chain never change,
# so a height below the last one seen means another chain and clears both.
#
# Memory only: after a restart every resolved audit is timed again, TIMING_BUDGET of them per build (one
# job read and one event search each), so the first builds are slower until the set is covered.
_TIMING = {"height": 0, "done": {}, "skipped": set()}


def _read_season_generator():
    """The generator's address from the season status, or None when the status could not be read or has none."""
    try:
        with urllib.request.urlopen(SEASON_STATUS, timeout=5) as r:
            g = json.loads(r.read(65536).decode())["generator"]
    except Exception:  # noqa: BLE001 -- unread is None, never an address
        return None
    return g if isinstance(g, str) and g else None


def _read_miners():
    """[{id, operator}] or None when the registry could not be read."""
    rows = dc.list_all("list-miner", "miner")
    if rows is None or any(not isinstance(m, dict) for m in rows):
        return None
    return [{"id": dc._g(m, "minerId", "miner_id"), "operator": dc._g(m, "operator")} for m in rows]


COMMIT_PAGE = 500          # rows per page of a long read
COMMIT_MAX_PAGES = 400     # as client.list_all: a longer listing is refused, not cut
_COMMITS = new_commit_cache()


def _commit_query(limit, key=None, offset=None, count_total=False):
    """One page of the commit listing as the node answers it, or None. `key` is the base64 page key,
    `offset` a row offset, `count_total` asks for the number of rows. On the CLI path (DENDRA_CHAIN_READ=cli)
    the same query is made with the flags of the listing command."""
    if dc._use_rest():
        q = f"{dc._JOBS}commit?pagination.limit={limit}"
        if key:
            q += "&pagination.key=" + urllib.parse.quote(key, safe="")   # base64 holds + / =
        if offset is not None:
            q += f"&pagination.offset={offset}"
        if count_total:
            q += "&pagination.count_total=true"
        return dc.rest_json(q, 180)
    cmd = ["dendrad", "query", "jobs", "list-commit", "--page-limit", str(limit), "--output", "json", *dc._node()]
    if key:
        cmd += ["--page-key", key]
    if offset is not None:
        cmd += ["--page-offset", str(offset)]
    if count_total:
        cmd += ["--page-count-total"]
    return dc.read_json(cmd, 180)


def _commit_page(d):
    """([{key, model_id}], next page key or None) of one answer, or None when its shape is not a page of
    commits. An answer with no list is a page of zero rows: proto3 omits an empty repeated field."""
    rows = d["commit"] if "commit" in d else d.get("Commit")
    if rows is None:
        rows = []
    pg = d.get("pagination")
    pg = {} if pg is None else pg
    if not isinstance(rows, list) or not isinstance(pg, dict):
        return None
    nxt = pg.get("next_key") or pg.get("nextKey")
    if nxt and not isinstance(nxt, str):
        return None
    out = []
    for c in rows:
        if not isinstance(c, dict):
            return None
        key, model = dc._g(c, "jobId", "job_id"), dc._g(c, "modelId", "model_id")
        if not isinstance(key, str) or not key or not isinstance(model, str):
            return None
        out.append({"key": key, "model_id": model})
    return out, (nxt or None)


def _count_of(raw):
    """A non-negative whole count from a JSON number or a numeric string, else None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    return None


def _commit_total():
    """How many commits the chain holds, or None when the node did not say. An answer without the count is
    unknown, not zero."""
    d = _commit_query(1, count_total=True)
    pg = d.get("pagination") if isinstance(d, dict) else None
    return _count_of(pg.get("total")) if isinstance(pg, dict) else None


def _commit_row_at(i):
    """The commit at offset `i` of the listing, False when the listing has none there, None when the node
    could not be read."""
    d = _commit_query(1, offset=i)
    page = _commit_page(d) if isinstance(d, dict) else None
    if page is None:
        return None
    return page[0][0] if page[0] else False


def _commit_rows_from(start_key, max_rows):
    """The commits from `start_key` (None = the first; the key itself included) to the end of the listing,
    or the first `max_rows` of them, or None when a page could not be read (never a shorter list)."""
    out, pages = [], 0
    key = None if start_key is None else base64.b64encode(start_key.encode()).decode()
    while pages < COMMIT_MAX_PAGES:
        want = COMMIT_PAGE if max_rows is None else min(COMMIT_PAGE, max_rows - len(out))
        if want <= 0:
            return out
        d = _commit_query(want, key=key)
        page = _commit_page(d) if isinstance(d, dict) else None
        if page is None:
            return None
        out.extend(page[0])
        key = page[1]
        pages += 1
        if not key or (max_rows is not None and len(out) >= max_rows):
            return out[:max_rows] if max_rows is not None else out
    sys.stderr.write(f"[the-proof] list-commit: more than {COMMIT_MAX_PAGES} pages -> incomplete read, refusing\n")
    return None


def _read_commits():
    """[{key, model_id}] or None when the commits could not be read. Only the rows the chain gained since
    the last build are read (`read_commits_by_increment`); the full listing is read again when the cache
    cannot be trusted, and every COMMIT_FULL_EVERY builds."""
    rows, how = read_commits_by_increment(_COMMITS, _commit_total, _commit_row_at, _commit_rows_from,
                                          COMMIT_WINDOW, COMMIT_PATCHES, COMMIT_FULL_EVERY)
    if how == "unread":
        sys.stderr.write("[the-proof] list-commit: unreadable, no commit figure this build\n")
    return rows


def _read_audit_timing(jobs, height):
    """{audits, timed, median_blocks} over the audits a jury or an adjudication concluded, or None.

    `audits` counts them all and `timed` the ones whose two heights were read, so the reader sees how much
    of the set the median covers. The opening height is the job's `dispute_height` and the closing one the
    height of its resolution (final_season_chain.audit_resolution). Even counts take the lower middle."""
    import final_season_chain as fsc
    ids = [j["id"] for j in jobs if audit_class(state_markers(j["state"])) in ("resolved_by_quorum",
                                                                              "resolved_by_adjudication")]
    # A height that was not read (0, see `_known_height`) is below every height seen: it would look like
    # another chain and empty what was timed. It neither clears the record nor dates it.
    if _known_height(height) is not None:
        if height < _TIMING["height"]:
            _TIMING["done"].clear()
            _TIMING["skipped"].clear()
        _TIMING["height"] = height
    budget = TIMING_BUDGET
    for jid in ids:
        if budget <= 0:
            break
        if jid in _TIMING["done"] or jid in _TIMING["skipped"]:
            continue
        budget -= 1
        try:
            d = dc.read_json(["dendrad", "query", "jobs", "get-job", jid, "--output", "json", *dc._node()], 60)
            if d is None:
                continue
            job = d["job"] if "job" in d else d
            opened = int(dc._g(job, "disputeHeight", "dispute_height", default=0) or 0)
            closed = fsc.audit_resolution(fsc.default_node(), RPC_URL, jid)
        except Exception:  # noqa: BLE001 -- not read this time, read again at the next refresh
            continue
        if opened > 0 and closed is not None and closed[0] >= opened:
            _TIMING["done"][jid] = closed[0] - opened
        else:
            _TIMING["skipped"].add(jid)
    timed = sorted(_TIMING["done"][j] for j in ids if j in _TIMING["done"])
    return {"audits": len(ids), "timed": len(timed),
            "median_blocks": timed[(len(timed) - 1) // 2] if timed else None}


def _read_inputs():
    """The chain reads `build_proof` needs. Raises `Unmeasured` when the job listing is unreadable:
    `dc.list_jobs_full()` answers None then, and None is not an empty network.

    The listing, the opening counter and the block events are separate reads, and an audit that opens or
    unwinds between them makes them disagree for one refresh with no state hiding. The counter is read
    before the listing and after the event search, and the event search stops at the height read after
    the listing. When something moved, the reads are made once more; a second move publishes the
    partition as null with its reason, never as false."""
    for _attempt in range(2):
        before = dc.audit_deferred()
        h_listed = dc.head()[0]
        jobs = dc.list_jobs_full()
        if jobs is None:
            raise Unmeasured("list-job unreadable or incomplete")
        # One status read yields both: pairing a height with a timestamp fetched separately would
        # produce the age of neither.
        height, block_epoch = dc.head()
        # Taken once the status has answered, so the age it dates can only be overstated by the read itself.
        read_at = int(time.time())
        # None when the block events cannot be read -> the unwound classes and partition_ok are null
        origin = read_unwind_origin(jobs, _rpc_get, heights=(h_listed, height), cache=_ORIGIN_CACHE)
        # None when the query does not exist (older binary) -> null fields, never 0
        deferred = dc.audit_deferred()
        moved = chain_moved(before, deferred)
        # Read again only after a move; an unread counter is reported as such, not retried here.
        if moved is not True:
            break
    return {"jobs": jobs, "pools": dc.pools(), "seed_health": dc.committee_seed_health(),
            "height": height, "block_epoch": block_epoch, "read_at": read_at, "deferred": deferred,
            "unwind_origin": origin, "chain_moved": moved,
            # same: "retained is not lost" is only measurable on a recent binary
            "held": dc.held_summary(),
            # age-warning threshold; None when the params are unreadable
            "prune_window_blocks": dc.prune_window_blocks(),
            # Each None when its read failed: the parts drawn from it are then null in the payload.
            "season_generator": _read_season_generator(),
            "gateway_address": GATEWAY_ADDRESS or None,
            "miners": _read_miners(), "commits": _read_commits(),
            "audit_timing": _read_audit_timing(jobs, height)}


def _refresh():
    """One build, now, and publish it. The builder thread is the only caller in production; the lock is
    kept so that a second caller (a bench, a future hook) skips instead of walking the job listing in
    parallel. The pace between builds belongs to `build_loop`, not to this function."""
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        publish(_CACHE, time.time(), _read_inputs, refresh_s=REFRESH_S)
    finally:
        _REFRESH_LOCK.release()


def _read_chain():
    """The `chain` section from one status read of the node, over the in-process RPC reader of the client
    (never the CLI, whatever the chain-read mode says). Raises `Unmeasured` when the answer has no usable
    height: the light reader publishes a block it measured, never a default one. A status without a block
    time is a reading, and `chain_block` then leaves the verdicts null."""
    doc = dc.rpc_json("/status")
    res = doc.get("result") if isinstance(doc, dict) else None
    sync = res.get("sync_info") if isinstance(res, dict) else None
    if not isinstance(sync, dict):
        raise Unmeasured("node status unreadable")
    try:
        height = int(sync["latest_block_height"])
    except (KeyError, TypeError, ValueError):
        raise Unmeasured("node status has no block height") from None
    if height <= 0:
        raise Unmeasured("node status has no block height")
    # Taken once the status has answered, as in `_read_inputs`.
    return chain_block(height, dc._block_epoch(sync), int(_NOW()))


def _chain_step():
    """One light reading, published. A failure leaves the served `chain` as it was: it ages, and the strip
    in `serve_body` withdraws its verdicts and says so. Logged when it starts failing and when it recovers."""
    try:
        chain = _read_chain()
    except Unmeasured as e:
        if not _CHAIN_FAILING[0]:
            print(f"[the-proof] chain reading: {e}", file=sys.stderr, flush=True)
        _CHAIN_FAILING[0] = True
        return
    if _CHAIN_FAILING[0]:
        print("[the-proof] chain reading: recovered", file=sys.stderr, flush=True)
    _CHAIN_FAILING[0] = False
    publish_chain(_CACHE, chain)


def start_builder():
    """Start the background threads, once; returns the builder thread. The builder publishes the payload;
    the chain thread (`_CHAIN_READER`) keeps its `chain` section current between builds. Requests only
    serve what they published."""
    global _BUILDER, _CHAIN_READER
    if _BUILDER is not None and _BUILDER.is_alive():
        return _BUILDER
    _STOP.clear()
    _CHAIN_READER = threading.Thread(
        target=lambda: build_loop({}, _chain_step, CHAIN_REFRESH_S, _STOP.wait, _STOP.is_set),
        name="the-proof-chain", daemon=True)
    _BUILDER = threading.Thread(
        target=lambda: build_loop(_CACHE, _refresh, REFRESH_S, _STOP.wait, _STOP.is_set),
        name="the-proof-builder", daemon=True)
    _BUILDER.start()
    _CHAIN_READER.start()
    return _BUILDER


def cors_origins(raw):
    """The origins a browser page may read the feed from, parsed from DENDRA_PROOF_CORS: a comma-separated
    list of exact origins, or "*". Pages on several origins read the feed (the site's two hosts, the chat, the
    explorer), and a fixed Access-Control-Allow-Origin names only one, so the others could not read it."""
    return tuple(o.strip() for o in (raw or "").split(",") if o.strip())


def cors_allow(allowed, origin):
    """The Access-Control-Allow-Origin value for a request sent from `origin`, or None for no header. The
    header carries one origin, so a listed origin is echoed back; an origin outside the list gets none."""
    if "*" in allowed:
        return "*"
    if origin and origin in allowed:
        return origin
    return None


CORS_ORIGINS = cors_origins(CORS)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        allow = cors_allow(CORS_ORIGINS, self.headers.get("Origin"))
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
        if CORS_ORIGINS and "*" not in CORS_ORIGINS:
            # The answer depends on Origin: a shared cache must not hand one origin's headers to another.
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/proof", "/"):
            # Never builds: the builder thread publishes, a request reads the last payload.
            self._send(200, serve_body(_CACHE["payload"], _NOW()))
        elif self.path == "/health":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    print(f"[the-proof] read-only facade on http://{HOST}:{PORT}/proof (refresh {REFRESH_S:g}s, chain {CHAIN_REFRESH_S:g}s; CORS {CORS or 'off'})")
    start_builder()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
