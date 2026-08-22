#!/usr/bin/env python3
"""Tests for c3_run_measure — no dependencies. `python3 test_c3_run_measure.py`

Tests (1) and (1b) are the red cases: they fail against the previous identifier read
(`j.get("id", j.get("jobId",""))`) and pass against the current one.
"""
import sys

from c3_run_measure import job_id_of, measure, tokens

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


# EXACT shape returned by `dendrad query jobs list-job -o json`: snake_case, uint64 as STRINGS,
# zero-valued fields OMITTED (verified against a real dendrad node).
def chain_job(jid, miner, state, slashes=None, voters=None, distinct=None, maxop=None):
    j = {"job_id": jid, "client": "dendra1cli", "miner_id": miner, "fee": "700", "state": state}
    if slashes:
        j["slash_records"] = [{"miner_id": m, "amount": str(a)} for m, a in slashes]
    if voters is not None:
        j["audit_voters"] = str(voters)
    if distinct is not None:
        j["audit_distinct_voters"] = str(distinct)
    if maxop is not None:
        j["audit_max_operator_votes"] = str(maxop)
    return j


def legacy_job_id(j):
    """The previous identifier read, reproduced verbatim so that the defect stays EXECUTABLE."""
    return j.get("id", j.get("jobId", ""))


# --- (1) RED CASE: the previous read returns "" on the real chain shape ---------------------------
j = chain_job("job-5", "cheater", "settled+optimistic+disputed+resolved+clawed+quorum")
chk("(1) RED CASE: the previous id read returns an EMPTY string on a real job",
    legacy_job_id(j) == "", f"-> {legacy_job_id(j)!r}")
chk("(1b) the current read returns the real job_id", job_id_of(j) == "job-5", f"-> {job_id_of(j)!r}")
chk("(1c) it also accepts camelCase (REST) and the client's normalised shape",
    job_id_of({"jobId": "a"}) == "a" and job_id_of({"id": "b"}) == "b")

# --- (2) the triplet only speaks about honest miners; the cheater has its own counter -------------
state = {
    "jobs": [
        chain_job("j1", "juge1", "settled+optimistic"),
        chain_job("j2", "juge1", "settled+optimistic+disputed+resolved+vindicated+quorum",
                  voters=5, distinct=3, maxop=2),
        chain_job("j3", "juge2", "settled+optimistic+disputed+resolved+vindicated+quorum",
                  voters=5, distinct=3, maxop=2),
        chain_job("j4", "cheater", "settled+optimistic+disputed+resolved+clawed+quorum",
                  slashes=[("cheater", 45000)], voters=6, distinct=3, maxop=2),
    ],
    "params": {"params": {"audit_min_quorum": "4"}},
    "audit_deferred": {"audits_opened": "3"},
    "verdicts": {"j4": {"juge1": {"vote": "0", "model": "qwen3:30b-a3b"},
                        "juge2": {"vote": "0", "model": "mistral-nemo"}}},
    "seats": {"j4": ["juge1", "juge2", "juge3", "juge4", "juge5", "juge6"]},
}
a = measure(state, cheaters=["cheater"])
chk("(2) the cheater's slash is NOT counted as a false slash of an honest miner",
    a["triplet"]["honnetes_faux_slash"]["value"] == 0)
chk("(2b) the cheater's slash is counted as POSITIVE EVIDENCE",
    a["cheater_evidence"]["hard_slash_at_quorum"] == 1
    and a["cheater_evidence"]["slashed_udndr"] == {"cheater": 45000})
chk("(2c) the honest rate is measured over RESOLVED honest audits (2), not over all of them (3)",
    a["counts"]["resolved_honest"] == 2 and a["triplet"]["hard_slash_at_quorum_rate"]["value"] == 0.0)

# --- (2d) THE SAME STATE WITHOUT DECLARING THE CHEATER: the measurement must FLIP ----------------
b = measure(state, cheaters=[])
chk("(2d) without a declared cheater, the same slash becomes a false slash (the role CHANGES everything)",
    b["triplet"]["honnetes_faux_slash"]["value"] == 1
    and b["triplet"]["hard_slash_at_quorum_rate"]["value"] == 1 / 3)

# --- (3) nothing resolved -> the rate is NOT MEASURED, never 0.0 ---------------------------------
c = measure({"jobs": [chain_job("j1", "juge1", "settled+optimistic+disputed")],
             "params": {}, "audit_deferred": {}}, cheaters=["cheater"])
chk("(3) no resolved honest audit -> rate NOT MEASURED (not a fabricated zero)",
    c["triplet"]["hard_slash_at_quorum_rate"]["measured"] is False
    and c["triplet"]["hard_slash_at_quorum_rate"]["value"] is None)
chk("(3b) and the unresolved disputed job is counted in jobs_unresolved",
    c["triplet"]["jobs_unresolved"]["value"] == 1)

# --- (4) EXACT tokens: `noquorum` must never count as `quorum` -----------------------------------
d = measure({"jobs": [chain_job("j9", "juge1", "settled+disputed+resolved+clawed+noquorum",
                                slashes=[("juge1", 10)])],
             "params": {}, "audit_deferred": {}}, cheaters=[])
chk("(4) `+noquorum` is NOT counted as a slash AT QUORUM",
    d["triplet"]["hard_slash_at_quorum_rate"]["value"] == 0.0)
chk("(4b) but the honest miner's slash_records entry is still seen (the clawback happened)",
    d["triplet"]["honnetes_faux_slash"]["value"] == 1)
chk("(4c) tokens() splits on '+'", tokens({"state": "a+b"}) == ["a", "b"])

# --- (5) cross witnesses: a divergence between tokens and slash_records is reported ---------------
e = measure({"jobs": [chain_job("j1", "juge1", "settled+disputed+resolved+clawed+quorum")],
             "params": {}, "audit_deferred": {}}, cheaters=[])
chk("(5) slash written by the tokens but ABSENT from slash_records -> inconsistency reported",
    e["cross_check"]["consistent"] is False and e["_notes"])

# --- (6) empty capture -> NOTHING is measured (fail closed), not a triplet of zeros ---------------
f = measure({"jobs": None}, cheaters=[])
chk("(6) list-job absent -> _measured False (no invented triplet)",
    f["_measured"] is False and "triplet" not in f)

# --- (7) judge diversity: empty is not the same as "no judge" -------------------------------------
chk("(7) models and judges read from the verdicts",
    a["judge_diversity"]["models_observed"] == ["mistral-nemo", "qwen3:30b-a3b"]
    and a["judge_diversity"]["judges_observed"] == ["juge1", "juge2"])

# --- (8) margins: raw INPUTS exposed, the threshold marked DERIVED (never a decision) -------------
m = a["veto_margins_observational"]["j4"]
chk("(8) the margin exposes anchored seats, invalid votes and the governed quorum",
    m["anchored_seats"] == 6 and m["invalid_votes"] == 2 and m["audit_min_quorum_param"] == 4)
chk("(8b) the derived threshold follows the chain rule max(min_quorum, ceil(2/3 seats)) = 4 at 6 seats",
    m["need_seats_DERIVED_informational"] == 4)
chk("(8c) at 7 seats the chain requires 5 — a hard-coded `inv >= 4` diverged",
    measure({"jobs": [chain_job("jx", "m", "settled+disputed", voters=7)],
             "params": {"params": {"audit_min_quorum": "4"}}, "audit_deferred": {},
             "seats": {"jx": list("abcdefg")}, "verdicts": {"jx": {}}},
            cheaters=[])["veto_margins_observational"]["jx"]["need_seats_DERIVED_informational"] == 5)
chk("(8d) the displayed decision stays the one WRITTEN by the chain",
    m["chain_wrote_hard_slash"] is True)

# --- (9) PAGINATION: the read must concatenate the pages, not stop at the first one ---------------
# The chain caps an unpaginated list at 100 entries (SDK DefaultLimit). A run of 150 jobs read in a
# single page shows 100 — silently, and always in the reassuring direction.
import c3_run_measure as M

_calls = []


def _fake_rpc(node, args, timeout=60):
    _calls.append(list(args))
    if args[0] != "list-job":
        return {}
    if "--page-key" not in args:
        return {"job": [{"job_id": f"p1-{i}"} for i in range(100)],
                "pagination": {"next_key": "SUITE"}}
    return {"job": [{"job_id": f"p2-{i}"} for i in range(50)], "pagination": {}}


_orig, M._rpc = M._rpc, _fake_rpc
try:
    got = M.rpc_all("n", "list-job", "job")
    chk("(9) rpc_all follows next_key and returns the 150 jobs (not 100)", got is not None and len(got) == 150,
        f"-> {None if got is None else len(got)}")
    chk("(9b) an explicit page-limit is requested",
        any("--page-limit" in c for c in _calls))
    M._rpc = lambda *a, **k: None
    chk("(9c) an unreadable page -> None (fail closed), never a partial list",
        M.rpc_all("n", "list-job", "job") is None)
finally:
    M._rpc = _orig

print(f"\n{OK[0]} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
