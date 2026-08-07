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


def build_proof(jobs, pools, seed_health, height, deferred=None, held=None,
                prune_window_blocks=None, max_recent=10):
    """The Proof payload (PURE). `jobs` = list_jobs_full() (id/state/miner_id/fee/audit_*/slashes).
    `deferred` = client.audit_deferred(): {"job_ids": [...], "audits_opened": N}, measured
    on-chain. `held` = client.held_summary(): {"held_total", "held_count",
    "oldest_age_blocks", "bond_escrowed_*", ...}. `prune_window_blocks` = the age-warning threshold
    (dc.prune_window_blocks()). Each of them is None when NOT MEASURABLE on the running binary, and
    None is not zero."""
    total = len(jobs or [])
    settled = audited = vindicated = clawed = pending = 0
    by_quorum = no_quorum = by_adjudication = legacy_resolved = 0
    distinct_min = None  # MIN of distinct operators over the audits closed BY QUORUM
    max_op_share_bps = None  # MAX single-operator share (max_votes/voters, in bps) over those same audits
    disputed_ids = set()
    slash_events = []
    recent_audits = []
    for j in jobs or []:
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
    return {
        "generated_at": int(time.time()),
        "height": int(height or 0),
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
        "vrf": seed_health or {},
        "pools": pools or {},
        # PUBLIC feed: these strings are served verbatim on the /proof endpoint. Neutral and
        # descriptive - provenance is described, not argued.
        "_provenance": {
            "claim": "On-chain state, READ-ONLY, through the public RPC. No value is computed or "
                     "completed off-chain.",
            "note": "audited = job selected by the VRF audit; clawed = payment reclaimed; "
                    "vindicated = honest verdict. A null field means NOT MEASURABLE on this "
                    "binary, never zero. Research devnet.",
        },
    }


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
    print("SELFTEST THE-PROOF", "PASS" if ok else "FAIL")
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
        deferred = dc.audit_deferred()  # None when the query does not exist (older binary) -> null fields, never 0
        held = dc.held_summary()        # same: "retained is not lost" is only measurable on a recent binary
        window = dc.prune_window_blocks()  # age-warning threshold; None when the params are unreadable
        _CACHE["payload"] = json.dumps(build_proof(jobs, pools, seed, height, deferred=deferred, held=held,
                                                   prune_window_blocks=window),
                                       ensure_ascii=False).encode()
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
    print(f"[the-proof] read-only facade on http://{HOST}:{PORT}/proof (refresh {REFRESH_S:g}s; CORS {CORS or 'off'})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
