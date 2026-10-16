# -*- coding: utf-8 -*-
"""The daemon's reading of the chain's epoch and of its own dendrad, the gate on the proof's kit_version, and the judge
declaration: miner.read_chain_epoch, EpochProbe, cli_offers, KitCli, proof_kit_flags, judge_role_on,
read_judge_declaration, judge_action and JudgeSeat.

What is pinned, each with its counterpart:
  * The epoch in three answers: a plan height is applied, `{}` is not applied (the upgrade keeper answers height 0
    without an error), anything else is unknown; with no plan height, the jobs module at version 2 is applied (a chain
    started or restored on the epoch 15 binary records no plan height), at version 1 not applied, unread unknown.
  * The probe reads on its period, logs a change once, and acts on its last known answer through an unknown one;
    before any answer it acts on nothing.
  * A verb or a flag of this dendrad is read from its help: the parent's usage line means absent (the real call
    fails on "unknown flag: --from", never "unknown command"), a flag counts on its own flags line only.
  * kit_version only on epoch 15, with a known kit number and a dendrad that has the flag.
  * The declaration: read before anything is sent, `true` only when the chain holds none, `false` only when it holds
    one, nothing on an unknown or unsupported reading, nothing off epoch 15, and a declaration whose fate was not read
    is not sent again before the next reading.

The shapes are the ones measured on throwaway chains with the epoch 14 binary, the epoch 15 binary after a governed
upgrade from it, and the epoch 15 binary from a fresh genesis. The subject can be a mutated copy (DENDRA_DAEMON_FILE).
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

NL = chr(10)
TXH = "AB" * 32
OK_BCAST = "code: 0" + NL + "txhash: " + TXH + NL
INCLUDED = NL.join(["code: 0", 'height: "42"', "txhash: " + TXH])
REFUSED_CONN = 'post failed: Post "http://127.0.0.1:1": dial tcp 127.0.0.1:1: connect: connection refused'
APPLIED_38 = json.dumps({"height": "38"}, indent=2)
VERSIONS = lambda v: json.dumps({"module_versions": [{"name": "jobs", "version": str(v)}]}, indent=2)  # noqa: E731

# The help texts the two binaries print (first lines, then the flags the cases read).
HELP_E14_DECLARE = NL.join(["Transactions commands for the jobs module", "", "Usage:", "  dendrad tx jobs [flags]",
                            "  dendrad tx jobs [command]", "", "Available Commands:"])
HELP_E15_DECLARE = NL.join(["Enter (true) or leave (false) the jury pool for a miner, signed by its operator (ADR-059)",
                            "", "Usage:", "  dendrad tx jobs declare-judge [miner_id] [judge] [flags]", "", "Flags:",
                            "  -a, --account-number uint     The account number of the signing account"])
HELP_E14_PROVE = NL.join(["Prove availability by answering the challenge of the current epoch", "", "Usage:",
                          "  dendrad tx jobs prove-availability [miner-id] [challenge] [vrf_proof] [flags]", "",
                          "Flags:", "      --gas string                  gas limit to set per-transaction"])
HELP_E15_PROVE = NL.join(["Prove availability by answering the challenge of the current epoch (--kit-version: the kit "
                          "number the operator runs)", "", "Usage:",
                          "  dendrad tx jobs prove-availability [miner-id] [challenge] [vrf_proof] [flags]", "",
                          "Flags:", "      --gas string                  gas limit to set per-transaction",
                          "      --kit-version uint            "])
HELP_SHORT_ONLY = HELP_E14_PROVE.replace("current epoch", "current epoch (--kit-version: the kit number)", 1)


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

    def asked(self, key):
        return [c for c in self.calls if ("help:" + c[-2] if c[-1] == "--help" else c[3] if len(c) > 3 else "") == key]


# == The epoch, in three answers ===================================================================================
@pytest.mark.parametrize("ans,state,height", [
    ((0, APPLIED_38, ""), md.EPOCH_APPLIED, 38),
    ((0, "{}" + NL, ""), md.EPOCH_NOT_APPLIED, None),
    ((0, json.dumps({"height": "0"}), ""), md.EPOCH_NOT_APPLIED, None),
    ((0, json.dumps({"height": 38}), ""), md.EPOCH_APPLIED, 38),
    ((1, "", REFUSED_CONN), md.EPOCH_UNKNOWN, None),
    ((None, "", "TimeoutExpired: dendrad"), md.EPOCH_UNKNOWN, None),
    ((0, 'height: "38"', ""), md.EPOCH_UNKNOWN, None),
    ((0, "", ""), md.EPOCH_UNKNOWN, None),
    ((0, "[]", ""), md.EPOCH_UNKNOWN, None),
    ((0, json.dumps({"height": "38", "name": "x"}), ""), md.EPOCH_UNKNOWN, None),
    ((0, json.dumps({"height": "-3"}), ""), md.EPOCH_UNKNOWN, None),
    ((0, json.dumps({"height": True}), ""), md.EPOCH_UNKNOWN, None),
])
def test_the_plan_reads_in_three_answers(ans, state, height):
    r = Runner(applied=ans)
    st, h, why = md.read_plan_applied(r)
    assert (st, h) == (state, height)
    assert r.calls[0][:4] == ["dendrad", "query", "upgrade", "applied"] and r.calls[0][-1] == md.EPOCH15_PLAN
    assert (why == "") == (state != md.EPOCH_UNKNOWN)


@pytest.mark.parametrize("ans,version", [
    ((0, VERSIONS(1), ""), 1),
    ((0, VERSIONS(2), ""), 2),
    ((0, json.dumps({"module_versions": [{"name": "jobs"}]}), ""), 0),
    ((0, json.dumps({"module_versions": [{"name": "bank", "version": "4"}]}), ""), None),
    ((0, "{}", ""), None),
    ((1, "", REFUSED_CONN), None),
    ((0, json.dumps({"module_versions": [{"name": "jobs", "version": "two"}]}), ""), None),
])
def test_the_jobs_module_version_is_read_or_none(ans, version):
    v, why = md.read_jobs_version(Runner(**{"module-versions": ans}))
    assert v == version
    assert (why == "") == (version is not None)


@pytest.mark.parametrize("plan,versions,state,asked_versions", [
    ((0, APPLIED_38, ""), (0, VERSIONS(2), ""), md.EPOCH_APPLIED, False),
    ((0, "{}", ""), (0, VERSIONS(2), ""), md.EPOCH_APPLIED, True),
    ((0, "{}", ""), (0, VERSIONS(1), ""), md.EPOCH_NOT_APPLIED, True),
    ((0, "{}", ""), (1, "", REFUSED_CONN), md.EPOCH_UNKNOWN, True),
    ((1, "", REFUSED_CONN), (0, VERSIONS(2), ""), md.EPOCH_UNKNOWN, False),
])
def test_the_epoch_is_the_plan_then_the_module_version(plan, versions, state, asked_versions):
    r = Runner(applied=plan, **{"module-versions": versions})
    st, why, detail = md.read_chain_epoch(r)
    assert st == state and why
    assert bool(r.asked("module-versions")) == asked_versions


# == The probe: a period, a log on change, the last known answer ======================================================
def test_the_probe_reads_on_its_period_and_acts_on_its_last_known_answer(capsys):
    script = [md.EPOCH_APPLIED, md.EPOCH_UNKNOWN, md.EPOCH_UNKNOWN, md.EPOCH_NOT_APPLIED]
    reads = []

    def reader():
        reads.append(1)
        return script[len(reads) - 1], "why %d" % len(reads), {}
    p = md.EpochProbe(reader)
    assert p.acting is None and p.tick(now=0.0) is True and p.applied()
    assert p.tick(now=md.EPOCH_PROBE_S - 1) is False and len(reads) == 1, "not before its period"
    assert p.tick(now=md.EPOCH_PROBE_S) is False and len(reads) == 2
    assert (p.state, p.acting) == (md.EPOCH_UNKNOWN, md.EPOCH_APPLIED), "an unknown keeps the last answer"
    assert p.tick(now=md.EPOCH_PROBE_S + md.EPOCH_RETRY_S - 1) is False and len(reads) == 2
    assert p.tick(now=md.EPOCH_PROBE_S + md.EPOCH_RETRY_S) is False and len(reads) == 3
    assert p.tick(now=md.EPOCH_PROBE_S + 2 * md.EPOCH_RETRY_S) is True and p.acting == md.EPOCH_NOT_APPLIED
    out = capsys.readouterr().out
    assert out.count("consensus epoch") == 3, "the second unknown is not said again"
    assert md._STATUS["chain_epoch"]["acting"] == md.EPOCH_NOT_APPLIED


def test_before_any_answer_the_probe_acts_on_nothing(capsys):
    p = md.EpochProbe(lambda: (md.EPOCH_UNKNOWN, "connection refused", {}))
    p.tick(now=0.0)
    assert p.acting is None and not p.applied()
    assert md.proof_kit_flags(p.acting, 7, True) == ()
    assert "nothing only epoch 15 decodes is sent" in capsys.readouterr().out


def test_a_reader_that_raises_or_answers_no_state_is_unknown():
    def boom():
        raise RuntimeError("x")
    p = md.EpochProbe(boom)
    p.tick(now=0.0)
    assert p.state == md.EPOCH_UNKNOWN and p.acting is None
    p = md.EpochProbe(lambda: ("maybe", "", {}))
    p.tick(now=0.0)
    assert p.state == md.EPOCH_UNKNOWN and p.acting is None


# == What this dendrad offers =========================================================================================
@pytest.mark.parametrize("rc,text,verb,flag,want", [
    (0, HELP_E14_DECLARE, "declare-judge", None, False),
    (0, HELP_E15_DECLARE, "declare-judge", None, True),
    (0, HELP_E14_PROVE, "prove-availability", "--kit-version", False),
    (0, HELP_E15_PROVE, "prove-availability", "--kit-version", True),
    (0, HELP_SHORT_ONLY, "prove-availability", "--kit-version", False),
    (0, HELP_E15_PROVE, "prove-availability", None, True),
    (1, HELP_E15_DECLARE, "declare-judge", None, None),
    (0, "", "declare-judge", None, None),
    (0, NL.join(["Usage:", "  something else"]), "declare-judge", None, None),
    (0, NL.join(["Usage:", "  dendrad tx jobs declare-judgement [flags]"]), "declare-judge", None, False),
])
def test_a_verb_and_a_flag_are_read_from_the_help(rc, text, verb, flag, want):
    assert md.cli_offers(rc, text, "", ("tx", "jobs"), verb, flag) is want


def test_each_help_is_read_once_and_an_unread_one_is_asked_again(capsys):
    answers = iter([(1, "", "exec: dendrad not found"), (0, HELP_E14_DECLARE, "")])
    r = Runner(**{"help:declare-judge": lambda: next(answers)})
    cli = md.KitCli(r)
    assert cli.has(("tx", "jobs"), "declare-judge") is None
    assert cli.has(("tx", "jobs"), "declare-judge") is False
    assert cli.has(("tx", "jobs"), "declare-judge") is False
    assert len(r.asked("help:declare-judge")) == 2
    cli.predates("dm1x", "`tx jobs declare-judge`")
    cli.predates("dm1x", "`tx jobs declare-judge`")
    out = capsys.readouterr().out
    assert out.count("the kit's dendrad predates the plan") == 1 and "Update the kit" in out


# == The field gate ===================================================================================================
@pytest.mark.parametrize("acting,kit,cli,want", [
    (md.EPOCH_APPLIED, 7, True, ("--kit-version", "7")),
    (md.EPOCH_NOT_APPLIED, 7, True, ()),
    (None, 7, True, ()),
    (md.EPOCH_UNKNOWN, 7, True, ()),
    (md.EPOCH_APPLIED, None, True, ()),
    (md.EPOCH_APPLIED, 0, True, ()),
    (md.EPOCH_APPLIED, True, True, ()),
    (md.EPOCH_APPLIED, 7, False, ()),
    (md.EPOCH_APPLIED, 7, None, ()),
])
def test_kit_version_goes_only_to_an_epoch_15_chain_through_a_dendrad_that_has_the_flag(acting, kit, cli, want):
    assert md.proof_kit_flags(acting, kit, cli) == want


# == The judge role ===================================================================================================
@pytest.mark.parametrize("env,on", [({"DENDRA_MINER_JUDGE": "1"}, True), ({"DENDRA_MINER_JUDGE": "0"}, False),
                                    ({"DENDRA_MINER_JUDGE": ""}, False), ({"DENDRA_MINER_JUDGE": " 1"}, False),
                                    ({"DENDRA_MINER_JUDGE": "true"}, False), ({}, False)])
def test_the_judge_role_is_the_value_the_entrypoint_starts_the_worker_on(env, on):
    assert md.judge_role_on(env) is on


# == The declaration's reading ========================================================================================
def _read(answer, help_=(0, HELP_E15_DECLARE.replace("tx jobs declare-judge [miner_id] [judge]",
                                                      "query jobs miner-judge [miner_id]"), "")):
    r = Runner(**{"help:miner-judge": help_, "miner-judge": answer})
    return md.read_judge_declaration("dm1x", cli=md.KitCli(r), runner=r), r


def test_an_empty_answer_is_not_declared_and_fields_read_as_integers():
    (st, fields, why), r = _read((0, "{}", ""))
    assert st == md.JUDGE_NOT_DECLARED and fields == {"declared": False, "declared_height": 0,
                                                      "last_verdict_height": 0, "missed_draws": 0}
    assert r.asked("miner-judge")[0][-1] == "dm1x"
    (st, fields, _), _r = _read((0, json.dumps({"declared": True, "declared_height": "40", "missed_draws": "2"}), ""))
    assert st == md.JUDGE_DECLARED and fields["declared_height"] == 40 and fields["missed_draws"] == 2


@pytest.mark.parametrize("answer", [
    (0, json.dumps({"judge": True}), ""),
    (0, json.dumps({"declared": "true"}), ""),
    (0, json.dumps({"declared": True, "missed_draws": "x"}), ""),
    (0, "declared: true", ""),
    (1, "", REFUSED_CONN),
    (1, "", "rpc error: code = Unimplemented desc = unknown method MinerJudge"),
])
def test_an_answer_this_daemon_was_not_written_against_is_unknown(answer):
    (st, fields, why), _r = _read(answer)
    assert st == md.JUDGE_READ_UNKNOWN and fields == {} and why


def test_a_dendrad_without_the_read_is_unsupported_and_runs_no_query():
    (st, _f, why), r = _read((0, "{}", ""), help_=(0, HELP_E14_DECLARE.replace("tx jobs", "query jobs"), ""))
    assert st == md.JUDGE_READ_UNSUPPORTED and "miner-judge" in why
    assert r.asked("miner-judge") == []
    (st, _f, _w), r = _read((0, "{}", ""), help_=(1, "", "exec failed"))
    assert st == md.JUDGE_READ_UNKNOWN and r.asked("miner-judge") == []


# == What is sent =====================================================================================================
A, N = md.EPOCH_APPLIED, md.EPOCH_NOT_APPLIED
D, ND = md.JUDGE_DECLARED, md.JUDGE_NOT_DECLARED


@pytest.mark.parametrize("acting,role,draining,read,want", [
    (A, True, False, ND, "true"),       # epoch 15, judge role, not declared: declare
    (A, True, False, D, None),          # already declared: never again (decision 4)
    (A, False, False, D, "false"),      # role off, declared: withdraw
    (A, False, False, ND, None),        # role off, not declared: nothing
    (A, True, True, D, "false"),        # draining: withdraw
    (A, True, True, ND, None),          # draining, not declared: nothing
    (N, True, False, ND, None),         # epoch 14: the message does not exist
    (None, True, False, ND, None),      # no reading yet
])
def test_the_declaration_table(acting, role, draining, read, want):
    assert md.judge_action(acting, role, draining, True, read) == want


def test_nothing_is_sent_off_epoch_15_unregistered_or_on_a_reading_that_is_not_known():
    for acting in (A, N, None, md.EPOCH_UNKNOWN):
        for role in (True, False):
            for draining in (True, False):
                for registered in (True, False):
                    for read in (D, ND, md.JUDGE_READ_UNKNOWN, md.JUDGE_READ_UNSUPPORTED):
                        got = md.judge_action(acting, role, draining, registered, read)
                        if acting != A or not registered or read not in (D, ND):
                            assert got is None, (acting, role, draining, registered, read)
                        assert not (got == "true" and read == D), "a second true launders the missed draws"
                        assert not (got == "false" and read == ND)


# == The seat, turn by turn ===========================================================================================
class Chain:
    """The declaration's reading and its transaction, for JudgeSeat."""

    def __init__(self, declared=False, read=None, land=True):
        self.declared, self.read_state, self.land = declared, read, land
        self.reads, self.sent = 0, []

    def reader(self, mid):
        self.reads += 1
        if self.read_state:
            return self.read_state, {}, "scripted"
        return (D if self.declared else ND), {"declared": self.declared, "declared_height": 40,
                                              "last_verdict_height": 0, "missed_draws": 0}, ""

    def send(self, mid, value):
        self.sent.append(value)
        self.declared = value == "true"
        return OK_BCAST if self.land else ""


def _seat(monkeypatch, chain, verb=HELP_E15_DECLARE):
    monkeypatch.setattr(md, "run", lambda c, t=None, stdin="": INCLUDED)
    r = Runner(**{"help:declare-judge": (0, verb, "")})
    return md.JudgeSeat("dm1x", md.KitCli(r), reader=chain.reader, send=chain.send), r


def test_a_seat_not_declared_is_declared_once(monkeypatch, capsys):
    c = Chain()
    s, _r = _seat(monkeypatch, c)
    assert s.turn(A, True, False, True, now=0.0) == "true"
    for k in range(1, 6):
        s.turn(A, True, False, True, now=k * md.JUDGE_TURN_S)
    assert c.sent == ["true"] and c.reads == 6
    assert "judge declared on chain at height 42" in capsys.readouterr().out


def test_a_declaration_whose_fate_was_not_read_is_not_sent_again_before_the_next_reading(monkeypatch, capsys):
    c = Chain(land=False)
    s, _r = _seat(monkeypatch, c)
    assert s.turn(A, True, False, True, now=0.0) is None
    s.turn(A, True, False, True, now=md.JUDGE_TURN_S - 1)
    assert c.sent == ["true"] and c.reads == 1
    s.turn(A, True, False, True, now=md.JUDGE_TURN_S)            # the late transaction landed: the chain holds it
    assert c.sent == ["true"] and c.reads == 2
    assert "not confirmed" in capsys.readouterr().out


def test_a_seat_already_declared_is_left_alone(monkeypatch):
    c = Chain(declared=True)
    s, _r = _seat(monkeypatch, c)
    for k in range(4):
        s.turn(A, True, False, True, now=k * md.JUDGE_TURN_S)
    assert c.sent == []


def test_a_role_off_withdraws_and_a_drain_wakes_the_seat(monkeypatch):
    c = Chain(declared=True)
    s, _r = _seat(monkeypatch, c)
    assert s.turn(A, False, False, True, now=0.0) == "false"
    c2 = Chain()
    s2, _r2 = _seat(monkeypatch, c2)
    s2.turn(A, True, False, True, now=0.0)
    s2.wake()
    assert s2.turn(A, True, True, True, now=1.0) == "false"
    assert c2.sent == ["true", "false"]


def test_off_epoch_15_or_unregistered_nothing_is_asked(monkeypatch):
    c = Chain()
    s, r = _seat(monkeypatch, c)
    for acting, registered in ((N, True), (None, True), (A, False)):
        assert s.turn(acting, True, False, registered, now=0.0) is None
    assert c.reads == 0 and c.sent == [] and r.calls == []
    assert md._STATUS["judge_declaration"]["state"] == "not_sent"


def test_a_dendrad_without_declare_judge_stops_the_seat_once(monkeypatch, capsys):
    c = Chain()
    s, r = _seat(monkeypatch, c, verb=HELP_E14_DECLARE)
    for k in range(5):
        s.turn(A, True, False, True, now=k * md.JUDGE_TURN_S)
    assert c.reads == 0 and c.sent == [] and len(r.calls) == 1
    assert s.stopped == "kit_cli_predates_plan"
    assert capsys.readouterr().out.count("predates the plan") == 1


@pytest.mark.parametrize("state,stops", [(md.JUDGE_READ_UNSUPPORTED, True), (md.JUDGE_READ_UNKNOWN, False)])
def test_a_reading_that_is_not_known_sends_nothing(monkeypatch, state, stops):
    c = Chain(read=state)
    s, _r = _seat(monkeypatch, c)
    for k in range(3):
        s.turn(A, True, False, True, now=k * md.JUDGE_TURN_S)
    assert c.sent == []
    assert c.reads == (1 if stops else 3)
