#!/usr/bin/env python3
"""
test_c3_gate.py — self-honesty of the readiness gate report. No dependency (`python3 test_c3_gate.py`).

The report's failing-first case IS its test: on a single-job network it MUST return NO-GO. The tests
also prove that the script NEVER pronounces `GO` (its ceiling is ATTESTED_PENDING_SIGNOFF), that
attestations must be NAMED — an unverifiable boolean does not suffice — and that the `quorum` token
being a substring of `noquorum` does not cause over-counting.
"""
import c3_gate

CFG0 = {"min_quorum_resolved": 1, "max_deferred": 0, "min_contributors": 2,
        "distinct_voters_min": 3, "attest_operators": "", "attest_models": "", "attest_honest": ""}
# Valid NAMED attestations, reused across cases.
OPS = "opA=key1,opB=key2"
MODELS = "qwen3:30b-a3b,mistral-nemo"
HONEST = "120 honest miners in the run"


def cfg(**over):
    c = dict(CFG0)
    c.update(over)
    return c


STATE_N1 = {
    "seed_health": {"committee_seed_source": 1, "latest_contributors": 1, "current_height": 200},
    "audit_deferred": {"audits_opened": 1},
    "held_summary": {"pockets_measured": 3, "held_count": 1, "oldest_age_blocks": 5},
    "params": {"params": {"miner_prune_blocks": 100}},
    "jobs": [{"id": "job-1", "state": "open+paid+optimistic+disputed"}],
    "miners": [{"miner_id": "mA", "stake": 1000}],
}

STATE_HEALTHY = {
    "seed_health": {"committee_seed_source": 1, "latest_contributors": 3, "current_height": 5000},
    "audit_deferred": {"audits_opened": 3, "job_ids": []},
    "held_summary": {"pockets_measured": 3, "oldest_age_blocks": 5, "oldest_lock_age_blocks": 0},
    "params": {"params": {"miner_prune_blocks": 100}},
    "jobs": [
        {"id": "j1", "state": "open+paid+optimistic+disputed+resolved+vindicated+quorum",
         "audit_distinct_voters": 5, "audit_voters": 10, "audit_max_operator_votes": 4},
        {"id": "j2", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum",
         "audit_distinct_voters": 4, "audit_voters": 9, "audit_max_operator_votes": 4},
        {"id": "j3", "state": "open+paid+optimistic+disputed+resolved+adjudicated",
         "audit_distinct_voters": 3, "audit_voters": 3, "audit_max_operator_votes": 1},
    ],
    "miners": [{"miner_id": f"m{i}"} for i in range(120)],
}

_fails = []


def check(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        _fails.append(name)


print("## (1) single job -> NO-GO (the report's failing-first case)")
r = c3_gate.evaluate(STATE_N1, cfg())
by = {c["key"]: c["status"] for c in r["criteria"]}
check("verdict == NO-GO", r["verdict"] == c3_gate.NO_GO)
check("contributors FAIL", by["contributors"] == "FAIL")
check("resolved_by_quorum FAIL", by["resolved_by_quorum"] == "FAIL")
check("vindicated FAIL", by["vindicated"] == "FAIL")
check("distinct_voters_min FAIL (nothing resolved)", by["distinct_voters_min"] == "FAIL")
check("pockets_measured PASS (preamble satisfied)", by["pockets_measured"] == "PASS")
check("the 3 attestations stay TO ATTEST", sum(
    1 for c in r["criteria"] if c["kind"] == "attestation" and c["status"] == c3_gate.NEEDS_ATTEST) == 3)

print("## (2) pockets_measured missing -> NO-GO on the preamble")
r = c3_gate.evaluate({**STATE_HEALTHY, "held_summary": {"oldest_age_blocks": 5}}, cfg())
check("verdict == NO-GO", r["verdict"] == c3_gate.NO_GO)
check("the reason names the preamble", "PREAMBLE" in r["why"])

print("## (2b) missing query -> NO-GO, fail closed")
r = c3_gate.evaluate({**STATE_HEALTHY, "seed_health": None}, cfg())
check("verdict == NO-GO", r["verdict"] == c3_gate.NO_GO)
check("preambule_queries FAIL", any(
    c["key"] == "preambule_queries" and c["status"] == "FAIL" for c in r["criteria"]))

print("## (3) healthy WITHOUT attestation -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg())
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)
check("every measurable PASSes", all(
    c["status"] == "PASS" for c in r["criteria"] if c["kind"] == "measurable"))
check("resolved_by_quorum=2", r["partition"]["resolved_by_quorum"] == 2)
check("vindicated=1", r["partition"]["vindicated"] == 1)
check("adjudication=1", r["partition"]["resolved_by_adjudication"] == 1)
check("sum of classes == audits_opened", r["partition_sum_vs_opened"]["sum_classes"]
      == r["partition_sum_vs_opened"]["audits_opened"])

print("## (4) healthy + 3 NAMED attestations -> ATTESTED_PENDING_SIGNOFF (NEVER GO), exit 30, NOT 0")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest=HONEST))
check("verdict == ATTESTED_PENDING_SIGNOFF", r["verdict"] == c3_gate.ATTESTED)
check("the word 'GO' is NEVER a verdict", "GO" not in c3_gate.EXIT)
check("exit ATTESTED == 30 (non-zero)", c3_gate.EXIT[r["verdict"]] == 30)
check("NO verdict exits 0", 0 not in c3_gate.EXIT.values())

print("## (4b) INSUFFICIENT operator attestation (1 named) -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators="opA=key1", attest_models=MODELS, attest_honest=HONEST))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("distinct_operators stays TO ATTEST", by["distinct_operators_ge_2"] == c3_gate.NEEDS_ATTEST)
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)

print("## (4c) models NAMED but WITHOUT a MoE -> REQUIRES_ATTESTATION (refutable)")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models="mistral-nemo,llama3.1:8b", attest_honest=HONEST))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("judge_models stays TO ATTEST (no MoE)", by["judge_models_ge_2_incl_moe"] == c3_gate.NEEDS_ATTEST)
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)

print("## (4d) fewer than 100 honest miners declared -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest="42 honest miners"))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("honest stays TO ATTEST (< 100)", by["honest_miners_ge_100"] == c3_gate.NEEDS_ATTEST)

print("## (5) a failing measurable plus attestations -> NO-GO (an attestation never covers a measurement)")
r = c3_gate.evaluate(STATE_N1, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest=HONEST))
check("verdict == NO-GO despite the attestations", r["verdict"] == c3_gate.NO_GO)

print("## (6) the quorum-in-noquorum trap: exact token, no over-counting")
part, _ = c3_gate.classify_jobs([
    {"id": "a", "state": "open+paid+disputed+resolved+noquorum",
     "audit_distinct_voters": 4, "audit_voters": 8, "audit_max_operator_votes": 3},
    {"id": "b", "state": "open+paid+disputed+resolved+clawed+quorum",
     "audit_distinct_voters": 4, "audit_voters": 8, "audit_max_operator_votes": 3}], [])
check("quorum counts ONLY b (=1)", part["resolved_by_quorum"] == 1)
check("the 'noquorum' job falls into resolved_other", part["resolved_other"] == 1)

print("## (7) exit codes: never 0, GO absent")
check("NO-GO=10", c3_gate.EXIT[c3_gate.NO_GO] == 10)
check("REQUIRES_ATTESTATION=20", c3_gate.EXIT[c3_gate.NEEDS_ATTEST] == 20)
check("ATTESTED_PENDING_SIGNOFF=30", c3_gate.EXIT[c3_gate.ATTESTED] == 30)
check("no exit code is 0", 0 not in c3_gate.EXIT.values())

print()
if _fails:
    print(f"FAILED: {len(_fails)} assertion(s) -> {_fails}")
    raise SystemExit(1)
print("ALL GREEN — the readiness report is honest: NO-GO on a single job, NEVER a GO "
      "(ceiling ATTESTED_PENDING_SIGNOFF), and named attestations.")
