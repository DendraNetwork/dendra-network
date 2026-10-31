#!/usr/bin/env python3
"""
test_c3_gate.py — self-honesty of the readiness gate report. No dependency (`python3 test_c3_gate.py`).

The report's failing-first case is its test: on a single-job network it must return no-go. The tests
also prove that the script never pronounces `GO` (its ceiling is ATTESTED_PENDING_SIGNOFF), that
attestations must be named (an unverifiable boolean does not suffice), and that the `quorum` token
being a substring of `noquorum` does not cause over-counting.
"""
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile

# The directory of the gate under test. DENDRA_MODEA points the bench at a copy, so that both the import below
# and the lone copy of (6d) load a mutated gate, never the file shipped next to this bench.
MODEA = os.environ.get("DENDRA_MODEA", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODEA)

import c3_gate  # noqa: E402
import the_proof  # noqa: E402

CFG0 = {"min_quorum_resolved": 1, "max_deferred": 0, "min_contributors": 2,
        "distinct_voters_min": 3, "attest_operators": "", "attest_models": "", "attest_honest": ""}
# Valid named attestations, reused across cases.
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
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        _fails.append(name)


print("## (1) single job -> `NO-GO` (the report's failing-first case)")
r = c3_gate.evaluate(STATE_N1, cfg())
by = {c["key"]: c["status"] for c in r["criteria"]}
check("verdict == `NO-GO`", r["verdict"] == c3_gate.NO_GO)
check("contributors FAIL", by["contributors"] == "FAIL")
check("resolved_by_quorum FAIL", by["resolved_by_quorum"] == "FAIL")
check("vindicated FAIL", by["vindicated"] == "FAIL")
check("distinct_voters_min FAIL (nothing resolved)", by["distinct_voters_min"] == "FAIL")
check("pockets_measured PASS (preamble satisfied)", by["pockets_measured"] == "PASS")
check("the 3 attestations stay to attest", sum(
    1 for c in r["criteria"] if c["kind"] == "attestation" and c["status"] == c3_gate.NEEDS_ATTEST) == 3)

print("## (2) pockets_measured missing -> `NO-GO` on the preamble")
r = c3_gate.evaluate({**STATE_HEALTHY, "held_summary": {"oldest_age_blocks": 5}}, cfg())
check("verdict == `NO-GO`", r["verdict"] == c3_gate.NO_GO)
check("the reason names the preamble", "Preamble failed" in r["why"])

print("## (2b) missing query -> `NO-GO`, fail closed")
r = c3_gate.evaluate({**STATE_HEALTHY, "seed_health": None}, cfg())
check("verdict == `NO-GO`", r["verdict"] == c3_gate.NO_GO)
check("preamble_queries FAIL", any(
    c["key"] == "preamble_queries" and c["status"] == "FAIL" for c in r["criteria"]))

print("## (3) healthy without attestation -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg())
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)
check("every measurable PASSes", all(
    c["status"] == "PASS" for c in r["criteria"] if c["kind"] == "measurable"))
check("resolved_by_quorum=2", r["partition"]["resolved_by_quorum"] == 2)
check("vindicated=1", r["partition"]["vindicated"] == 1)
check("adjudication=1", r["partition"]["resolved_by_adjudication"] == 1)
check("sum of classes == audits_opened", r["partition_sum_vs_opened"]["sum_classes"]
      == r["partition_sum_vs_opened"]["audits_opened"])

print("## (4) healthy + 3 named attestations -> ATTESTED_PENDING_SIGNOFF (never GO), exit 30, not 0")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest=HONEST))
check("verdict == ATTESTED_PENDING_SIGNOFF", r["verdict"] == c3_gate.ATTESTED)
check("the word 'GO' is never a verdict", "GO" not in c3_gate.EXIT)
check("exit ATTESTED == 30 (non-zero)", c3_gate.EXIT[r["verdict"]] == 30)
check("no verdict exits 0", 0 not in c3_gate.EXIT.values())

print("## (4b) insufficient operator attestation (1 named) -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators="opA=key1", attest_models=MODELS, attest_honest=HONEST))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("distinct_operators stays to attest", by["distinct_operators_ge_2"] == c3_gate.NEEDS_ATTEST)
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)

print("## (4c) models named but without a MoE -> REQUIRES_ATTESTATION (refutable)")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models="mistral-nemo,llama3.1:8b", attest_honest=HONEST))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("judge_models stays to attest (no MoE)", by["judge_models_ge_2_incl_moe"] == c3_gate.NEEDS_ATTEST)
check("verdict == REQUIRES_ATTESTATION", r["verdict"] == c3_gate.NEEDS_ATTEST)

print("## (4d) fewer than 100 honest miners declared -> REQUIRES_ATTESTATION")
r = c3_gate.evaluate(STATE_HEALTHY, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest="42 honest miners"))
by = {c["key"]: c["status"] for c in r["criteria"]}
check("honest stays to attest (< 100)", by["honest_miners_ge_100"] == c3_gate.NEEDS_ATTEST)

print("## (5) a failing measurable plus attestations -> `NO-GO` (an attestation never covers a measurement)")
r = c3_gate.evaluate(STATE_N1, cfg(attest_operators=OPS, attest_models=MODELS, attest_honest=HONEST))
check("verdict == `NO-GO` despite the attestations", r["verdict"] == c3_gate.NO_GO)

print("## (6) the quorum-in-noquorum trap: exact token, no over-counting")
part, _ = c3_gate.classify_jobs([
    {"id": "a", "state": "open+paid+disputed+resolved+noquorum",
     "audit_distinct_voters": 4, "audit_voters": 8, "audit_max_operator_votes": 3},
    {"id": "b", "state": "open+paid+disputed+resolved+clawed+quorum",
     "audit_distinct_voters": 4, "audit_voters": 8, "audit_max_operator_votes": 3}], None, None, None)
check("quorum counts only b (=1)", part["resolved_by_quorum"] == 1)
check("the 'noquorum' job falls into resolved_no_quorum", part["resolved_no_quorum"] == 1)

print("## (6b) unwound audits without +disputed are in the partition, as in the feed")
# The live shape: disputed audits unwound, plus audits selected while the jury was below its floor and
# then unwound, which never carry +disputed but are counted in audits_opened.
UNW = ([{"id": f"r{i}", "state": "open+paid+optimistic+disputed+resolved+unwound"} for i in range(4)]
       + [{"id": f"w{i}", "state": "open+paid+optimistic+resolved+unwound"} for i in range(3)]
       + [{"id": "p0", "state": "open+paid+optimistic+disputed"}])
D11 = {"audits_opened": 3 + 4 + 3 + 1, "job_ids": []}
STATE_UNW = {**STATE_HEALTHY, "jobs": STATE_HEALTHY["jobs"] + UNW,
             "audit_deferred_before": dict(D11), "audit_deferred": dict(D11),
             "unwind_origin": {"noseed_unwound": [], "drawn": [], "unknown": []}}
r = c3_gate.evaluate(STATE_UNW, cfg())
s = r["partition_sum_vs_opened"]
check("the 3 undisputed unwound audits count as opened (unwound_after_deferral=3)",
      r["partition"]["unwound_after_deferral"] == 3)
check("the classes add up to audits_opened (11), so no divergence is reported",
      s["sum_classes"] == 11 and s["audits_opened"] == 11 and s["partition_ok"] is True)
check("the rendering shows no divergence", "! divergence" not in c3_gate.render(r))
feed = the_proof.build_proof(c3_gate._feed_rows(STATE_UNW["jobs"]), {}, {}, 1,
                             deferred=c3_gate._deferred(STATE_UNW["audit_deferred"]),
                             unwind_origin=STATE_UNW["unwind_origin"],
                             chain_moved=the_proof.chain_moved(STATE_UNW["audit_deferred_before"],
                                                               STATE_UNW["audit_deferred"]))["jobs"]
check("every partition field equals the feed's, for the same reads",
      all(r["partition"][k] == feed[k] for k in the_proof.DISPUTED_CLASSES + (
          "audits_opened", "unwound_after_deferral", "unwound_never_drawn", "deferred_no_jury",
          "partition_ok", "partition_bounds_ok", "partition_unmeasured_reason")))
r = c3_gate.evaluate({**STATE_UNW, "unwind_origin": None}, cfg())
s = r["partition_sum_vs_opened"]
check("origin unread -> not compared (null, with the reason), never a divergence",
      s["partition_ok"] is None and s["sum_classes"] is None and s["reason"] == "unwind origin unreadable"
      and "! divergence" not in c3_gate.render(r) and "unwound_after_deferral=?" in c3_gate.render(r))
check("... while the bounds check still answers", s["partition_bounds_ok"] is True)
D12 = {"audits_opened": 12, "job_ids": []}
r = c3_gate.evaluate({**STATE_UNW, "audit_deferred_before": D12, "audit_deferred": D12}, cfg())
check("one opened audit no state shows -> divergence", r["partition_sum_vs_opened"]["partition_ok"] is False
      and "! divergence" in c3_gate.render(r))

print("## (6c) the counter is read before and after the listing, as the feed reads it")
r = c3_gate.evaluate({**STATE_UNW, "audit_deferred": D12}, cfg())
s = r["partition_sum_vs_opened"]
check("an audit opened during the walk -> not compared, with the reason, never a divergence",
      s["partition_ok"] is None and s["partition_bounds_ok"] is None and s["chain_moved"] is True
      and s["reason"] == "the chain moved during the read" and "! divergence" not in c3_gate.render(r))
r = c3_gate.evaluate({k: v for k, v in STATE_UNW.items() if k != "audit_deferred_before"}, cfg())
s = r["partition_sum_vs_opened"]
check("no read before the listing (an older capture) -> unknown, not compared, never 'no move'",
      s["partition_ok"] is None and s["partition_bounds_ok"] is None and s["chain_moved"] is None
      and s["reason"] == "counter unread before the listing")
# read_partition_inputs: the order of the reads, and a second walk only after a move.
log, opened = [], iter([11, 12, 12, 12])


def _read_deferred():
    log.append("deferred")
    return {"audits_opened": next(opened), "job_ids": []}


def _read_jobs():
    log.append("jobs")
    return [{"id": "j1", "state": "open"}]


before, jobs, after, origin = c3_gate.read_partition_inputs(_read_deferred, _read_jobs, None)
check("counter, listing, counter; read again once after a move",
      log == ["deferred", "jobs", "deferred"] * 2 and before["audits_opened"] == 12 and after["audits_opened"] == 12)
check("... and without an RPC address the origin is unread (None), never assumed", origin is None)
log.clear()
c3_gate.read_partition_inputs(lambda: (log.append("deferred"), None)[1], _read_jobs, None)
check("an unread counter is not a move: one walk only", log == ["deferred", "jobs", "deferred"])
# The event search is bounded by the heights read around the listing, never by one height for both.
_saved = c3_gate._latest_height, c3_gate._origin
_heights, _bounds = iter([100, 105]), []
c3_gate._latest_height = lambda rpc: next(_heights)
c3_gate._origin = lambda jobs, rpc, heights: _bounds.append(heights)
try:
    c3_gate.read_partition_inputs(lambda: {"audits_opened": 1, "job_ids": []}, _read_jobs, "http://node:26657")
finally:
    c3_gate._latest_height, c3_gate._origin = _saved
check("the origin is read between the heights before and after the listing", _bounds == [(100, 105)])

print("## (6d) the gate runs with the standard library alone, as a lone copy (the launchers' forms)")
with tempfile.TemporaryDirectory() as lone:
    shutil.copy(os.path.join(MODEA, "c3_gate.py"), lone)
    with open(os.path.join(lone, "state.json"), "w", encoding="utf-8") as f:
        json.dump(STATE_N1, f)
    # -I: no user site, no `PYTHONPATH`, no script directory beyond the copy's own; -S: no site-packages.
    run = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", os.path.join(lone, "c3_gate.py"),
                          "--from-json", os.path.join(lone, "state.json")],
                         cwd=lone, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("a lone copy under -I -S prints its verdict line and exits 10 (`NO-GO`)",
      run.returncode == 10 and f"  verdict : {c3_gate.NO_GO}" in run.stdout.splitlines())
r = c3_gate.evaluate({**STATE_UNW, "audit_deferred": None}, cfg())
check("audit-deferred unanswered -> deferred_no_jury not measured (FAIL, shown '?'), never 0", any(
    c["key"] == "deferred_no_jury" and c["status"] == "FAIL" and c["observed"] == "?" for c in r["criteria"]))
check("a camelCase deferred answer reads the same",
      c3_gate._deferred({"auditsOpened": "11", "jobIds": ["x"]}) == {"job_ids": ["x"], "audits_opened": 11})

print("## (6e) no default for the move of the chain: an omitted argument is not 'the chain did not move'")
_params = inspect.signature(c3_gate.audit_partition).parameters
check("audit_partition has no parameter with a default",
      all(p.default is inspect.Parameter.empty for p in _params.values()))
check("classify_jobs has no parameter with a default",
      all(p.default is inspect.Parameter.empty for p in inspect.signature(c3_gate.classify_jobs).parameters.values()))
try:
    c3_gate.audit_partition([], {"audits_opened": 0, "job_ids": []}, None)
    _omitted = False
except TypeError:
    _omitted = True
check("calling audit_partition without chain_moved is an error, never a verdict", _omitted)
_p = c3_gate.audit_partition([], {"audits_opened": 0, "job_ids": []}, None, None)
check("chain_moved None (unknown) draws no verdict and says why",
      _p["partition_ok"] is None and _p["partition_unmeasured_reason"] == "counter unread before the listing")
_p = c3_gate.audit_partition([], {"audits_opened": 0, "job_ids": []}, None, False)
check("chain_moved False (measured: no move) is the only value that compares", _p["partition_ok"] is True)


def _patched(attrs, fn):
    """Runs fn() with the named attributes of c3_gate replaced, then restores them."""
    saved = {k: getattr(c3_gate, k) for k in attrs}
    for k, v in attrs.items():
        setattr(c3_gate, k, v)
    try:
        return fn()
    finally:
        for k, v in saved.items():
            setattr(c3_gate, k, v)


STATE_STORE = {"seed_health", "audit_deferred_before", "audit_deferred", "held_summary", "params", "jobs",
               "miners", "unwind_origin", "_source"}
D3 = {"audits_opened": 3, "job_ids": []}

print("## (8) gather (the CLI reader): every query is made, in the feed's order, and a failure stays None")


def _cli_run(answers, node="tcp://HOST:26657", rpc=None, deferred_seq=None):
    """gather() against fake dendrad queries. -> (state, the log of queries, the RPC the heights came from)."""
    log, heights_rpc = [], []
    seq = iter(deferred_seq) if deferred_seq is not None else None

    def fake_rpc(n, args, timeout=60):
        log.append((n, args[0]))
        if args[0] == "audit-deferred" and seq is not None:
            return next(seq)
        return answers.get(args[0])

    def fake_all(n, cmd, field, timeout=180, max_pages=200):
        log.append((n, cmd))
        return answers.get(cmd)

    def fake_height(r):
        heights_rpc.append(r)
        return 5000

    state = _patched({"_rpc": fake_rpc, "rpc_all": fake_all, "_latest_height": fake_height},
                     lambda: c3_gate.gather(node, rpc))
    return state, log, heights_rpc


CLI = {"committee-seed-health": STATE_HEALTHY["seed_health"], "audit-deferred": D3,
       "held-summary": STATE_HEALTHY["held_summary"], "params": STATE_HEALTHY["params"],
       "list-job": STATE_HEALTHY["jobs"], "list-miner": STATE_HEALTHY["miners"]}
st, log, hr = _cli_run(CLI)
check("the state carries exactly the keys evaluate() reads, and says it came from the CLI",
      set(st) == STATE_STORE and st["_source"] == "cli")
check("the six queries are made, to the node given",
      {q for _, q in log} == set(CLI) and {n for n, _ in log} == {"tcp://HOST:26657"})
_qs = [q for _, q in log if q in ("audit-deferred", "list-job")]
check("the counter is read before the listing and again after it", _qs == ["audit-deferred", "list-job", "audit-deferred"])
check("without --rpc the heights come from the node's address (tcp:// -> http://)",
      hr and set(hr) == {"http://HOST:26657"})
st2, _, hr2 = _cli_run(CLI, rpc="http://rpc.example:26657")
check("--rpc overrides the address derived from --node", hr2 and set(hr2) == {"http://rpc.example:26657"})
r = c3_gate.evaluate(st, cfg())
check("a gathered healthy state is evaluated like the captured one: attestation needed, partition compared",
      r["verdict"] == c3_gate.NEEDS_ATTEST and r["partition_sum_vs_opened"]["partition_ok"] is True
      and r["partition_sum_vs_opened"]["chain_moved"] is False)
for dead in ("committee-seed-health", "held-summary", "params", "list-job"):
    st, _, _ = _cli_run({k: v for k, v in CLI.items() if k != dead})
    key = {"committee-seed-health": "seed_health", "held-summary": "held_summary", "params": "params",
           "list-job": "jobs"}[dead]
    r = c3_gate.evaluate(st, cfg())
    check(f"a failing {dead} stays None and the verdict is no-go on the preamble",
          st[key] is None and r["verdict"] == c3_gate.NO_GO
          and any(c["key"] == "preamble_queries" and key in c["observed"] for c in r["criteria"]))
st, _, _ = _cli_run({k: v for k, v in CLI.items() if k != "list-job"})
check("a failing listing leaves the origin unread (None), never an empty origin", st["unwind_origin"] is None)
st, _, _ = _cli_run({k: v for k, v in CLI.items() if k != "audit-deferred"})
r = c3_gate.evaluate(st, cfg())
check("an unanswered counter: both reads None, the move is unknown (None), nothing is compared",
      st["audit_deferred_before"] is None and st["audit_deferred"] is None
      and r["partition_sum_vs_opened"]["chain_moved"] is None and r["partition_sum_vs_opened"]["partition_ok"] is None)
st, log, _ = _cli_run(CLI, deferred_seq=[{"audits_opened": 3, "job_ids": []}, {"audits_opened": 4, "job_ids": []},
                                          {"audits_opened": 4, "job_ids": []}, {"audits_opened": 4, "job_ids": []}])
check("a counter that moved during the listing is read again: the second walk's reads are kept",
      [q for _, q in log].count("list-job") == 2 and st["audit_deferred_before"]["audits_opened"] == 4
      and st["audit_deferred"]["audits_opened"] == 4)

st, log, _ = _cli_run(CLI, deferred_seq=[{"audits_opened": 3, "job_ids": []}, {"audits_opened": 4, "job_ids": []},
                                          {"audits_opened": 4, "job_ids": []}, {"audits_opened": 5, "job_ids": []}])
r = c3_gate.evaluate(st, cfg())
check("a second move is kept as read: 'before' is the earlier read, 'after' the later, and nothing is compared",
      st["audit_deferred_before"]["audits_opened"] == 4 and st["audit_deferred"]["audits_opened"] == 5
      and r["partition_sum_vs_opened"]["chain_moved"] is True and r["partition_sum_vs_opened"]["partition_ok"] is None)

print("## (8b) rpc_all: pages are walked, a failing page or an endless walk is None")
_pages = {None: {"job": [{"id": "a"}], "pagination": {"next_key": "K1"}},
          "K1": {"Job": [{"id": "b"}], "pagination": {"nextKey": "K2"}},
          "K2": {"job": [{"id": "c"}], "pagination": {"next_key": None}}}
_asked = []


def _paged_rpc(node, args, timeout=60):
    key = args[args.index("--page-key") + 1] if "--page-key" in args else None
    _asked.append(key)
    return _pages.get(key)


got = _patched({"_rpc": _paged_rpc}, lambda: c3_gate.rpc_all("tcp://x:1", "list-job", "job"))
check("three pages (snake and camel keys) are read in order and joined",
      got is not None and [j["id"] for j in got] == ["a", "b", "c"] and _asked == [None, "K1", "K2"])
_pages_bad = {**_pages, "K1": None}
got = _patched({"_rpc": lambda n, a, timeout=60: _pages_bad.get(a[a.index("--page-key") + 1] if "--page-key" in a else None)},
               lambda: c3_gate.rpc_all("tcp://x:1", "list-job", "job"))
check("a failing second page makes the whole listing None (fail closed)", got is None)
got = _patched({"_rpc": lambda n, a, timeout=60: {"job": [{"id": "z"}], "pagination": {"next_key": "same"}}},
               lambda: c3_gate.rpc_all("tcp://x:1", "list-job", "job", max_pages=3))
check("a walk that never ends stops at the page bound and answers None, not a truncated list", got is None)

print("## (9) gather_rest (the REST reader): every endpoint is read, the origin needs --rpc, a failure stays None")
REST_ONE = {"audit_deferred": D11, "committee_seed_health": STATE_UNW["seed_health"],
            "held_summary": STATE_UNW["held_summary"], "params": STATE_UNW["params"]}
REST_ALL = {"job": STATE_UNW["jobs"], "miner": STATE_UNW["miners"]}
UNW_ORIGIN = STATE_UNW["unwind_origin"]


def _rest_run(one, many, rpc=None, origin=UNW_ORIGIN):
    """gather_rest() against fake REST answers. -> (state, the log of paths, the origin reader's arguments)."""
    log, origin_args = [], []

    def fake_rest(base, path, timeout=20):
        log.append((base, path))
        return one.get(path)

    def fake_rest_all(base, path, field, max_pages=400):
        log.append((base, path))
        return many.get(path)

    def fake_origin(jobs, reader, heights=None, cache=None, max_pages=100):
        origin_args.append(heights)
        return origin

    heights = iter([100, 105])
    state = _patched({"_rest": fake_rest, "_rest_all": fake_rest_all, "read_unwind_origin": fake_origin,
                      "_latest_height": lambda r: next(heights) if r else None},
                     lambda: c3_gate.gather_rest("http://node:1317", rpc))
    return state, log, origin_args


st, log, oargs = _rest_run(REST_ONE, REST_ALL, rpc="http://node:26657")
check("the state carries exactly the keys evaluate() reads, and names the gateway it came from",
      set(st) == STATE_STORE and st["_source"] == "rest http://node:1317")
check("the six endpoints are read, all on the gateway given",
      {p for _, p in log} == {"audit_deferred", "committee_seed_health", "held_summary", "params", "job", "miner"}
      and {b for b, _ in log} == {"http://node:1317"})
_ps = [p for _, p in log if p in ("audit_deferred", "job")]
check("the counter is read before the listing and again after it", _ps == ["audit_deferred", "job", "audit_deferred"])
check("with --rpc the origin is read between the heights taken around the listing", oargs == [(100, 105)])
r = c3_gate.evaluate(st, cfg())
check("with the origin read, the partition is compared and holds",
      r["partition_sum_vs_opened"]["partition_ok"] is True and r["partition"]["unwound_after_deferral"] == 3)
st, _, oargs = _rest_run(REST_ONE, REST_ALL, rpc=None)
r = c3_gate.evaluate(st, cfg())
check("without --rpc no origin is read (None), the partition is not compared, with the reason",
      st["unwind_origin"] is None and oargs == [] and r["partition_sum_vs_opened"]["partition_ok"] is None
      and r["partition_sum_vs_opened"]["reason"] == "unwind origin unreadable")
check("... and an unreadable address gives no height", c3_gate._latest_height(None) is None)
for dead in ("committee_seed_health", "held_summary", "params", "audit_deferred"):
    st, _, _ = _rest_run({k: v for k, v in REST_ONE.items() if k != dead}, REST_ALL, rpc="http://node:26657")
    r = c3_gate.evaluate(st, cfg())
    check(f"a failing {dead} stays None and the verdict is no-go",
          r["verdict"] == c3_gate.NO_GO and any(c["key"] == "preamble_queries" for c in r["criteria"]))
st, _, _ = _rest_run(REST_ONE, {"miner": REST_ALL["miner"]}, rpc="http://node:26657")
check("a failing listing leaves the origin unread (None) and the verdict no-go",
      st["jobs"] is None and st["unwind_origin"] is None
      and c3_gate.evaluate(st, cfg())["verdict"] == c3_gate.NO_GO)

print("## (9b) _rest_all: pages are walked, a failing page or an endless walk is None")
_rpages = {None: {"job": [{"id": "a"}], "pagination": {"next_key": "K1"}},
           "K1": {"Job": [{"id": "b"}], "pagination": {"nextKey": None}}}
_rasked = []


def _paged_rest(base, path, timeout=20):
    key = path.split("pagination.key=")[1] if "pagination.key=" in path else None
    _rasked.append(path)
    return _rpages.get(key)


got = _patched({"_rest": _paged_rest}, lambda: c3_gate._rest_all("http://x", "job", "job"))
check("two pages are read, the second asked with the first one's key",
      got is not None and [j["id"] for j in got] == ["a", "b"] and "pagination.key=K1" in _rasked[1])
got = _patched({"_rest": lambda b, p, timeout=20: None if "pagination.key=" in p else _rpages[None]},
               lambda: c3_gate._rest_all("http://x", "job", "job"))
check("a failing second page makes the whole listing None (fail closed)", got is None)
got = _patched({"_rest": lambda b, p, timeout=20: {"job": [], "pagination": {"next_key": "same"}}},
               lambda: c3_gate._rest_all("http://x", "job", "job", max_pages=3))
check("a walk that never ends stops at the page bound and answers None", got is None)

print("## (9c) a run that crashes prints no verdict line and exits with a code no verdict uses")
# dendra_preuve_archive.sh reads the verdict line and the exit code together; a crash must give neither.
with tempfile.TemporaryDirectory() as lone:
    shutil.copy(os.path.join(MODEA, "c3_gate.py"), lone)
    run = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", os.path.join(lone, "c3_gate.py"),
                          "--from-json", os.path.join(lone, "absent.json")],
                         cwd=lone, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("a missing capture: no 'verdict :' line on stdout, and an exit code outside the verdict codes",
      not any(line.startswith("  verdict :") for line in run.stdout.splitlines())
      and run.returncode not in c3_gate.EXIT.values() and run.returncode != 0)

print("## (10) exit codes: never 0, GO absent")
check("NO-GO=10", c3_gate.EXIT[c3_gate.NO_GO] == 10)
check("REQUIRES_ATTESTATION=20", c3_gate.EXIT[c3_gate.NEEDS_ATTEST] == 20)
check("ATTESTED_PENDING_SIGNOFF=30", c3_gate.EXIT[c3_gate.ATTESTED] == 30)
check("no exit code is 0", 0 not in c3_gate.EXIT.values())

print()
if _fails:
    print(f"FAILED: {len(_fails)} assertion(s) -> {_fails}")
    raise SystemExit(1)
print("All green — the readiness report is honest: `NO-GO` on a single job, never a `GO` "
      "(ceiling ATTESTED_PENDING_SIGNOFF), and named attestations.")
