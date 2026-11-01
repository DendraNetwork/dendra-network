# -*- coding: utf-8 -*-
"""A declared judge the chain no longer draws, declared again by the daemon: miner.read_judge_limits,
judge_exclusion, judge_answer_ready, judge_return_action and JudgeSeat._return.

What is pinned, each with its counterpart:
  * The chain's limits: an absent field reads 0 and selects the next value, as keeper/judge.go does (the cap its
    compiled 3, the window juror_freshness_blocks, then the compiled freshness); anything not read is no limit.
  * The chain's predicate (keeper/judge.go::eligibleAsJudgeAt): excluded at the cap and not one below it; excluded one
    block past the window and not at its edge; either date inside the window keeps the judge; the cap is read first.
    A field, a limit or a height that was not read is unknown, never a candidate and never excluded.
  * `true` only for a declared judge the chain excludes, with its role on, not draining, on epoch 15, registered,
    and a judge that can answer now; never when that readiness is False or not read.
  * The readiness: a judge worker runs with its model (its judge state tied to its live process), the judge engine
    lists that model and the memory guard lets it be used; no guard, no running worker, a guard that names another
    model, a reading that raises are not read; nothing is asked of the engine without a guard and a worker.
  * Turn by turn: one `true` per exclusion, each outcome said once, the heartbeat says why, and a judge this process
    sent back, seated since on a concluded audit it left unanswered and excluded again (by the count or by the
    window), is not sent back again; a return whose fate was not read is recorded from the next reading.

The subject can be a mutated copy (DENDRA_DAEMON_FILE).
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402

# The shipped worker reader, kept before any test replaces it.
_REAL_WORKER = md._judge_worker_model
NL = chr(10)
TXH = "AB" * 32
OK_BCAST = "code: 0" + NL + "txhash: " + TXH + NL
INCLUDED = NL.join(["code: 0", 'height: "42"', "txhash: " + TXH])
HELP_E15_DECLARE = NL.join(["Enter (true) or leave (false) the jury pool for a miner, signed by its operator",
                            "", "Usage:", "  dendrad tx jobs declare-judge [miner_id] [judge] [flags]", "", "Flags:"])
A, N = md.EPOCH_APPLIED, md.EPOCH_NOT_APPLIED
D, ND = md.JUDGE_DECLARED, md.JUDGE_NOT_DECLARED
CAP, WINDOW = 3, 100
LIMITS = {"missed_draws_max": CAP, "window_blocks": WINDOW}


@pytest.fixture(autouse=True)
def _quiet_status(monkeypatch, tmp_path):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    monkeypatch.setattr(md, "NODE", "")


class Runner:
    """miner.run_rc, answering by the sub-command; records every command."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, c, t=None):
        c = [str(x) for x in c]
        self.calls.append(c)
        key = "help:" + c[-2] if c[-1] == "--help" else c[3] if len(c) > 3 else ""
        ans = self.answers.get(key, (1, "", "the bench answers no other command"))
        return ans() if callable(ans) else ans


def F(declared_height=40, last_verdict_height=0, missed_draws=0):
    return {"declared": True, "declared_height": declared_height, "last_verdict_height": last_verdict_height,
            "missed_draws": missed_draws}


# == The chain's limits ===============================================================================================
def _limits(params, rc=0):
    r = Runner(params=(rc, json.dumps({"params": params}) if isinstance(params, dict) else params, ""))
    got = md.read_judge_limits(r)
    return got, r


@pytest.mark.parametrize("params,want", [
    ({}, {"missed_draws_max": md.JUDGE_MISSED_DRAWS_MAX_COMPILED,
          "window_blocks": md.JUROR_FRESHNESS_BLOCKS_COMPILED}),           # proto3 omits the zeros: compiled values
    ({"judge_missed_draws_max": "0", "judge_verdict_window_blocks": "0", "juror_freshness_blocks": "0"},
     {"missed_draws_max": md.JUDGE_MISSED_DRAWS_MAX_COMPILED, "window_blocks": md.JUROR_FRESHNESS_BLOCKS_COMPILED}),
    ({"judge_missed_draws_max": "5"}, {"missed_draws_max": 5, "window_blocks": md.JUROR_FRESHNESS_BLOCKS_COMPILED}),
    ({"juror_freshness_blocks": "700"}, {"missed_draws_max": md.JUDGE_MISSED_DRAWS_MAX_COMPILED,
                                         "window_blocks": 700}),            # the window at 0 is the freshness
    ({"judge_verdict_window_blocks": "900", "juror_freshness_blocks": "700"},
     {"missed_draws_max": md.JUDGE_MISSED_DRAWS_MAX_COMPILED, "window_blocks": 900}),
    ({"judge_missed_draws_max": 4, "judge_verdict_window_blocks": 50, "min_stake": "1"},
     {"missed_draws_max": 4, "window_blocks": 50}),
])
def test_the_limits_read_as_the_chain_applies_them(params, want):
    (got, why), r = _limits(params)
    assert got == want and why == ""
    assert r.calls[0][:6] == ["dendrad", "query", "jobs", "params", "--output", "json"]


@pytest.mark.parametrize("params,rc", [
    ({}, 1),                                                   # the query failed
    ("params: {}", 0),                                         # not the JSON asked for
    ("[]", 0),
    (json.dumps({"other": {}}), 0),                            # no params in the answer
    ({"judge_missed_draws_max": "many"}, 0),
    ({"judge_verdict_window_blocks": "-3"}, 0),
    ({"juror_freshness_blocks": True}, 0),
    ({"judge_missed_draws_max": None}, 0),
])
def test_limits_that_were_not_read_are_none(params, rc):
    (got, why), _r = _limits(params, rc)
    assert got is None and why


def test_the_compiled_values_are_the_chain_s():
    # The copies are held equal to the Go constants by dendra/onchain-staging/dendra_judge_defaults_test.py; this
    # pins the values the cases above derive from.
    assert (md.JUDGE_MISSED_DRAWS_MAX_COMPILED, md.JUROR_FRESHNESS_BLOCKS_COMPILED) == (3, 259200)


# == The chain's predicate ============================================================================================
@pytest.mark.parametrize("fields,height,word", [
    (F(missed_draws=CAP), 41, md.EXCLUDED_MISSED),                         # at the cap
    (F(missed_draws=CAP + 4), 41, md.EXCLUDED_MISSED),
    (F(missed_draws=CAP - 1), 41, md.EXCLUSION_NONE),                      # one below it
    (F(missed_draws=CAP, declared_height=10), 5000, md.EXCLUDED_MISSED),  # the cap is read before the window
    (F(declared_height=1000), 1000 + WINDOW, md.EXCLUSION_NONE),            # the window's edge: still a candidate
    (F(declared_height=1000), 1000 + WINDOW + 1, md.EXCLUDED_WINDOW),       # one block past it
    (F(declared_height=10, last_verdict_height=1000), 1000 + WINDOW, md.EXCLUSION_NONE),   # the last verdict keeps it
    (F(declared_height=10, last_verdict_height=1000), 1000 + WINDOW + 1, md.EXCLUDED_WINDOW),
    (F(declared_height=1000, last_verdict_height=10), 1000 + WINDOW, md.EXCLUSION_NONE),   # the declaration keeps it
    (F(declared_height=2000), 1500, md.EXCLUSION_NONE),                     # a date after the height is recent
    (F(declared_height=0, last_verdict_height=0), WINDOW, md.EXCLUSION_NONE),
    (F(declared_height=0, last_verdict_height=0), WINDOW + 1, md.EXCLUDED_WINDOW),
])
def test_the_predicate_is_the_chain_s(fields, height, word):
    got, why = md.judge_exclusion(fields, LIMITS, height)
    assert got == word, (fields, height, why)
    assert bool(why) == (word != md.EXCLUSION_NONE)


@pytest.mark.parametrize("fields,limits,height", [
    ({"declared": True, "declared_height": 1000, "last_verdict_height": 0}, LIMITS, 1001),   # missed_draws absent
    (F(missed_draws=None), LIMITS, 1001),
    (F(missed_draws="3"), LIMITS, 1001),
    (F(missed_draws=True), LIMITS, 1001),
    (F(declared_height=-1), LIMITS, 1001),
    (F(last_verdict_height=None, missed_draws=CAP), LIMITS, 1001),
    ({}, LIMITS, 1001),
    (None, LIMITS, 1001),
    (F(missed_draws=CAP), None, 1001),                                    # the limits were not read
    (F(missed_draws=CAP), {"missed_draws_max": CAP}, 1001),
    (F(missed_draws=CAP), {"missed_draws_max": 0, "window_blocks": WINDOW}, 1001),
    (F(missed_draws=CAP), {"missed_draws_max": CAP, "window_blocks": None}, 1001),
    (F(missed_draws=CAP), LIMITS, None),                                  # the height was not read
    (F(missed_draws=CAP), LIMITS, 0),
    (F(missed_draws=CAP), LIMITS, True),
    (F(missed_draws=CAP), LIMITS, "1001"),
])
def test_what_was_not_read_is_unknown(fields, limits, height):
    got, why = md.judge_exclusion(fields, limits, height)
    assert got == md.EXCLUSION_UNKNOWN and why


# == What is sent =====================================================================================================
X, W = md.EXCLUDED_MISSED, md.EXCLUDED_WINDOW


@pytest.mark.parametrize("args,want", [
    ((A, True, False, True, D, X, True), "true"),          # excluded by the cap, the judge can answer: back
    ((A, True, False, True, D, W, True), "true"),          # excluded by the window
    ((A, True, False, True, D, X, False), None),           # its engine is not ready
    ((A, True, False, True, D, X, None), None),            # whether it can answer is not known
    ((A, False, False, True, D, X, True), None),           # role off
    ((A, True, True, True, D, X, True), None),             # draining
    ((A, True, False, True, D, md.EXCLUSION_NONE, True), None),
    ((A, True, False, True, D, md.EXCLUSION_UNKNOWN, True), None),
    ((N, True, False, True, D, X, True), None),            # the chain before epoch 15
    ((None, True, False, True, D, X, True), None),
    ((A, True, False, False, D, X, True), None),           # not recorded with this key as operator
    ((A, True, False, True, ND, X, True), None),           # nothing declared: judge_action's case
])
def test_the_return_table(args, want):
    assert md.judge_return_action(*args) == want


def test_a_held_judge_is_not_sent_back():
    assert md.judge_return_action(A, True, False, True, D, X, True, held=True) is None


def test_true_only_when_every_condition_holds():
    for acting in (A, N, None, md.EPOCH_UNKNOWN):
        for role in (True, False):
            for draining in (True, False):
                for registered in (True, False):
                    for read in (D, ND, md.JUDGE_READ_UNKNOWN):
                        for word in (X, W, md.EXCLUSION_NONE, md.EXCLUSION_UNKNOWN):
                            for ready in (True, False, None, 1, "yes"):
                                got = md.judge_return_action(acting, role, draining, registered, read, word, ready)
                                expect = ("true" if (acting == A and role and not draining and registered
                                                     and read == D and word in (X, W) and ready is True) else None)
                                assert got == expect, (acting, role, draining, registered, read, word, ready)


# == The readiness ====================================================================================================
class Engine:
    def __init__(self, ok=True):
        self.ok, self.asked = ok, []

    def __call__(self, endpoint, model, wait_s, step_s=5.0, sleep=None, clock=None):
        self.asked.append((endpoint, model, wait_s))
        return (True, "") if self.ok else (False, f"the engine does not hold {model!r}, within 0 s")


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr(md, "judge_engine_ready", e)
    monkeypatch.setattr(md.memwatch, "judge_model_named", lambda env=None: "judge-model:q4")
    monkeypatch.setattr(md, "_judge_worker_model", lambda: ("judge-model:q4", "env", None))
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "http://ollama-cpu:11434/")
    return e


def test_ready_when_the_engine_lists_the_model_and_the_guard_is_open(engine):
    ok, why = md.judge_answer_ready(lambda: (True, "the judge model is loaded"))
    assert ok is True and "judge-model:q4" in why and "the judge model is loaded" in why
    assert engine.asked == [("http://ollama-cpu:11434", "judge-model:q4", 0.0)], "one reading, nothing waited for"


@pytest.mark.parametrize("worker", [
    lambda: (None, None, "no judge state at /tmp/dendra-judge-state.json"),      # no worker ever wrote one
    lambda: (None, None, "the judge state names process 77, which does not run: it was written by an earlier worker"),
    lambda: ("", "env", None),
])
def test_no_running_judge_worker_is_not_ready_and_the_engine_is_not_asked(engine, worker):
    gates = []
    ok, why = md.judge_answer_ready(lambda: gates.append(1) or (True, "room"), worker=worker)
    assert ok is None and "no running judge worker" in why and engine.asked == [] and gates == []


def test_the_worker_reading_is_the_default_and_a_worker_reading_that_raises_is_not_read(engine, monkeypatch):
    monkeypatch.setattr(md, "_judge_worker_model", lambda: (None, None, "no judge state here"))
    ok, why = md.judge_answer_ready(lambda: (True, "room"))
    assert ok is None and "no judge state here" in why

    def boom():
        raise OSError("x")
    monkeypatch.setattr(md, "_judge_worker_model", boom)
    ok, why = md.judge_answer_ready(lambda: (True, "room"))
    assert ok is None and "OSError" in why and engine.asked == []


def test_the_worker_is_read_through_the_self_test_on_this_container_s_proc(monkeypatch):
    import miner_selftest
    asked = []
    monkeypatch.setattr(miner_selftest, "judge_worker_model", lambda root: asked.append(root) or ("m", "env", None))
    assert md._judge_worker_model() == ("m", "env", None) and asked == ["/proc"]


@pytest.mark.skipif(not os.path.isfile("/proc/self/stat"), reason="reads a Linux /proc")
def test_a_living_judge_worker_is_read_and_a_dead_one_is_not(engine, monkeypatch, tmp_path):
    # A real process named judge_worker.py and the judge state it would write: the shipped reader ties the state to
    # it; once it is gone, the same state names a process that does not run.
    import subprocess
    import time
    from modea import heartbeat as hb
    script = tmp_path / "judge_worker.py"
    script.write_text("import time" + NL + "time.sleep(120)" + NL)
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        with open(f"/proc/{proc.pid}/stat", encoding="utf-8") as f:
            start = hb.proc_starttime(f.read())
        state = tmp_path / "judge-state.json"
        state.write_text(json.dumps({"schema": 1, "pid": proc.pid, "starttime": start, "model": "judge-model:q4",
                                     "source": "env", "written_at": int(time.time())}))
        monkeypatch.setenv("DENDRA_JUDGE_STATE_FILE", str(state))
        got = _REAL_WORKER()
        assert got == ("judge-model:q4", "env", None), got
        assert md.judge_answer_ready(lambda: (True, "room"), worker=_REAL_WORKER)[0] is True
    finally:
        proc.kill()
        proc.wait()
    got = _REAL_WORKER()
    assert got[0] is None and "does not run" in got[2], got
    assert md.judge_answer_ready(lambda: (True, "room"), worker=_REAL_WORKER)[0] is None


def test_not_ready_when_the_engine_does_not_list_the_model(engine):
    engine.ok = False
    gates = []
    ok, why = md.judge_answer_ready(lambda: gates.append(1) or (True, "room"))
    assert ok is False and "does not hold" in why and gates == []


def test_not_ready_when_the_guard_holds_the_model_back(engine):
    ok, why = md.judge_answer_ready(lambda: (False, "9952 MiB free, the model needs 19456 MiB"))
    assert ok is False and "19456 MiB" in why


def test_without_a_guard_nothing_is_asked_and_nothing_is_ready(engine):
    ok, why = md.judge_answer_ready(None)
    assert ok is None and why and engine.asked == []


@pytest.mark.parametrize("named", [None, "other-model:q8"])
def test_a_guard_about_another_model_than_the_worker_s_is_not_read(engine, monkeypatch, named):
    # The guard checks the model memwatch names; the worker judges with its own. Two names: the guard's answer is not
    # about the model a seat would be answered with.
    monkeypatch.setattr(md.memwatch, "judge_model_named", lambda env=None: named)
    gates = []
    ok, why = md.judge_answer_ready(lambda: gates.append(1) or (True, "room"))
    assert ok is None and "is not about the worker's model" in why and engine.asked == [] and gates == []


def test_a_guard_that_raises_is_not_read(engine):
    def boom():
        raise RuntimeError("x")
    ok, why = md.judge_answer_ready(boom)
    assert ok is None and "RuntimeError" in why


def test_the_process_guard_is_the_default(engine, monkeypatch):
    monkeypatch.setattr(md.memwatch, "_LOCAL", None)
    assert md._local_ready()[0] is None
    watch = type("W", (), {"load_gate": lambda self: (True, "room")})()
    monkeypatch.setattr(md.memwatch, "_LOCAL", watch)
    assert md._local_ready()[0] is True


# == The seat, turn by turn ===========================================================================================
class Chain:
    """The declaration's reading, the chain's limits and height, the judge's readiness and the transaction."""

    def __init__(self, fields, height=50, limits=None, ready=(True, "ready"), land=True):
        self.fields, self.height_at, self.limits_doc = dict(fields), height, LIMITS if limits is None else limits
        self.ready_ans, self.land = ready, land
        self.reads = self.limit_reads = self.height_reads = self.ready_reads = 0
        self.sent = []

    def reader(self, mid):
        self.reads += 1
        if self.fields.get("declared") is None:
            return md.JUDGE_READ_UNKNOWN, {}, "scripted"
        return (D if self.fields["declared"] else ND), dict(self.fields), ""

    def limits(self):
        self.limit_reads += 1
        return (dict(self.limits_doc), "") if self.limits_doc else (None, "`dendrad query jobs params` failed (refused)")

    def height(self):
        self.height_reads += 1
        return self.height_at

    def ready(self):
        self.ready_reads += 1
        return self.ready_ans

    def send(self, mid, value):
        self.sent.append(value)
        if self.land and value == "true":
            self.fields.update(declared=True, declared_height=42, missed_draws=0)
        return OK_BCAST if self.land else ""


def _seat(monkeypatch, chain, **kw):
    monkeypatch.setattr(md, "run", lambda c, t=None, stdin="": INCLUDED)
    r = Runner(**{"help:declare-judge": (0, HELP_E15_DECLARE, "")})
    args = dict(reader=chain.reader, send=chain.send, limits=chain.limits, height=chain.height, ready=chain.ready)
    args.update(kw)
    return md.JudgeSeat("dm1x", md.KitCli(r), **args)


def _turns(s, n, role=True, draining=False, acting=A, registered=True, start=0):
    return [s.turn(acting, role, draining, registered, now=(start + k) * md.JUDGE_TURN_S) for k in range(n)]


def test_a_judge_excluded_by_the_cap_is_declared_again_once(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP))
    s = _seat(monkeypatch, c)
    assert _turns(s, 5) == ["true", None, None, None, None]
    assert c.sent == ["true"] and c.ready_reads == 1
    out = capsys.readouterr().out
    assert out.count("the chain no longer draws this judge") == 1 and "at or above judge_missed_draws_max 3" in out
    assert out.count("judge declared on chain at height 42") == 1
    jd = md._STATUS["judge_declaration"]
    assert jd["state"] == D and jd["exclusion"]["state"] == md.EXCLUSION_NONE


def test_a_judge_excluded_by_the_window_is_declared_again_past_the_edge_only(monkeypatch):
    c = Chain(F(declared_height=1000), height=1000 + WINDOW)
    s = _seat(monkeypatch, c)
    assert _turns(s, 2) == [None, None] and c.ready_reads == 0, "at the edge it is still a candidate"
    c.height_at += 1
    assert _turns(s, 1, start=2) == ["true"]
    assert c.sent == ["true"]


def test_a_judge_whose_engine_is_not_ready_is_not_declared_again_and_it_is_said_once(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP), ready=(False, "the judge engine http://ollama-cpu:11434 is not ready (absent)"))
    s = _seat(monkeypatch, c)
    assert _turns(s, 4) == [None] * 4 and c.sent == [] and c.ready_reads == 4
    out = capsys.readouterr().out
    assert out.count("it is not declared again") == 1, out
    jd = md._STATUS["judge_declaration"]
    assert jd["state"] == D and jd["sent"] is None
    assert jd["exclusion"]["state"] == md.EXCLUDED_MISSED and jd["exclusion"]["judge"] == "not_ready"
    assert "not declared again" in jd["why"] and "is not ready" in jd["why"]
    c.ready_ans = (True, "ready")                         # the engine is fixed: the next turn declares it again
    assert _turns(s, 1, start=4) == ["true"]


def test_a_readiness_not_read_sends_nothing(monkeypatch):
    c = Chain(F(missed_draws=CAP), ready=(None, "no memory guard runs in this process"))
    s = _seat(monkeypatch, c)
    assert _turns(s, 3) == [None] * 3 and c.sent == []
    assert md._STATUS["judge_declaration"]["exclusion"]["judge"] == "unknown"


def test_a_readiness_that_raises_or_answers_no_boolean_sends_nothing(monkeypatch):
    def boom():
        raise RuntimeError("x")
    for ready in (boom, lambda: (1, "one"), lambda: ("yes", "")):
        c = Chain(F(missed_draws=CAP))
        s = _seat(monkeypatch, c, ready=ready)
        assert _turns(s, 2) == [None, None] and c.sent == []
        assert md._STATUS["judge_declaration"]["exclusion"]["judge"] == "unknown", "1 is not True"


def test_each_outcome_is_said_once_and_again_when_it_changes(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP), limits={}, ready=(False, "the engine does not hold it"))
    s = _seat(monkeypatch, c)
    _turns(s, 2)                                           # the limits are not read
    c.limits_doc = LIMITS
    c.height_at = None
    _turns(s, 2, start=2)                                  # the height is not read: another cause, said
    c.height_at = 50
    _turns(s, 2, start=4)                                  # excluded, and its engine is not ready
    c.fields["missed_draws"] = 0
    _turns(s, 1, start=6)                                  # a candidate again
    c.fields["missed_draws"] = CAP
    _turns(s, 2, start=7)                                  # excluded again: said again
    out = capsys.readouterr().out
    assert out.count("the chain's limits for a judge were not read") == 1
    assert out.count("the node's height was not read") == 1
    assert out.count("it is not declared again") == 2 and c.sent == []


def test_a_judge_first_declared_then_excluded_is_sent_back(monkeypatch):
    # The first `true` (judge_action, nothing declared) is not a return: the first exclusion after it is sent back.
    c = Chain({"declared": False, "declared_height": 0, "last_verdict_height": 0, "missed_draws": 0})
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"] and s.returned is None
    c.fields["missed_draws"] = CAP
    assert _turns(s, 1, start=1) == ["true"] and c.sent == ["true", "true"]


def test_an_undeclared_judge_with_its_role_off_reads_no_limit(monkeypatch):
    c = Chain({"declared": False, "declared_height": 0, "last_verdict_height": 0, "missed_draws": CAP})
    s = _seat(monkeypatch, c)
    assert _turns(s, 2, role=False) == [None, None]
    assert c.sent == [] and c.limit_reads == c.ready_reads == 0


@pytest.mark.parametrize("role,draining,want", [(False, False, ["false"]), (True, True, ["false"])])
def test_role_off_or_draining_withdraws_and_reads_no_limit(monkeypatch, role, draining, want):
    c = Chain(F(missed_draws=CAP))
    s = _seat(monkeypatch, c)
    _turns(s, 1, role=role, draining=draining)
    assert c.sent == want and c.limit_reads == c.height_reads == c.ready_reads == 0


def test_limits_not_read_send_nothing_and_say_it_once(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP), limits={})
    s = _seat(monkeypatch, c)
    assert _turns(s, 4) == [None] * 4 and c.sent == []
    assert c.height_reads == 0 and c.ready_reads == 0, "nothing is read past the step that failed"
    out = capsys.readouterr().out
    assert out.count("whether the chain still draws this judge is not known") == 1
    jd = md._STATUS["judge_declaration"]
    assert jd["exclusion"]["state"] == md.EXCLUSION_UNKNOWN and "refused" in jd["why"]


def test_a_height_not_read_sends_nothing(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP), height=None)
    s = _seat(monkeypatch, c)
    assert _turns(s, 3) == [None] * 3 and c.sent == [] and c.ready_reads == 0
    assert capsys.readouterr().out.count("the node's height was not read") == 1


def test_readers_that_raise_send_nothing(monkeypatch):
    def boom():
        raise RuntimeError("x")
    c = Chain(F(missed_draws=CAP))
    s = _seat(monkeypatch, c, limits=boom)
    assert _turns(s, 2) == [None, None] and c.sent == [] and c.height_reads == 0
    assert "raised RuntimeError" in md._STATUS["judge_declaration"]["why"]
    c2 = Chain(F(missed_draws=CAP))
    s2 = _seat(monkeypatch, c2, height=boom)
    assert _turns(s2, 2) == [None, None] and c2.sent == [] and c2.ready_reads == 0


def test_a_cli_without_a_runner_reads_through_run_rc(monkeypatch):
    # miner_selftest.py's bench hands the seat a cli with no runner: the limits are then read through run_rc.
    asked = []
    monkeypatch.setattr(md, "run_rc", lambda c, t=None: asked.append([str(x) for x in c]) or (1, "", "refused"))
    cli = type("Cli", (), {"has": lambda self, *a, **k: True, "predates": lambda self, *a, **k: None})()
    c = Chain(F(missed_draws=CAP))
    s = md.JudgeSeat("dm1x", cli, reader=c.reader, send=c.send, ready=c.ready)
    assert s.turn(A, True, False, True, now=0.0) is None and c.sent == []
    assert [x[:4] for x in asked] == [["dendrad", "query", "jobs", "params"]]


def test_fields_not_known_send_nothing(monkeypatch):
    c = Chain({"declared": True, "declared_height": 1000, "last_verdict_height": 0, "missed_draws": None})
    s = _seat(monkeypatch, c)
    assert _turns(s, 2) == [None, None] and c.sent == [] and c.ready_reads == 0
    c2 = Chain({"declared": None})                         # a reading that is not known: judge_action's case
    s2 = _seat(monkeypatch, c2)
    assert _turns(s2, 2) == [None, None] and c2.sent == [] and c2.limit_reads == 0


def test_a_judge_still_a_candidate_reads_no_readiness(monkeypatch):
    c = Chain(F(missed_draws=CAP - 1, last_verdict_height=990))
    s = _seat(monkeypatch, c)
    assert _turns(s, 3) == [None] * 3 and c.sent == [] and c.ready_reads == 0 and c.limit_reads == 3


@pytest.mark.parametrize("acting,registered", [(N, True), (None, True), (md.EPOCH_UNKNOWN, True), (A, False)])
def test_before_epoch_15_or_unregistered_nothing_is_read(monkeypatch, acting, registered):
    c = Chain(F(missed_draws=CAP))
    s = _seat(monkeypatch, c)
    assert _turns(s, 3, acting=acting, registered=registered) == [None] * 3
    assert c.reads == c.limit_reads == c.ready_reads == 0 and c.sent == []


def test_a_return_whose_fate_was_not_read_is_not_sent_again_before_the_next_reading(monkeypatch):
    c = Chain(F(missed_draws=CAP), land=False)
    s = _seat(monkeypatch, c)
    assert s.turn(A, True, False, True, now=0.0) is None
    s.turn(A, True, False, True, now=md.JUDGE_TURN_S - 1)
    assert c.sent == ["true"] and c.reads == 1
    s.turn(A, True, False, True, now=md.JUDGE_TURN_S)      # still excluded at the next reading: sent once more
    assert c.sent == ["true", "true"]
    assert s.returned is None, "a return that did not land (the same declaration read after it) is not recorded"


def _fate_unread(monkeypatch):
    """Every transaction lands on chain (Chain.send), and its fate is never read back."""
    monkeypatch.setattr(md, "wait_tx_height", lambda o, timeout=24: 0)


def test_a_return_that_landed_unseen_is_recorded_from_the_next_reading_and_held(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    _fate_unread(monkeypatch)
    assert _turns(s, 1) == [None] and c.sent == ["true"] and s.returned is None
    assert _turns(s, 1, start=1) == [None], "the new declaration is read: a candidate"
    assert s.returned == {"height": 42, "last_verdict_height": 7} and s.pending is None
    assert capsys.readouterr().out.count("whose fate was not read landed") == 1
    c.fields["missed_draws"] = CAP                          # seated again, and silent again
    assert _turns(s, 2, start=2) == [None, None] and c.sent == ["true"]
    assert md._STATUS["judge_declaration"]["exclusion"]["judge"] == "held"


def test_a_return_that_landed_unseen_and_was_answered_since_is_a_new_episode(monkeypatch):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    _fate_unread(monkeypatch)
    _turns(s, 1)
    c.fields["last_verdict_height"] = 45                    # it answered after the return, before the next reading
    _turns(s, 1, start=1)
    assert s.returned == {"height": 42, "last_verdict_height": 7}, "the last verdict is the one read before the send"
    c.fields["missed_draws"] = CAP
    assert _turns(s, 1, start=2) == [None] and c.sent == ["true", "true"]


def test_a_first_declaration_that_landed_unseen_is_not_a_return(monkeypatch):
    c = Chain({"declared": False, "declared_height": 0, "last_verdict_height": 0, "missed_draws": 0})
    s = _seat(monkeypatch, c)
    _fate_unread(monkeypatch)
    assert _turns(s, 2) == [None, None] and c.sent == ["true"] and s.pending is None and s.returned is None
    c.fields["missed_draws"] = CAP
    _turns(s, 1, start=2)
    assert c.sent == ["true", "true"], "the first exclusion after judge_action's declaration is sent back"


def test_a_return_whose_fate_was_not_read_and_a_withdrawal_since_is_dropped(monkeypatch):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7), land=False)
    s = _seat(monkeypatch, c)
    _turns(s, 1)
    assert s.pending == {"declared_height": 40, "last_verdict_height": 7}
    c.fields.update(declared=False, declared_height=99)     # the operator withdrew it meanwhile
    _turns(s, 1, start=1, role=False)
    assert s.pending is None and s.returned is None


def test_a_reading_that_is_not_known_keeps_the_record_of_a_return_whose_fate_was_not_read(monkeypatch):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    _fate_unread(monkeypatch)
    _turns(s, 1)
    keep = dict(c.fields)
    c.fields["declared"] = None                            # the next reading fails
    _turns(s, 1, start=1)
    assert s.pending is not None and s.returned is None
    c.fields = keep
    _turns(s, 1, start=2)
    assert s.returned == {"height": 42, "last_verdict_height": 7}


def test_a_judge_sent_back_that_the_count_excludes_again_without_an_answer_is_held(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"] and s.returned == {"height": 42, "last_verdict_height": 7}
    c.fields["missed_draws"] = CAP                          # seated again, and silent again
    assert _turns(s, 3, start=1) == [None] * 3 and c.sent == ["true"]
    assert c.ready_reads == 1, "a held judge asks nothing of its engine"
    out = capsys.readouterr().out
    assert out.count("this process does not declare it again") == 1
    jd = md._STATUS["judge_declaration"]
    assert jd["exclusion"]["judge"] == "held" and "not declared again by this process" in jd["why"]
    c.fields["last_verdict_height"] = 50                    # it answered once since: a new exclusion is a new episode
    assert _turns(s, 1, start=4) == ["true"] and c.sent == ["true", "true"]


def test_a_judge_sent_back_read_as_a_candidate_on_the_way_to_the_cap_is_still_held(monkeypatch):
    # On chain the misses after a return climb one by one, so turns read a candidate before the cap comes back.
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"]
    for k, missed in enumerate(range(1, CAP)):
        c.fields["missed_draws"] = missed
        assert _turns(s, 1, start=1 + k) == [None]
        assert md._STATUS["judge_declaration"]["exclusion"]["state"] == md.EXCLUSION_NONE
    c.fields["missed_draws"] = CAP
    assert _turns(s, 1, start=CAP) == [None] and c.sent == ["true"]
    assert md._STATUS["judge_declaration"]["exclusion"]["judge"] == "held"


def test_a_reading_that_failed_between_the_return_and_the_cap_keeps_the_hold(monkeypatch):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"]
    c.fields["missed_draws"] = CAP
    c.limits_doc = {}                                       # the params are not read on this turn
    assert _turns(s, 1, start=1) == [None]
    assert md._STATUS["judge_declaration"]["exclusion"]["state"] == md.EXCLUSION_UNKNOWN
    c.limits_doc = LIMITS
    assert _turns(s, 1, start=2) == [None] and c.sent == ["true"]
    assert md._STATUS["judge_declaration"]["exclusion"]["judge"] == "held"


@pytest.mark.parametrize("missed", [1, CAP - 1])
def test_a_judge_sent_back_then_seated_without_answering_until_the_window_excludes_it_is_held(monkeypatch, capsys,
                                                                                              missed):
    # A mute judge drawn fewer times than the cap within a window: the window excludes it with its misses below the
    # cap. Sending it back would clear those misses at every window, and the cap would never exclude it.
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"]
    c.fields["missed_draws"] = missed
    c.height_at = 42 + WINDOW                               # the window's edge: still a candidate
    assert _turns(s, 1, start=1) == [None]
    assert md._STATUS["judge_declaration"]["exclusion"]["state"] == md.EXCLUSION_NONE
    c.height_at = 42 + WINDOW + 1
    assert _turns(s, 2, start=2) == [None, None] and c.sent == ["true"]
    jd = md._STATUS["judge_declaration"]
    assert jd["exclusion"]["state"] == md.EXCLUDED_WINDOW and jd["exclusion"]["judge"] == "held"
    assert f"seated since on {missed} concluded audit(s) without an answer" in jd["why"]
    assert capsys.readouterr().out.count("this process does not declare it again") == 1
    c.fields["last_verdict_height"] = 42 + WINDOW + 1       # an answer since: a new exclusion is a new episode
    c.height_at = 42 + 2 * WINDOW + 2
    assert _turns(s, 1, start=4) == ["true"]


def test_a_judge_sent_back_and_then_left_undrawn_by_the_window_is_sent_back_again(monkeypatch):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"]
    c.height_at = 42 + WINDOW + 1                           # a quiet network: no draw, no missed draw
    assert _turns(s, 1, start=1) == ["true"]


def test_a_declaration_made_elsewhere_is_not_held(monkeypatch, capsys):
    c = Chain(F(missed_draws=CAP, last_verdict_height=7))
    s = _seat(monkeypatch, c)
    assert _turns(s, 1) == ["true"]
    c.fields.update(declared_height=60, missed_draws=CAP)  # another declaration than this process's, then three misses
    assert _turns(s, 1, start=1) == ["true"]
    assert capsys.readouterr().out.count("its judge can answer now") == 2, "each return is logged"


def test_the_default_limits_and_height_go_through_the_kit_s_dendrad(monkeypatch):
    # No reader injected for the limits and the height: the limits through the KitCli runner, the height through
    # `dendrad status` (miner.run); neither answers here, and nothing is sent.
    c = Chain(F(missed_draws=CAP))
    s = _seat(monkeypatch, c, limits=None, height=None)
    assert _turns(s, 2) == [None, None] and c.sent == []
    assert any(cmd[:4] == ["dendrad", "query", "jobs", "params"] for cmd in s.cli.runner.calls)
    params_ok = (0, json.dumps({"params": {"judge_missed_draws_max": "3"}}), "")
    s2 = _seat(monkeypatch, Chain(F(missed_draws=CAP)), limits=None, height=None)
    s2.cli.runner.answers["params"] = params_ok
    monkeypatch.setattr(md, "run", lambda c, t=None, stdin="": (json.dumps(
        {"sync_info": {"latest_block_height": "1001"}}) if list(c[:2]) == ["dendrad", "status"] else INCLUDED))
    assert s2.turn(A, True, False, True, now=0.0) == "true"
