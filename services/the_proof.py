#!/usr/bin/env python3
"""the_proof.py, the Proof feed: a read-only HTTP facade over the real on-chain state, so that the
public site can show a feed nobody staged (audited jobs, slashes, VRF health).

No writes, no secrets, no content: the chain only sees metadata and hashes, and this facade only
exposes counters and public states that anyone can already query over RPC.

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
# Pure core (selftest without chain or network): builds the payload from already-fetched snapshots.
# State markers = on-chain predicates (job_state.go): paid/settled, disputed, resolved, clawed.
# ---------------------------------------------------------------------------


# Past this, a last block stops being proof of life. The value is an order of magnitude, not a derivation:
# no consensus parameter fixes the interval between blocks (it emerges from `timeout_commit` and from
# propagation), so no number on the chain turns into this threshold. It is published in the payload, so a
# reader can apply their own and see which one produced the verdict.
STALE_BLOCK_S = int(os.environ.get("DENDRA_PROOF_STALE_S", "300"))


def build_proof(jobs, pools, seed_health, height, deferred=None, held=None,
                prune_window_blocks=None, max_recent=10, block_epoch=None, now=None):
    """The Proof payload (pure). `jobs` = list_jobs_full() (id/state/miner_id/fee/audit_*/slashes).
    `deferred` = client.audit_deferred(): {"job_ids": [...], "audits_opened": N}, measured
    on-chain. `held` = client.held_summary(): {"held_total", "held_count",
    "oldest_age_blocks", "bond_escrowed_*", ...}. `prune_window_blocks` = the age-warning threshold
    (dc.prune_window_blocks()). Each of them is None when not measurable on the running binary, and
    None is not zero.

    `jobs` is the one input that has no null form here: every count below is drawn from it, so a
    listing that could not be read would publish a network with no job, no audit and no slash. It
    raises on None; the caller keeps the last snapshot it published (see `_refresh`)."""
    if jobs is None:
        raise ValueError("jobs not measured (list-job unreadable): no payload is built from an unread listing")
    total = len(jobs)
    settled = audited = vindicated = clawed = pending = 0
    by_quorum = no_quorum = by_adjudication = legacy_resolved = 0
    distinct_min = None  # Min of distinct operators over the audits closed by quorum
    max_op_share_bps = None  # Max single-operator share (max_votes/voters, in bps) over those same audits
    disputed_ids = set()
    slash_events = []
    recent_audits = []
    for j in jobs:
        st = j.get("state", "") or ""
        if ("paid" in st) or ("settled" in st):
            settled += 1
        if "disputed" in st:
            audited += 1
            disputed_ids.add(j.get("id", ""))
            if "resolved" in st:
                # Partition (ADR-034): an audit conducted to a quorum, one that expired without quorum, and
                # one adjudicated by a redo committee are different things, and a monitoring gate counts
                # only the `quorum` class.
                #
                # The order matters: "noquorum" contains "quorum", so the more specific case is tested
                # first, or timeouts would count as conducted audits. (`adjudicated` shares no substring
                # with either, and the paths are mutually exclusive on chain: jobIsResolved forbids a
                # double resolution.)
                if "noquorum" in st:
                    no_quorum += 1
                elif "quorum" in st:
                    by_quorum += 1
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
                elif "adjudicated" in st:
                    by_adjudication += 1
                else:
                    legacy_resolved += 1  # jobs older than the markers, or an unknown state: kept visible
                if "clawed" in st:
                    clawed += 1
                else:
                    vindicated += 1
            else:
                pending += 1
            recent_audits.append({"job_id": j.get("id", ""), "state": st, "fee": int(j.get("fee", 0) or 0),
                                  "distinct_voters": int(j.get("audit_distinct_voters", 0) or 0)})
        for s in j.get("slashes", []) or []:
            if int(s.get("amount", 0) or 0) > 0:
                slash_events.append({"job_id": j.get("id", ""), "miner_id": s.get("miner_id", ""),
                                     "amount": int(s.get("amount", 0))})
    slashed_total_u = sum(e["amount"] for e in slash_events)
    # `deferred_no_jury`: selected then deferred (on-chain query), deduplicated against the disputed set,
    # since a job selected, deferred and then disputed by a person carries both marks and must land in one
    # class only.
    if deferred is None:
        deferred_no_jury = None
        opened_onchain = None
    else:
        deferred_no_jury = len([d for d in (deferred.get("job_ids") or []) if d and d not in disputed_ids])
        opened_onchain = int(deferred.get("audits_opened") or 0)
    # The partition is checked against an independent source: comparing the sum of the classes with
    # `audited` would compare two numbers counted by this same loop, true by construction (ADR-033,
    # question 6). The other side is `audits_opened`, counted by the chain when audits open. When it is
    # missing (an older binary), `partition_ok` is null, never true by default.
    classes_sum = by_quorum + no_quorum + by_adjudication + legacy_resolved + pending
    if opened_onchain is None:
        partition_ok = None
    else:
        partition_ok = (classes_sum + (deferred_no_jury or 0)) == opened_onchain
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
    # possible (`deploy/join.sh::_caught_up_line` has the same rule). With `age = now - last_block_epoch`:
    #   age >= threshold               -> stale (the equality counts, see the selftest);
    #   -threshold < age < threshold   -> not stale, and the age is published as measured, a negative
    #                                     one included: ordinary clock skew, which no zero may hide;
    #   age <= -threshold              -> this host's clock disagrees with the chain: the age and the
    #                                     verdict are both null -- not measured, never fresh.
    _now = int(time.time()) if now is None else int(now)
    _age = None if block_epoch is None else _now - int(block_epoch)
    if _age is not None and _age + STALE_BLOCK_S <= 0:
        _age = None
    chain_out = {
        "height": int(height or 0),
        "last_block_epoch": (None if block_epoch is None else int(block_epoch)),
        "last_block_age_s": _age,
        "stale": (None if _age is None else _age >= STALE_BLOCK_S),
        "stale_threshold_s": STALE_BLOCK_S,
    }
    return {
        "generated_at": _now,
        "height": int(height or 0),
        # The pair (height, age) lives here together. `height` above stays where it is, so that a consumer
        # that reads it keeps working.
        "chain": chain_out,
        "jobs": {"total": total, "settled": settled, "audited": audited,
                 "vindicated": vindicated, "clawed": clawed, "audit_pending": pending,
                 # Partition of the opened audits, in 6 exclusive classes. Either the sum equals
                 # audits_opened (an independent on-chain counter), or partition_ok is false, which
                 # means a state is hiding.
                 "audits_opened": opened_onchain,
                 "resolved_by_quorum": by_quorum,
                 "resolved_no_quorum": no_quorum,
                 "resolved_by_adjudication": by_adjudication,
                 "resolved_unmarked": legacy_resolved,
                 "deferred_no_jury": deferred_no_jury,
                 "partition_ok": partition_ok,
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
                    "resolved_by_quorum counts the audits a jury concluded. A null field means not "
                    "measurable on this binary, never zero. Public research testnet.",
        },
    }


class Unmeasured(Exception):
    """A read the payload depends on could not be made. Its message is served in the public payload,
    so it names what was not read, never a host, a path or a command line."""


# The fields `build_proof` computes against the clock of the build (`now`), apart from `generated_at`,
# which dates the document itself. They are verdicts about now, not facts about the snapshot. The
# bench derives the same set from `build_proof` (two builds that differ only by `now`) and fails when a
# field that depends on the clock is added without being listed here.
NOW_VERDICTS = (("chain", "last_block_age_s"), ("chain", "stale"))


def kept_payload(doc):
    """The last snapshot, as it is served after a failed refresh: its facts kept, its verdicts about
    now withdrawn (null = not measured now).

    Served as built, a snapshot taken five seconds after a block would keep answering "new blocks keep
    arriving" (`chain.stale` false, `chain.last_block_age_s` 5) for the whole outage of the node, on the one
    field a reader consults to know whether the chain runs. The kept age cannot be advanced either: a block
    newer than the kept one may exist, so `now - last_block_epoch` only bounds the real age from above. The
    facts (`height`, `chain.last_block_epoch`, every count) stay, dated by `refresh.snapshot_generated_at`.
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
                           "reason": NOT_YET_BUILT, "snapshot_generated_at": None}}
    return {"t": 0.0, "payload": json.dumps(payload, ensure_ascii=False).encode(), "doc": None,
            "failing_since": None}


def publish(cache, now, read):
    """One refresh of the served payload; True when it was rebuilt. Pure apart from `read()`.

    `read()` returns the keyword arguments of `build_proof`, or raises. On success the payload is
    rebuilt and marked current. On any failure the last payload built is kept and served, and it says
    so: `refresh.current` is false, with the time the failures began and their reason. Rebuilding from what
    could not be read would publish zeros (no job, no audit, no slash) as if measured, and serving the old
    snapshot without a mark would present it as fresh. A kept snapshot also loses its verdicts about now
    (`kept_payload`), since a reader of `chain.stale` does not read the mark.

    `refresh.current` has no third value: a payload either came from the last attempt or it did not. What
    is not known, because nothing was ever built, shows as the absence of every other field, with
    `snapshot_generated_at` null, never as a payload of zeros (see `new_cache` for the payload served
    before the first refresh completes)."""
    try:
        doc = build_proof(**read())
        payload = dict(doc)
        payload["refresh"] = {"current": True, "attempted_at": int(now), "failing_since": None,
                              "reason": None, "snapshot_generated_at": doc.get("generated_at")}
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
                          "snapshot_generated_at": None if kept is None else kept.get("generated_at")}
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
    unbuilt = json.loads(new_cache()["payload"])
    chk("before the first refresh completes the payload is marked (current=false), never {}",
        unbuilt["refresh"]["current"] is False and unbuilt["refresh"]["reason"] == NOT_YET_BUILT
        and "jobs" not in unbuilt)

    print("selftest the-proof", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # Before the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import threading

import client as dc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DENDRA_PROOF_HOST", "127.0.0.1")  # public = reverse proxy in front (no bare 0.0.0.0 bind)
PORT = int(os.environ.get("DENDRA_PROOF_PORT", "8090"))
CORS = os.environ.get("DENDRA_PROOF_CORS", "")           # e.g. https://dendranetwork.com (empty = no header)
REFRESH_S = float(os.environ.get("DENDRA_PROOF_REFRESH_S", "15"))
# `t` is the last attempt, failed or not: a read that keeps failing is retried at the refresh pace,
# never on every request -- each attempt walks the whole job listing.
_CACHE = new_cache()
_REFRESH_LOCK = threading.Lock()


def _read_inputs():
    """The chain reads `build_proof` needs. Raises `Unmeasured` when the job listing is unreadable:
    `dc.list_jobs_full()` answers None then, and None is not an empty network."""
    jobs = dc.list_jobs_full()
    if jobs is None:
        raise Unmeasured("list-job unreadable or incomplete")
    # One status read yields both: pairing a height with a timestamp fetched separately would
    # produce the age of neither.
    height, block_epoch = dc.head()
    return {"jobs": jobs, "pools": dc.pools(), "seed_health": dc.committee_seed_health(),
            "height": height, "block_epoch": block_epoch,
            # None when the query does not exist (older binary) -> null fields, never 0
            "deferred": dc.audit_deferred(),
            # same: "retained is not lost" is only measurable on a recent binary
            "held": dc.held_summary(),
            # age-warning threshold; None when the params are unreadable
            "prune_window_blocks": dc.prune_window_blocks()}


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
