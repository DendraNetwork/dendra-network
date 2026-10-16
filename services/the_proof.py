#!/usr/bin/env python3
"""the_proof.py, the Proof feed: a read-only HTTP facade over the real on-chain state, so that the
public site can show a feed nobody staged (audited jobs, slashes, VRF health).

No writes, no secrets, no content: the chain only sees metadata and hashes, and this facade only
exposes counters and public states that anyone can already query over RPC.

  GET /proof   -> JSON (see build_proof); refreshed at most every DENDRA_PROOF_REFRESH_S (default 15 s).
                  `chain.read_at` is the epoch second of the status read the block age is measured at,
                  and `refresh.build_s` the seconds the last refresh took (see `publish`).
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
# Pure core (selftest without chain or network): builds the payload from already-fetched snapshots.
# State markers = on-chain predicates (job_state.go): paid/settled, disputed, resolved, clawed.
# ---------------------------------------------------------------------------


# Past this, a last block stops being proof of life. The value is an order of magnitude, not a derivation:
# no consensus parameter fixes the interval between blocks (it emerges from `timeout_commit` and from
# propagation), so no number on the chain turns into this threshold. It is published in the payload, so a
# reader can apply their own and see which one produced the verdict.
STALE_BLOCK_S = int(os.environ.get("DENDRA_PROOF_STALE_S", "300"))


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
    out = {"read_at_height": None if height is None else int(height), "total": total, "season_generator": None, "chat_gateway": None,
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


def _concentration(counter, top=TOP_ROWS):
    """{total, distinct, top_share_bps, top: [{id, count, share_bps}]} of a counter, largest first. Shares are
    in basis points of the counter's own total; with nothing counted there is no share (null), not 0."""
    whole = sum(counter.values())
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"total": whole, "distinct": len(ranked),
            "top_share_bps": (ranked[0][1] * 10000 // whole) if whole else None,
            "top": [{"id": k, "count": v, "share_bps": v * 10000 // whole} for k, v in ranked[:top]]}


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
    The unit of the last one is blocks: the interval between blocks emerges from the network and varies."""
    out = {"read_at_height": None if height is None else int(height), "jobs_by_miner": None, "jobs_by_operator": None,
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
        return _concentration(ops, top)

    if all("miner_id" in j for j in jobs):
        served = {}
        for j in jobs:
            if j["miner_id"]:
                served[j["miner_id"]] = served.get(j["miner_id"], 0) + 1
        out["jobs_by_miner"] = _concentration(served, top)
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
        out["verdicts_by_judge"] = _concentration(votes, top)
        out["verdicts_by_operator"] = by_operator(votes)
        out["requests_by_model"] = _concentration(models, top)
    if audit_timing is not None:
        out["dispute_to_verdict"] = {"audits": audit_timing["audits"], "timed": audit_timing["timed"],
                                     "median_blocks": audit_timing["median_blocks"], "unit": "blocks"}
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
    _read = _now if read_at is None else int(read_at)
    _age = None if block_epoch is None else _read - int(block_epoch)
    if _age is not None and _age + STALE_BLOCK_S <= 0:
        _age = None
    chain_out = {
        "height": int(height or 0),
        "last_block_epoch": (None if block_epoch is None else int(block_epoch)),
        "last_block_age_s": _age,
        "stale": (None if _age is None else _age >= STALE_BLOCK_S),
        "stale_threshold_s": STALE_BLOCK_S,
        "read_at": _read,
    }
    return {
        "generated_at": _now,
        "height": int(height or 0),
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


def kept_payload(doc):
    """The last snapshot, as it is served after a failed refresh: its facts kept, its verdicts about
    now withdrawn (null = not measured now).

    Served as built, a snapshot taken five seconds after a block would keep answering "new blocks keep
    arriving" (`chain.stale` false, `chain.last_block_age_s` 5) for the whole outage of the node, on the one
    field a reader consults to know whether the chain runs. The kept age cannot be advanced either: a block
    newer than the kept one may exist, so `now - last_block_epoch` only bounds the real age from above. The
    facts (`height`, `chain.last_block_epoch`, `chain.read_at`, every count) stay, dated by
    `refresh.snapshot_generated_at`.
    `doc` is not modified."""
    payload = dict(doc)
    for section, field in NOW_VERDICTS:
        part = dict(payload[section])
        part[field] = None
        payload[section] = part
    return payload


NOT_YET_BUILT = "first refresh not completed"


def new_cache():
    """The served state before any refresh has completed. Its payload is marked, never `{}`: the refresh
    lock does not make a request wait, so a request arriving during the first walk of the job listing is
    served this payload, and an empty object would have no `refresh` field at all. Same shape as a refresh
    that failed with nothing to keep; `attempted_at` is null because no attempt has completed."""
    payload = {"refresh": {"current": False, "attempted_at": None, "failing_since": None,
                           "reason": NOT_YET_BUILT, "snapshot_generated_at": None, "build_s": None}}
    return {"t": 0.0, "payload": json.dumps(payload, ensure_ascii=False).encode(), "doc": None,
            "failing_since": None}


def publish(cache, now, read, clock=None):
    """One refresh of the served payload; True when it was rebuilt. Pure apart from `read()` and `clock`
    (`time.monotonic` when None), which times the attempt.

    `read()` returns the keyword arguments of `build_proof`, or raises. On success the payload is
    rebuilt and marked current. On any failure the last payload built is kept and served, and it says
    so: `refresh.current` is false, with the time the failures began and their reason. Rebuilding from what
    could not be read would publish zeros (no job, no audit, no slash) as if measured, and serving the old
    snapshot without a mark would present it as fresh. A kept snapshot also loses its verdicts about now
    (`kept_payload`), since a reader of `chain.stale` does not read the mark.

    `refresh.current` has no third value: a payload either came from the last attempt or it did not. What
    is not known, because nothing was ever built, shows as the absence of every other field, with
    `snapshot_generated_at` null, never as a payload of zeros (see `new_cache` for the payload served
    before the first refresh completes).

    `refresh.build_s` is how long the attempt dated by `attempted_at` took, failed or not. It grows with
    the number of jobs and commits of the season, since every refresh walks both listings, and a reader
    needs it to know how far apart two readings of the feed can be."""
    clock = time.monotonic if clock is None else clock
    started = clock()
    try:
        doc = build_proof(**read())
        build_s = round(clock() - started, 1)
        payload = dict(doc)
        payload["refresh"] = {"current": True, "attempted_at": int(now), "failing_since": None,
                              "reason": None, "snapshot_generated_at": doc.get("generated_at"),
                              "build_s": build_s}
        body = json.dumps(payload, ensure_ascii=False).encode()
    except Unmeasured as e:
        reason = str(e)
    except Exception as e:  # noqa: BLE001 -- the facade never breaks: it serves the last snapshot
        sys.stderr.write(f"[the-proof] refresh: {type(e).__name__}: {e}\n")
        reason = f"refresh failed ({type(e).__name__})"
    else:
        cache["doc"] = doc
        cache["failing_since"] = None
        cache["payload"] = body
        return True
    if cache.get("failing_since") is None:
        cache["failing_since"] = int(now)
    kept = cache.get("doc")
    payload = kept_payload(kept) if kept is not None else {}
    payload["refresh"] = {"current": False, "attempted_at": int(now),
                          "failing_since": cache["failing_since"], "reason": reason,
                          "snapshot_generated_at": None if kept is None else kept.get("generated_at"),
                          "build_s": round(clock() - started, 1)}
    cache["payload"] = json.dumps(payload, ensure_ascii=False).encode()
    return False


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

    # Partition (ADR-034): the gate counts only the audits closed by quorum, and these cases exercise the
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
    # to return False (ADR-033, question 6).
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

    print("selftest the-proof", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # Before the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import threading
import urllib.request

import client as dc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DENDRA_PROOF_HOST", "127.0.0.1")  # public = reverse proxy in front (no bare 0.0.0.0 bind)
PORT = int(os.environ.get("DENDRA_PROOF_PORT", "8090"))
CORS = os.environ.get("DENDRA_PROOF_CORS", "")           # e.g. https://dendranetwork.com (empty = no header)
REFRESH_S = float(os.environ.get("DENDRA_PROOF_REFRESH_S", "15"))
# The CometBFT RPC of the node the CLI reads (DENDRA_NODE, tcp:// -> http://), for the block events.
RPC_URL = (os.environ.get("DENDRA_PROOF_RPC")
           or (dc.NODE.replace("tcp://", "http://", 1) if dc.NODE else "http://127.0.0.1:26657")).rstrip("/")
# `t` is the last attempt, failed or not: a read that keeps failing is retried at the refresh pace,
# never on every request -- each attempt walks the whole job listing.
_CACHE = new_cache()
_REFRESH_LOCK = threading.Lock()
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


def _read_commits():
    """[{key, model_id}] or None when the commits could not be read."""
    rows = dc.list_all("list-commit", "commit", t=180)
    if rows is None or any(not isinstance(c, dict) for c in rows):
        return None
    return [{"key": dc._g(c, "jobId", "job_id"), "model_id": dc._g(c, "modelId", "model_id")} for c in rows]


def _read_audit_timing(jobs, height):
    """{audits, timed, median_blocks} over the audits a jury or an adjudication concluded, or None.

    `audits` counts them all and `timed` the ones whose two heights were read, so the reader sees how much
    of the set the median covers. The opening height is the job's `dispute_height` and the closing one the
    height of its resolution (final_season_chain.audit_resolution). Even counts take the lower middle."""
    import final_season_chain as fsc
    ids = [j["id"] for j in jobs if audit_class(state_markers(j["state"])) in ("resolved_by_quorum",
                                                                              "resolved_by_adjudication")]
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
    # One refresh at a time: a request arriving while one runs is served what is published, instead
    # of starting a second walk of the job listing in parallel.
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        now = time.time()
        if now - _CACHE["t"] < REFRESH_S:
            return
        _CACHE["t"] = now
        publish(_CACHE, now, _read_inputs)
    finally:
        _REFRESH_LOCK.release()


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
    print(f"[the-proof] read-only facade on http://{HOST}:{PORT}/proof (refresh {REFRESH_S:g}s; CORS {CORS or 'off'})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
