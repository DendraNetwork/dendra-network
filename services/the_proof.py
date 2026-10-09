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


# Past this, a last block stops being proof of life. The value is an ORDER OF MAGNITUDE owned as such,
# not a derivation: the interval between blocks is fixed by no consensus parameter (it emerges from
# `timeout_commit` and from propagation), so no number on the chain honestly turns into this threshold.
# It is published inside the payload so a reader can apply their own and see which one produced ours.
STALE_BLOCK_S = int(os.environ.get("DENDRA_PROOF_STALE_S", "300"))


def build_proof(jobs, pools, seed_health, height, deferred=None, held=None,
                prune_window_blocks=None, max_recent=10, block_epoch=None, now=None):
    """The Proof payload (PURE). `jobs` = list_jobs_full() (id/state/miner_id/fee/audit_*/slashes).
    `deferred` = client.audit_deferred(): {"job_ids": [...], "audits_opened": N}, measured
    on-chain. `held` = client.held_summary(): {"held_total", "held_count",
    "oldest_age_blocks", "bond_escrowed_*", ...}. `prune_window_blocks` = the age-warning threshold
    (dc.prune_window_blocks()). Each of them is None when NOT MEASURABLE on the running binary, and
    None is not zero.

    `jobs` is the one input that has NO null form here: every count below is drawn from it, so a
    listing that could not be read would publish a network with no job, no audit and no slash. It
    RAISES on None; the caller keeps the last snapshot it published (see `_refresh`)."""
    if jobs is None:
        raise ValueError("jobs not measured (list-job unreadable): no payload is built from an unread listing")
    total = len(jobs)
    settled = audited = vindicated = clawed = pending = 0
    by_quorum = no_quorum = by_adjudication = legacy_resolved = 0
    distinct_min = None  # MIN of distinct operators over the audits closed BY QUORUM
    max_op_share_bps = None  # MAX single-operator share (max_votes/voters, in bps) over those same audits
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
                # PARTITION (ADR-034): an audit that was CONDUCTED (quorum), one that EXPIRED
                # (noquorum) and one that was ADJUDICATED by a redo committee are not the same thing.
                # A monitoring gate counts ONLY the `quorum` class; fed an aggregate, it would read
                # "4 audits" where ZERO had actually been conducted.
                #
                # MANDATORY ORDER: "noquorum" CONTAINS "quorum". Testing `"quorum" in st` first would
                # classify timeouts as conducted audits - the substring swallowing its own variant.
                # The most specific case is tested FIRST. (`adjudicated` shares no substring with
                # either, and the paths are mutually exclusive on-chain: jobIsResolved forbids a
                # double resolution.)
                if "noquorum" in st:
                    no_quorum += 1
                elif "quorum" in st:
                    by_quorum += 1
                    dv = int(j.get("audit_distinct_voters", 0) or 0)
                    distinct_min = dv if distinct_min is None else min(distinct_min, dv)
                    # Plurality is not counted in heads: the gate reads the SHARE of the largest
                    # operator (< 50 %). What is exposed is the WORST case of the window, the MAX of
                    # those shares - an average would drown one captured audit among ten healthy
                    # ones.
                    voters = int(j.get("audit_voters", 0) or 0)
                    mov = int(j.get("audit_max_operator_votes", 0) or 0)
                    if voters > 0:
                        share = mov * 10000 // voters
                        max_op_share_bps = share if max_op_share_bps is None else max(max_op_share_bps, share)
                elif "adjudicated" in st:
                    by_adjudication += 1
                else:
                    legacy_resolved += 1  # jobs predating the markers, or unknown state -> MUST stay visible
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
    # `deferred_no_jury`: selected-then-deferred (on-chain query), DEDUPLICATED against the disputed
    # set - a job that was selected, deferred, and then disputed by a human carries both marks, and
    # must land in exactly one class.
    if deferred is None:
        deferred_no_jury = None
        opened_onchain = None
    else:
        deferred_no_jury = len([d for d in (deferred.get("job_ids") or []) if d and d not in disputed_ids])
        opened_onchain = int(deferred.get("audits_opened") or 0)
    # THE PARTITION TEST, checked against an INDEPENDENT source - otherwise it tests nothing.
    # Comparing the sum of the classes to `audited` would compare two numbers counted by THIS same
    # loop: true BY CONSTRUCTION, and a sum that cannot come out false verifies nothing (ADR-033,
    # question 6). The right-hand side is therefore `audits_opened`, counted BY THE CHAIN at opening
    # time. When it is missing (older binary), `partition_ok` is null - never a true by default.
    classes_sum = by_quorum + no_quorum + by_adjudication + legacy_resolved + pending
    if opened_onchain is None:
        partition_ok = None
    else:
        partition_ok = (classes_sum + (deferred_no_jury or 0)) == opened_onchain
    # The `held` section, precomputed. Pockets 2 and 3 PRESERVE the client's None: an absent
    # `pockets_measured` sentinel means a binary older than that pocket, whose zeros are not
    # measurements but artifacts of the codec omitting zeros. A zero HERE is a MEASURED zero.
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
            # The THIRD pocket: jurors RETAINED by the exit guard. Same family of promise ("locked
            # is not seized"), so the same section. A `jurors_locked` that keeps growing means the
            # guard is immobilizing the juror pool; an `oldest_lock_age` that only ever rises means
            # honest jurors are serving other people's silence. This is the evidence on which
            # exit-after-vote should be decided.
            "jurors_locked": (None if held.get("jurors_locked_count") is None
                              else int(held.get("jurors_locked_count") or 0)),
            "oldest_lock_age_blocks": _lockage,
            "oldest_lock_job_id": held.get("oldest_lock_job_id"),
            "age_warning": (prune_window_blocks is not None
                            and int(held.get("oldest_age_blocks") or 0) > int(prune_window_blocks or 0) > 0),
            # `lock_warning` is the STRICT TWIN of the held warning: the SAME debt - unbounded
            # deferral - seen from the STAKE side, at the SAME threshold. Two faces of one signal
            # cannot raise the alarm at different thresholds, so `age_warning_threshold_blocks`
            # governs both. It is a signal, never a block and never an automatic release. None when
            # the AGE is not measurable: "I could not look" is not "it is fine". False when only the
            # THRESHOLD is missing - same rule as the held warning: no warning is invented without a
            # threshold.
            "lock_warning": (None if _lockage is None
                             else (prune_window_blocks is not None
                                   and _lockage > int(prune_window_blocks or 0) > 0)),
            "age_warning_threshold_blocks": prune_window_blocks}
    # THE AGE OF THE LAST BLOCK -- the only field here that tells a live chain from a stopped one.
    # `generated_at` is the age of THIS DOCUMENT and it is always fresh, which is precisely what makes
    # it misleading on its own: a feed regenerated every fifteen seconds looks alive while the chain it
    # describes has produced nothing for hours. `height` does not help either -- it stays high, it is
    # simply motionless. Measured on this network: a full stop was served for hours with a fresh
    # `generated_at`, a plausible `height`, and no field able to contradict either.
    #
    # `stale` HAS THREE VALUES AND THE THIRD IS THE POINT. true = the last block is older than the
    # threshold. false = it is not. null = the timestamp could not be read, so nothing is claimed --
    # reading that as false would answer "the chain is fine" to a question nobody managed to ask. Same
    # rule as `held` and `lock_warning` above: a null field means NOT MEASURABLE, never zero.
    #
    # THE AGE IS BOUNDED ON BOTH SIDES, BY THE SAME THRESHOLD. It used to be clamped at zero, which
    # turned a last block dated in the FUTURE of this host into "0 s old": the freshest reading there
    # is. A block is only ahead of the clock that measures it when that clock is behind, and a clock
    # behind by the threshold or more makes a live chain and a stopped one look alike from here
    # (`deploy/join.sh::_caught_up_line` measured it: a clock 24 h behind announced a chain stopped
    # since 2020 as healthy). So, with `age = now - last_block_epoch`:
    #   age >= threshold               -> stale (the equality counts, see the selftest);
    #   -threshold < age < threshold   -> not stale, and the age is published AS MEASURED, a negative
    #                                     one included: ordinary clock skew, which no zero may hide;
    #   age <= -threshold              -> this host's clock disagrees with the chain: the age and the
    #                                     verdict are both NULL -- not measured, never fresh.
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
        # The pair (height, age) lives here together. `height` above is kept where it was: a consumer
        # that reads it must keep working, and a field moved is a field a third party stops finding.
        "chain": chain_out,
        "jobs": {"total": total, "settled": settled, "audited": audited,
                 "vindicated": vindicated, "clawed": clawed, "audit_pending": pending,
                 # PARTITION of the OPENED audits, in 6 exclusive classes. Either the sum equals
                 # audits_opened (an independent on-chain counter), or partition_ok is false, which
                 # means a state is hiding.
                 "audits_opened": opened_onchain,
                 "resolved_by_quorum": by_quorum,
                 "resolved_no_quorum": no_quorum,
                 "resolved_by_adjudication": by_adjudication,
                 "resolved_unmarked": legacy_resolved,
                 "deferred_no_jury": deferred_no_jury,
                 "partition_ok": partition_ok,
                 # MIN of the DISTINCT operators behind the audits closed BY QUORUM, over the whole
                 # life of the chain. null while no audit has been conducted. A quorum reached by 15
                 # seats all held by one operator is a quorum in form only, so the gate reads
                 # distinct_min >= 3 (a cheap floor) AND max_operator_share < 50 %, which is the real
                 # property: no operator held the majority of the counted votes.
                 "distinct_voters_min": distinct_min,
                 "max_operator_share_bps": max_op_share_bps},
        # "Retained is not lost", made FALSIFIABLE, for BOTH pockets: the retained fee AND the
        # escrowed bond. An invisible bond is exactly the orphaned fund this section exists to make
        # impossible. `age_warning` is a signal - never a block, never a release - raised when the
        # oldest retention outlives the window it takes to evict a dead miner (miner_prune_blocks):
        # if funds stay retained longer than that, the network is not healing.
        # null means not measurable on this binary, never an invented zero.
        "held": held_out,
        "slashes": {"events": len(slash_events), "total_udndr": slashed_total_u,
                    "recent": slash_events[-max_recent:]},
        "recent_audits": recent_audits[-max_recent:],
        # None when the seed-health query FAILED (client.committee_seed_health): published as null,
        # never as an empty object whose every field the explorer would read as a measured zero.
        "vrf": seed_health,
        "pools": pools or {},
        # PUBLIC feed: these strings are served verbatim on the /proof endpoint. Neutral and
        # descriptive - provenance is described, not argued.
        "_provenance": {
            "claim": "On-chain state, READ-ONLY, through the public RPC. No value is computed or "
                     "completed off-chain.",
            "note": "audited = audit opened with an anchored jury, or disputed; clawed = payment "
                    "reclaimed by an audit jury's conviction; vindicated = resolved with no clawed "
                    "marker, which includes jobs unwound to the client and jobs adjudicated by a "
                    "dispute jury (either outcome), so it is not a count of upheld verdicts; "
                    "resolved_by_quorum counts the audits a jury concluded. A null field means NOT "
                    "MEASURABLE on this binary, never zero. Public research testnet.",
        },
    }


class Unmeasured(Exception):
    """A read the payload depends on could not be made. Its message is served in the PUBLIC payload,
    so it names WHAT was not read, never a host, a path or a command line."""


# The fields `build_proof` computes against the clock of the build (`now`), apart from `generated_at`,
# which dates the document itself. They are verdicts about NOW, not facts about the snapshot. The
# bench derives the same set from `build_proof` (two builds that differ only by `now`) and fails when a
# field that depends on the clock is added without being listed here.
NOW_VERDICTS = (("chain", "last_block_age_s"), ("chain", "stale"))


def kept_payload(doc):
    """The last snapshot, as it is served after a failed refresh: its facts kept, its verdicts about
    NOW withdrawn (null = not measured now).

    Served as built, a snapshot taken five seconds after a block keeps answering "new blocks keep
    arriving" (`chain.stale` false, `chain.last_block_age_s` 5) for the whole outage of the node: the
    reassuring answer, on the one field a reader consults to know whether the chain runs, at the very
    moment it may not. The kept age cannot be advanced either: a block newer than the kept one may
    exist, so `now - last_block_epoch` bounds the real age from ABOVE and decides nothing. The facts
    (`height`, `chain.last_block_epoch`, every count) stay, dated by `refresh.snapshot_generated_at`.
    `doc` is not modified."""
    payload = dict(doc)
    for section, field in NOW_VERDICTS:
        part = dict(payload[section])
        part[field] = None
        payload[section] = part
    return payload


NOT_YET_BUILT = "first refresh not completed"


def new_cache():
    """The served state before any refresh has completed. Its payload is MARKED, never `{}`: the
    refresh lock does not make a request wait, so a request arriving during the first walk of the job
    listing is served this payload, and an empty object has no `refresh` field at all -- the one
    payload whose `refresh.current` would be neither true nor false. Same shape as a refresh that
    failed with nothing to keep; `attempted_at` is null because no attempt has completed."""
    payload = {"refresh": {"current": False, "attempted_at": None, "failing_since": None,
                           "reason": NOT_YET_BUILT, "snapshot_generated_at": None}}
    return {"t": 0.0, "payload": json.dumps(payload, ensure_ascii=False).encode(), "doc": None,
            "failing_since": None}


def publish(cache, now, read):
    """One refresh of the served payload; True when it was rebuilt. Pure apart from `read()`.

    `read()` returns the keyword arguments of `build_proof`, or raises. On success the payload is
    rebuilt and marked current. On ANY failure the LAST payload built is kept and served, and it SAYS
    so: `refresh.current` is false, with the time the failures began and their reason. The two other
    choices are both wrong, in opposite directions: rebuilding from what could not be read publishes
    zeros (no job, no audit, no slash) as if they were measured, and serving the old snapshot without
    a mark presents it as fresh. A kept snapshot loses its verdicts about NOW (`kept_payload`): the
    mark says the counts are old, but a reader of `chain.stale` does not read the mark.

    `refresh.current` has no third value: a payload either came from the last attempt or it did not.
    What is NOT known -- because nothing was ever built -- shows as the ABSENCE of every other field,
    with `snapshot_generated_at` null, never as a payload of zeros (see `new_cache` for the payload
    served before the first refresh completes)."""
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
         # NEUTRAL identifier: `slashes[].miner_id` travels verbatim into the public feed, and a
         # bench moniker there would read as staging where there is only a test fixture.
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
    chk("without the on-chain query (deferred=None): deferred/opened/partition_ok are null, NEVER a true by default",
        p["jobs"]["deferred_no_jury"] is None and p["jobs"]["audits_opened"] is None
        and p["jobs"]["partition_ok"] is None)

    # PARTITION (ADR-034): the gate counts ONLY the audits closed by quorum. Without these
    # assertions the counters would read "green" while never being exercised at all.
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
    # deferred: q6 was never opened (it counts), q4 was selected THEN disputed (deduplicated, so it
    # does not count twice).
    deferred = {"job_ids": ["q6", "q4"], "audits_opened": 6}
    pq = build_proof(jobs_part, {}, {}, 1, deferred=deferred)
    # THE point: "noquorum" CONTAINS "quorum". A naive classification would count q2 as a CONDUCTED
    # audit, and the gate would declare itself green over audits that never took place.
    chk("partition: by_quorum=2, no_quorum=1, by_adjudication=1, pending=1 (an EXPIRED audit is not a CONDUCTED one)",
        pq["jobs"]["resolved_by_quorum"] == 2 and pq["jobs"]["resolved_no_quorum"] == 1
        and pq["jobs"]["resolved_by_adjudication"] == 1 and pq["jobs"]["audit_pending"] == 1)
    chk("deferred deduplicated against disputes: q4 (selected THEN disputed) counts once -> deferred=1",
        pq["jobs"]["deferred_no_jury"] == 1)
    # The partition test against the INDEPENDENT source: 5 classes + 1 deferred == 6 opened.
    chk("partition_ok: classes+deferred == audits_opened (an ON-CHAIN counter, not the same enumeration)",
        pq["jobs"]["partition_ok"] is True)
    # FALSIFIABILITY: a version comparing two sides of the same loop is true BY CONSTRUCTION. If
    # this case cannot return False, the partition test tests nothing (ADR-033, question 6).
    bad = build_proof(jobs_part, {}, {}, 1, deferred={"job_ids": ["q6", "q4"], "audits_opened": 99})
    chk("partition_ok CAN come out FALSE (opened=99 != classes) - a sum that cannot fail verifies nothing",
        bad["jobs"]["partition_ok"] is False)
    chk("distinct_voters_min = MIN over the BY-QUORUM audits only (3, not 4, and not the adjudicated 5)",
        pq["jobs"]["distinct_voters_min"] == 3)
    # Largest-operator share: q1 = 6/10 = 6000 bps (over 50 %, so the gate WOULD see it), q3 = 3/10
    # = 3000. The WORST case (MAX) is exposed, not an average that would drown the captured audit.
    chk("max_operator_share_bps = MAX share over the by-quorum audits (6000, not 3000)",
        pq["jobs"]["max_operator_share_bps"] == 6000)
    no_q = build_proof([{"id": "n1", "state": "open+paid+optimistic+disputed+resolved+clawed+noquorum",
                         "fee": 1, "slashes": []}], {}, {}, 1, deferred={"job_ids": [], "audits_opened": 1})
    chk("distinct_voters_min = null with no conducted audit (never 0: a false zero would pass the gate backwards)",
        no_q["jobs"]["distinct_voters_min"] is None)
    chk("max_operator_share_bps = null with no conducted audit (0 would claim 'no capture' without measuring)",
        no_q["jobs"]["max_operator_share_bps"] is None)
    # The held section: measured versus not measurable (None is not 0), TWO pockets (fee and bond),
    # and the age warning (a signal, never a block).
    ph = build_proof([], {}, {}, 1, held={"held_total": 160000, "held_count": 2, "oldest_age_blocks": 900,
                                          "bond_escrowed_total": 50000, "bond_escrowed_count": 1},
                     prune_window_blocks=604800)
    chk("held measured: total/count/age exposed (the two numbers that would contradict 'retained is not lost')",
        ph["held"]["total_udndr"] == 160000 and ph["held"]["count"] == 2 and ph["held"]["oldest_age_blocks"] == 900)
    chk("the escrowed BOND is VISIBLE (the second pocket - an invisible bond is the archetypal orphaned fund)",
        ph["held"]["bond_escrowed_udndr"] == 50000 and ph["held"]["bond_escrowed_count"] == 1)
    # The third pocket: locked jurors. None is not 0 AT FIELD LEVEL (the client-side
    # `pockets_measured` sentinel), and `lock_warning` is the strict twin of the held warning, at the
    # SAME threshold.
    pj = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 15, "oldest_lock_age_blocks": 400000,
                                          "oldest_lock_job_id": "jLock"},
                     prune_window_blocks=604800)
    chk("LOCKED JURORS are VISIBLE (the third pocket - the evidence for exit-after-vote)",
        pj["held"]["jurors_locked"] == 15 and pj["held"]["oldest_lock_age_blocks"] == 400000
        and pj["held"]["oldest_lock_job_id"] == "jLock")
    chk("lock BELOW the threshold -> lock_warning False (a measurement, not silence)",
        pj["held"]["lock_warning"] is False)
    chk("pocket ABSENT -> None, NEVER a false zero (ph has no lock fields: not measurable)",
        ph["held"]["jurors_locked"] is None and ph["held"]["lock_warning"] is None)
    pl = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 3, "oldest_lock_age_blocks": 604801,
                                          "oldest_lock_job_id": "jOld"},
                     prune_window_blocks=604800)
    chk("lock BEYOND the threshold -> lock_warning True WHILE age_warning sleeps - the two faces are "
        "independent (small jobs, large committees: a silent held no longer hides the lock)",
        pl["held"]["lock_warning"] is True and pl["held"]["age_warning"] is False)
    pz = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 0, "oldest_lock_age_blocks": 0,
                                          "oldest_lock_job_id": ""},
                     prune_window_blocks=604800)
    chk("zero PRESENT = zero MEASURED (0, not None) - the sentinel is what makes the claim opposable",
        pz["held"]["jurors_locked"] == 0 and pz["held"]["lock_warning"] is False)
    pt = build_proof([], {}, {}, 1, held={"held_total": 0, "held_count": 0, "oldest_age_blocks": 0,
                                          "jurors_locked_count": 1, "oldest_lock_age_blocks": 999999,
                                          "oldest_lock_job_id": "jT"},
                     prune_window_blocks=None)
    chk("threshold not measurable -> lock_warning False, never invented (same rule as the held warning)",
        pt["held"]["lock_warning"] is False)
    chk("age warning SILENT below the threshold (900 <= 604800)",
        ph["held"]["age_warning"] is False)
    pw = build_proof([], {}, {}, 1, held={"held_total": 1, "held_count": 1, "oldest_age_blocks": 604801},
                     prune_window_blocks=604800)
    chk("age warning RAISED beyond miner_prune_blocks (older than the eviction of a dead miner)",
        pw["held"]["age_warning"] is True)
    pn = build_proof([], {}, {}, 1, held={"held_total": 1, "held_count": 1, "oldest_age_blocks": 999999},
                     prune_window_blocks=None)
    chk("threshold not measurable (params unreadable) -> warning NEVER invented (False, null threshold)",
        pn["held"]["age_warning"] is False and pn["held"]["age_warning_threshold_blocks"] is None)
    chk("held = null when the query is missing (older binary) - never invented zeros",
        p["held"] is None)
    chk("slash events=1 total=320000", p["slashes"]["events"] == 1 and p["slashes"]["total_udndr"] == 320000)
    chk("recent_audits capped at 2", len(p["recent_audits"]) == 2)
    chk("height + vrf + pools present", p["height"] == 1234 and p["vrf"]["contributors"] == 2 and p["pools"]["treasury"] == 7)
    chk("payload is JSON-serializable", bool(json.dumps(p)))
    chk("a failed seed-health query is published as null, never as an empty object of zeros",
        build_proof([], {}, None, height=9)["vrf"] is None)

    # ── THE AGE OF THE LAST BLOCK ───────────────────────────────────────────────────────────────
    # These cases exist because a stopped chain was served for hours with a fresh `generated_at` and
    # a plausible `height`, and no field in this payload could contradict either.
    live = build_proof([], {}, {}, height=9, block_epoch=1000, now=1030)["chain"]
    chk("live chain: the age of the last block is published", live["last_block_age_s"] == 30)
    chk("live chain: stale is False - a measurement, not silence", live["stale"] is False)
    chk("the top-level height is still served (a field that moves is a field a reader stops finding)",
        live["height"] == 9)

    dead = build_proof([], {}, {}, height=9, block_epoch=1000, now=1000 + 24128)["chain"]
    chk("stopped chain: the real outage measured on this network reads as stale", dead["stale"] is True)
    chk("stopped chain: the exact age is published, not just a flag", dead["last_block_age_s"] == 24128)

    # The threshold bites AT EQUALITY: a guard demanding a strict excess lets through exactly the
    # value it was asked to refuse.
    at = build_proof([], {}, {}, height=9, block_epoch=1000, now=1000 + STALE_BLOCK_S)["chain"]
    chk("at the threshold exactly: already stale", at["stale"] is True)
    under = build_proof([], {}, {}, height=9, block_epoch=1000, now=999 + STALE_BLOCK_S)["chain"]
    chk("one second under the threshold: not stale", under["stale"] is False)

    # A last block dated AHEAD of this host. Inside the threshold it is ordinary clock skew: the age is
    # published as measured, negative, and the chain is not stale. At the threshold or beyond, this
    # clock disagrees with the chain: both fields are null. The clamp that stood here served this very
    # case as "0 s old, not stale".
    skew = build_proof([], {}, {}, height=9, block_epoch=1000, now=998)["chain"]
    chk("block 2 s ahead of this clock: the age is published as measured (-2), never clamped to 0",
        skew["last_block_age_s"] == -2)
    chk("block 2 s ahead of this clock: not stale (ordinary skew)", skew["stale"] is False)
    edge = build_proof([], {}, {}, height=9, block_epoch=1001 + STALE_BLOCK_S - 2, now=1000)["chain"]
    chk("one second inside the bound, ahead: still judged, age as measured, not stale",
        edge["last_block_age_s"] == 1 - STALE_BLOCK_S and edge["stale"] is False)
    ahead = build_proof([], {}, {}, height=9, block_epoch=1000 + STALE_BLOCK_S, now=1000)["chain"]
    chk("block dated a full threshold ahead: this clock disagrees -- the age is NULL, never 0",
        ahead["last_block_age_s"] is None)
    chk("block dated a full threshold ahead: stale is NULL, never False", ahead["stale"] is None)
    chk("block dated a full threshold ahead: the block's own date is still published",
        ahead["last_block_epoch"] == 1000 + STALE_BLOCK_S)

    unk = build_proof([], {}, {}, height=9, block_epoch=None, now=1000)["chain"]
    chk("unreadable timestamp: the age is null, never an invented zero",
        unk["last_block_age_s"] is None)
    # The load-bearing case: False here would answer "the chain is fine" to a question nobody managed
    # to ask, on the single field a reader consults to know whether the network runs at all.
    chk("unreadable timestamp: stale is NULL, never False", unk["stale"] is None)
    chk("the threshold used is published next to the verdict",
        unk["stale_threshold_s"] == STALE_BLOCK_S)

    # ── AN UNREAD LISTING IS NOT AN EMPTY NETWORK ──────────────────────────────────────────────
    try:
        build_proof(None, {}, {}, height=9)
        chk("jobs=None RAISES -- a payload is never built from an unread listing", False)
    except ValueError:
        chk("jobs=None RAISES -- a payload is never built from an unread listing", True)
    chk("an EMPTY listing is a measurement: zero jobs, published",
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
    chk("an unread listing KEEPS the last snapshot (5 jobs), never zeros",
        kept is False and served["jobs"]["total"] == 5 and served["generated_at"] == good["generated_at"])
    chk("... and SAYS it: current=false, the reason, and when the failures began",
        served["refresh"]["current"] is False and served["refresh"]["failing_since"] == 1030
        and served["refresh"]["reason"] == "list-job unreadable or incomplete"
        and served["refresh"]["snapshot_generated_at"] == good["generated_at"])
    publish(cache, 1060, _unread)
    chk("a second failure keeps the START of the outage, not the latest attempt",
        json.loads(cache["payload"])["refresh"]["failing_since"] == 1030
        and json.loads(cache["payload"])["refresh"]["attempted_at"] == 1060)
    publish(cache, 1090, _inputs([]))
    healed = json.loads(cache["payload"])
    chk("a later EMPTY listing that was READ is published as zero, and the outage mark is cleared",
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
    chk("an unexpected failure serves its TYPE only, never the message (hosts, paths, commands)",
        json.loads(blank["payload"])["refresh"]["reason"] == "refresh failed (OSError)")

    # ── A KEPT SNAPSHOT SAYS NOTHING ABOUT NOW ─────────────────────────────────────────────────
    dated = {}
    publish(dated, 2000, lambda: {"jobs": jobs, "pools": {}, "seed_health": {}, "height": 100,
                                  "block_epoch": 1995, "now": 2000})
    built_chain = json.loads(dated["payload"])["chain"]
    publish(dated, 2030, _unread)
    kept_chain = json.loads(dated["payload"])["chain"]
    chk("a kept snapshot withdraws its verdicts about NOW: stale and age null, never the 'live' of its build",
        built_chain["stale"] is False and built_chain["last_block_age_s"] == 5
        and kept_chain["stale"] is None and kept_chain["last_block_age_s"] is None)
    chk("... and keeps its facts: height and the epoch of its last block",
        kept_chain["height"] == 100 and kept_chain["last_block_epoch"] == 1995
        and dated["doc"]["chain"]["stale"] is False)
    unbuilt = json.loads(new_cache()["payload"])
    chk("before the first refresh completes the payload is MARKED (current=false), never {}",
        unbuilt["refresh"]["current"] is False and unbuilt["refresh"]["reason"] == NOT_YET_BUILT
        and "jobs" not in unbuilt)

    print("SELFTEST THE-PROOF", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # BEFORE the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import threading

import client as dc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DENDRA_PROOF_HOST", "127.0.0.1")  # public = reverse proxy in front (no bare 0.0.0.0 bind)
PORT = int(os.environ.get("DENDRA_PROOF_PORT", "8090"))
CORS = os.environ.get("DENDRA_PROOF_CORS", "")           # e.g. https://dendranetwork.com (empty = no header)
REFRESH_S = float(os.environ.get("DENDRA_PROOF_REFRESH_S", "15"))
# `t` is the last ATTEMPT, failed or not: a read that keeps failing is retried at the refresh pace,
# never on every request -- each attempt walks the whole job listing.
_CACHE = new_cache()
_REFRESH_LOCK = threading.Lock()


def _read_inputs():
    """The chain reads `build_proof` needs. Raises `Unmeasured` when the job listing is unreadable:
    `dc.list_jobs_full()` answers None then, and None is not an empty network."""
    jobs = dc.list_jobs_full()
    if jobs is None:
        raise Unmeasured("list-job unreadable or incomplete")
    # ONE status read yields both: pairing a height with a timestamp fetched separately would
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
