"""Bench of the audit readers of the exporter and the points indexer.

Both read a job's audit through c3_gate.py::audit_class over the parsed state markers, the classification
the feed and the readiness report publish, instead of a word searched in the state string. What that
changes, and what this bench holds:
  * an unwound audit (audit_unwind.go::unwindStrandedRetention: the client refunded, the miner neither paid
    nor slashed) is its own R2 bucket, never `counted`: its fee is not demand and the job is not work;
  * no verdict on an unwound audit is consistent, so a judge's "1" on one scores nothing, the same set
    final_season_facts.py::audit_outcome scores;
  * an unwound audit is never a hard slash on the launch gauges, and an undisputed unwind has no jury to scan;
  * the two copies of r2_job_bucket stay equal.
Each case drives the shipped module, loaded from its file. The mutants at the end load a copy of one shipped
file with one decision undone and expect a failure; a mutant whose anchor is gone fails on its own.
"""
import importlib.util
import itertools
import json
import os
import sys
import types

import pytest

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# `DENDRA_MODEA_DIR` puts a mutated copy of one module first on the path (the mutation benches of the
# development repository); the shipped directory stays behind it for the modules the copy does not hold. A
# copy that holds none of the modules this bench drives is refused: the bench would load the shipped files
# and pass with every sign of a measurement it did not make.
SUBJECTS = ("exporter.py", "points_indexer.py", "c3_gate.py")
if MODEA and not any(os.path.isfile(os.path.join(MODEA, s)) for s in SUBJECTS):
    raise FileNotFoundError(f"DENDRA_MODEA_DIR={MODEA}: none of {', '.join(SUBJECTS)} is there, "
                            f"this bench would measure the shipped files instead")
sys.path.insert(0, HERE)
if MODEA:
    sys.path.insert(0, MODEA)

import c3_gate  # noqa: E402
import final_season_facts  # noqa: E402


def _shipped(name):
    for d in (MODEA, HERE):
        p = os.path.join(d, name + ".py") if d else ""
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError(name)


class _Gauge:
    def __init__(self, *a, **k):
        self.v, self.children = None, {}

    def set(self, v):
        self.v = v

    def labels(self, **kw):
        return self.children.setdefault(tuple(sorted(kw.items())), _Gauge())

    def clear(self):
        self.children.clear()


_SEQ = itertools.count()


def _load(path, stem):
    """The module at `path`, executed under a fresh name. sys.path is restored: a shipped module inserts
    its own directory at the front, and a copy's directory must not outlive its case."""
    saved = list(sys.path)
    fake = types.ModuleType("prometheus_client")
    fake.Gauge, fake.start_http_server = _Gauge, (lambda *a, **k: None)
    prev = sys.modules.get("prometheus_client")
    sys.modules["prometheus_client"] = fake
    try:
        spec = importlib.util.spec_from_file_location(f"{stem}_{next(_SEQ)}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved
        if prev is None:
            sys.modules.pop("prometheus_client", None)
        else:
            sys.modules["prometheus_client"] = prev


# The states the chain writes, from the writers in chain/x/jobs/keeper (settlement, dispute, audit
# resolution, adjudication, unwind, expiry), plus the legacy `+noquorum` and bare `+resolved+clawed`.
PREFIXES = ["open", "open+paid+finalized", "open+paid+optimistic", "open+settled", "open+expired+refunded",
            "open+verified"]
DISPUTES = ["", "+disputed"]
RESOLUTIONS = ["", "+resolved", "+resolved+clawed+quorum", "+resolved+vindicated+quorum", "+resolved+adjudicated",
               "+resolved+unwound", "+resolved+clawed+noquorum", "+resolved+clawed"]
STATES = [p + d + r for p in PREFIXES for d in DISPUTES for r in RESOLUTIONS]
OPS, EXC = {"opA"}, {"sub"}
CLIENTS = ["ext", "opA", "sub"]

BUCKET_TABLE = [
    ("open", "ext", "unpaid"),
    ("open+expired+refunded", "ext", "unpaid"),
    ("open+paid+finalized", "ext", "counted"),
    ("open+settled", "ext", "counted"),
    ("open+paid+optimistic+disputed", "ext", "pending"),
    ("open+paid+optimistic+disputed", "sub", "pending"),
    ("open+paid+optimistic+disputed+resolved", "ext", "counted"),
    ("open+paid+optimistic+disputed+resolved+vindicated+quorum", "ext", "counted"),
    ("open+paid+optimistic+disputed+resolved+adjudicated", "ext", "counted"),
    ("open+paid+optimistic+disputed+resolved+clawed+quorum", "ext", "clawed"),
    ("open+paid+optimistic+disputed+resolved+clawed+quorum", "sub", "clawed"),
    ("open+paid+optimistic+disputed+resolved+unwound", "ext", "unwound"),
    ("open+paid+optimistic+disputed+resolved+unwound", "opA", "unwound"),
    ("open+paid+optimistic+resolved+unwound", "ext", "unwound"),
    ("open+paid+optimistic+resolved+unwound", "sub", "unwound"),
    ("open+paid+finalized", "sub", "subsidized"),
    ("open+paid+finalized", "opA", "operator_client"),
]


# -- the checks, each returning the list of what failed ------------------------------------------------
def check_buckets(ex):
    bad = []
    for st, cl, want in BUCKET_TABLE:
        got = ex.r2_job_bucket(st, cl, OPS, EXC)
        if got != want:
            bad.append(f"r2_job_bucket({st!r}, {cl!r}) = {got!r}, expected {want!r}")
    fees, _, buckets = ex.compute_r2([{"state": st, "client": cl, "fee": 10} for st, cl, _ in BUCKET_TABLE],
                                     OPS, EXC, 1000)
    if buckets.get("unwound") != 4 or fees != 50:
        bad.append(f"compute_r2: fees={fees} (expected 50), unwound={buckets.get('unwound')} (expected 4)")
    return bad


def check_copies_equal(ex, pi):
    bad = []
    for st in STATES:
        for cl in CLIENTS:
            a, b = ex.r2_job_bucket(st, cl, OPS, EXC), pi.r2_job_bucket(st, cl, OPS, EXC)
            if a != b:
                bad.append(f"{st!r} client={cl}: exporter {a!r} != points {b!r}")
    if tuple(ex.R2_BUCKETS) != tuple(pi.R2_BUCKETS):
        bad.append(f"R2_BUCKETS differ: {ex.R2_BUCKETS} != {pi.R2_BUCKETS}")
    return bad


def check_launch(ex):
    jobs = [
        {"id": "q1", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum"},
        {"id": "u1", "state": "open+paid+optimistic+disputed+resolved+unwound"},
        {"id": "p1", "state": "open+paid+optimistic+disputed"},
        {"id": "uu", "state": "open+paid+optimistic+resolved+unwound"},
        {"id": "f1", "state": "open+paid+finalized"},
    ]
    scanned = []

    def verdicts_of(jid, mids):
        scanned.append(jid)
        return ({f"j{i}": "0" for i in range(4)} if jid in ("q1", "u1") else {}), {"model-a"}
    ex._verdicts_of = verdicts_of
    ex._L_VERDICT_CACHE.clear()
    ex._L_SCAN_MAX = 50
    ex._refresh_launch(jobs, ["j0", "j1", "j2", "j3"])
    bad = []
    if ex.g_l_hard.v != 1:
        bad.append(f"hard slashes = {ex.g_l_hard.v}, expected 1 (the quorum clawback only, never the unwound audit)")
    if ex.g_l_unres.v != 1:
        bad.append(f"unresolved = {ex.g_l_unres.v}, expected 1 (the pending audit)")
    if ex.g_l_maxinv.v != 4:
        bad.append(f"max invalid without quorum = {ex.g_l_maxinv.v}, expected 4 (the unwound audit)")
    if sorted(scanned) != ["p1", "q1", "u1"]:
        bad.append(f"scanned {sorted(scanned)}, expected the three disputed jobs (an undisputed unwind has no jury)")
    return bad


POINT_JOBS = [
    {"id": "jc", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "miner_id": "mC", "client": "extC",
     "fee": 900, "slashes": [{"miner_id": "mC", "amount": 5}]},
    {"id": "jv", "state": "open+paid+optimistic+disputed+resolved+vindicated+quorum", "miner_id": "mV",
     "client": "extV", "fee": 700, "slashes": []},
    {"id": "ju", "state": "open+paid+optimistic+disputed+resolved+unwound", "miner_id": "mU", "client": "extU",
     "fee": 650, "slashes": []},
    {"id": "juu", "state": "open+paid+optimistic+resolved+unwound", "miner_id": "mU", "client": "extU",
     "fee": 300, "slashes": []},
    {"id": "jp", "state": "open+paid+optimistic+disputed", "miner_id": "mP", "client": "extP", "fee": 400,
     "slashes": []},
    {"id": "jf", "state": "open+paid+finalized", "miner_id": "mV", "client": "extV", "fee": 100, "slashes": []},
]


def check_points(pi):
    verdicts = {"jc": {"J": "0"}, "jv": {"J": "1"}, "ju": {"J": "1"}, "juu": {"J": "1"}, "jp": {"J": "1"}}
    rows, _ = pi.compute_points(POINT_JOBS, [{"id": "J", "operator": "opJ"}], verdicts, cap_bps=10000,
                                owner_cap_bps=10000, use_share_max_bps=10000)
    by = {r["id"]: r["detail"] for r in rows}
    bad = []
    if by.get("J", {}).get("judge") != 400.0:
        bad.append(f"judge J = {by.get('J')}, expected 2 x 200 (jc '0' and jv '1'; nothing on ju, juu, jp)")
    if "mU" in by:
        bad.append(f"the miner of the unwound jobs earned {by['mU']}")
    if "client:extU" in by:
        bad.append(f"the refunded client of the unwound jobs earned {by['client:extU']}")
    if "mP" in by:
        bad.append(f"the miner of a pending audit earned {by['mP']}")
    if by.get("mV", {}).get("work") != 800.0:
        bad.append(f"mV work = {by.get('mV')}, expected 800 (vindicated 700 + finalized 100)")
    return bad


def check_season_alignment(pi):
    bad = []
    for st in STATES:
        a, b = pi.audit_issue(st), final_season_facts.audit_outcome({"state": st})
        if a != b:
            bad.append(f"{st!r}: audit_issue {a!r} != final_season_facts.audit_outcome {b!r}")
    return bad


def check_fetch(pi, monkeypatch):
    queried = []

    def run(cmd, **kw):
        key = next(a for a in cmd if "__verdict__" in a)
        queried.append(key.split("__verdict__")[0])
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"commit": {"result_commit": "1"}}), stderr="")
    monkeypatch.setattr(pi, "subprocess", types.SimpleNamespace(run=run))
    monkeypatch.delenv("DENDRA_NODE", raising=False)
    out = pi._fetch_verdicts(POINT_JOBS, [{"id": "J"}])
    bad = []
    if sorted(queried) != ["jc", "jv"]:
        bad.append(f"verdicts queried for {sorted(queried)}, expected only the concluded audits jc and jv")
    if sorted(out) != ["jc", "jv"]:
        bad.append(f"verdicts kept for {sorted(out)}")
    return bad


@pytest.fixture()
def shipped():
    return _load(_shipped("exporter"), "exporter"), _load(_shipped("points_indexer"), "points")


# -- the shipped modules ---------------------------------------------------------------------------------
def test_buckets_read_the_audit_class(shipped):
    assert check_buckets(shipped[0]) == []


def test_the_two_bucket_copies_are_equal(shipped):
    assert check_copies_equal(*shipped) == []


def test_launch_gauges_never_count_an_unwound_audit_as_a_hard_slash(shipped):
    assert check_launch(shipped[0]) == []


def test_no_point_for_an_unwound_audit(shipped):
    assert check_points(shipped[1]) == []


def test_judge_outcome_matches_the_season_outcome(shipped):
    assert check_season_alignment(shipped[1]) == []


def test_verdicts_are_fetched_only_for_concluded_audits(shipped, monkeypatch):
    assert check_fetch(shipped[1], monkeypatch) == []


def test_no_word_predicate_left_on_the_state(shipped):
    # The readers parse the markers; a substring test on the state string is what this bench replaced.
    for name in ("exporter", "points_indexer"):
        with open(_shipped(name), encoding="utf-8") as f:
            src = f.read()
        for word in ("disputed", "resolved", "clawed", "unwound"):
            for form in (f'"{word}" in st', f'"{word}" not in st'):
                assert form not in src, f"{name}.py still reads `{form}`"


def test_every_disputed_class_is_known_here():
    # A class c3_gate adds later lands in no set here; this case names it instead of letting it count.
    known = {"audit_pending", "resolved_by_quorum", "resolved_no_quorum", "resolved_by_adjudication",
             "resolved_unwound", "resolved_unmarked"}
    assert set(c3_gate.DISPUTED_CLASSES) == known


# -- mutants: one decision undone in a copy of one shipped file, each must be caught ------------------------
MUTANTS = [
    ("exporter", "unwound bucket counted",
     '        return "unwound"           # trade undone, client refunded -> never counts', '        return "counted"'),
    ("exporter", "pending bucket dropped", '    if cls == "audit_pending":', '    if cls == "audit_pending" and False:'),
    ("exporter", "unwound audit counted as a hard slash",
     "            if resolved and inv >= 4 and cls not in UNWOUND_CLASSES:", "            if resolved and inv >= 4:"),
    ("exporter", "undisputed unwind scanned",
     "            if cls is not None and cls != UNDISPUTED_UNWOUND:", "            if cls is not None:"),
    ("exporter", "pending read as resolved",
     '            resolved = cls != "audit_pending"', "            resolved = cls is not None"),
    ("points_indexer", "unwound bucket counted", '        return "unwound"', '        return "counted"'),
    ("points_indexer", "pending bucket dropped", '    if cls == "audit_pending":', '    if cls == "audit_pending" and False:'),
    ("points_indexer", "unwound audit scored",
     '"resolved_unmarked"))', '"resolved_unmarked", "resolved_unwound"))'),
    ("points_indexer", "clawback read as cleared", '    return "0" if "clawed" in mk else "1"', '    return "1"'),
    ("points_indexer", "verdicts fetched for every job",
     '        if audit_issue(j.get("state")) is None:', "        if False:"),
]


@pytest.mark.parametrize("name,label,old,new", MUTANTS, ids=[f"{m[0]}:{m[1]}" for m in MUTANTS])
def test_mutant_is_caught(name, label, old, new, tmp_path, monkeypatch):
    with open(_shipped(name), encoding="utf-8", newline="") as f:
        src = f.read()
    assert src.count(old) == 1, f"mutant anchor gone or ambiguous in {name}.py: {old!r}"
    path = tmp_path / (name + ".py")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(old, new))
    mutated = _load(str(path), "mutant")
    if name == "exporter":
        other = _load(_shipped("points_indexer"), "points")
        bad = check_buckets(mutated) + check_copies_equal(mutated, other) + check_launch(mutated)
    else:
        other = _load(_shipped("exporter"), "exporter")
        bad = (check_points(mutated) + check_copies_equal(other, mutated) + check_season_alignment(mutated)
               + check_fetch(mutated, monkeypatch))
    assert bad, f"mutant survived: {label}"
