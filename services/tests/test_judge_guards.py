"""THE JUDGE'S GUARDS -- what it may post, where it may sit, when it stops, and what it counts.

Each function below decides something the chain cannot take back, and each is PURE or reads through an
injected seam, so it is exercised here without a chain, a relay or a model:
  * `divergence_guard`: a divergence ABSTAINS unless DENDRA_JUDGE_DIVERGENCE_SLASH is exactly "1"; word
    salad (the coherence stage) stays INVALID; an INVALID from a stage it does not know never passes;
  * `judge_revealed`: the order of the checks, the request's output cap carried to every reference, the
    coherence stage (`word_salad`) still run when that cap is unknown, and the guard applied to what
    `decide_verdict` returns -- with the REAL anchor computation (hash mode); a question that cannot be
    verified against the primary's commitment never graded (`prompt_check`), and one answer's coherence read
    once per process (`coherence_memo`);
  * `ambiguity_vote_allowed`: a retried audit whose references once agreed never votes on an ambiguity;
  * `seat_reading` / `own_verdict_reading`: three states each, and the unknown one never skips a seat;
  * `request_max_out`, `retry_delay`, `verdict_post_outcome`, `JudgeLedger`, the judge state's counters,
    the judge-call timeout and keep_alive, and the chain's deferral texts.
The loop that wires them together is exercised by tests/test_judge_worker_loop.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# THE ROOT IS POINTABLE (DENDRA_MODEA), as in tests/test_relay_startup_alarm.py: the functions are imported, and the
# worker is run, from a mutated copy when one is named, never from this file's own tree.
MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))

import judge_worker as jw  # noqa: E402
from modea import crypto  # noqa: E402
from modea import heartbeat as hb  # noqa: E402
from modea import judge as J  # noqa: E402
from modea import miner as M  # noqa: E402


class RecordingBackend:
    """Answers scripted references in order (the last one repeats) and records the cap of every call."""

    def __init__(self, refs):
        self.refs, self.i, self.max_outs = list(refs), 0, []

    def generate(self, prompt, max_out=0, temperature=None, timeout_s=None):
        self.max_outs.append(max_out)
        r = self.refs[min(self.i, len(self.refs) - 1)]
        self.i += 1
        return r


def _fns(coherent=True, relevant=True, multi=False):
    return dict(coherent_fn=lambda *a, **k: coherent, relevant_fn=lambda *a, **k: relevant,
                judge_fn=lambda a, b, **k: a.strip() == b.strip(), multiok_fn=lambda *a, **k: multi)


QUESTION = "Capital of France?"
SALT = crypto.prompt_salt(crypto.gen_keypair()[0], "jobX")


@pytest.fixture
def hashed(monkeypatch):
    """The anchor computed by the shipped function, in hash mode: no engine needed, nothing injected. By default
    the reveal carries its salt and the commit anchors the commitment to its question, as a primary of the kit
    makes them -- a question that cannot be verified is a case of its own (`unverified`)."""
    monkeypatch.setattr(M, "_EMBED_MODE", "hash")
    monkeypatch.setattr(jw, "GEN_DELAY", 0.0)
    monkeypatch.setattr(jw, "_COHERENCE_READ", {})     # one process per case: no reading carried between them

    def judge(answer, refs, *, anchored_answer=None, max_out=96, armed=False, vote=False, fns=None,
              get_fields=None, rev=None, job="jobX"):
        anchor = M.answer_embedding(anchored_answer if anchored_answer is not None else answer)
        b = RecordingBackend(refs)
        fields = {"prompt": jw.rv.prompt_commitment(SALT, QUESTION), "result": anchor}
        out = jw.judge_revealed(job, "m-prim", rev if rev is not None else
                                {"prompt": QUESTION, "answer": answer, "psalt": SALT},
                                backend=b, judge_model="qwen-judge", max_out=max_out,
                                get_root=lambda k: anchor if k == job + "__m-prim" else "",
                                get_fields=get_fields or (lambda k: fields),
                                divergence_armed=armed, abstain_vote=vote, verdict_fns=fns or _fns())
        return out, b
    return judge


# ═══ (1) THE DIVERGENCE GUARD ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("verdict,stage,armed,expected", [
    (False, "slash", False, (None, "divergence-held")),
    (False, "legacy", False, (None, "divergence-held")),
    (False, "slash", True, (False, "slash")),
    (False, "legacy", True, (False, "legacy")),
    (False, "coherence", False, (False, "coherence")),      # word salad stays INVALID, guard on or off
    (False, "coherence", True, (False, "coherence")),
    (False, "", True, (None, "invalid-unattributed")),       # an INVALID from nowhere never passes
    (False, "relevance", True, (None, "invalid-unattributed")),
    (True, "sc-valid", False, (True, "sc-valid")),
    (None, "sc-unstable", True, (None, "sc-unstable")),
])
def test_the_divergence_guard(verdict, stage, armed, expected):
    assert jw.divergence_guard(verdict, stage, armed) == expected


def test_a_withheld_invalid_never_becomes_a_valid_vote():
    for st in ("divergence-held", "invalid-unattributed"):
        assert jw.abstain_to_vote(st, True) is False


@pytest.mark.parametrize("value,armed", [("1", True), (None, False), ("", False), ("0", False),
                                          ("true", False), ("yes", False), (" 1", False), ("1 ", False)])
def test_only_exactly_1_arms_the_divergence_slash(value, armed):
    assert jw.divergence_slash_armed(value) is armed


# ═══ (2) ONE REVEALED AUDIT, DECIDED ═════════════════════════════════════════════════════════════════════════
def test_an_honest_answer_is_valid_and_every_reference_carries_the_requests_cap(hashed):
    (v, stage, _why, tr), b = hashed("Paris.", ["Paris.", "Paris."])
    assert (v, stage) == (True, "sc-valid")
    assert b.max_outs == [96, 96], "the references are bounded by the audited request, not by a constant"


def test_a_divergence_abstains_while_the_guard_is_on(hashed):
    (v, stage, why, _tr), b = hashed("Berlin.", ["Paris.", "Paris.", "Paris."])
    assert (v, stage) == (None, "divergence-held"), why
    assert b.max_outs == [96, 96, 96]


def test_a_divergence_is_invalid_once_armed(hashed):
    (v, stage, _why, _tr), _b = hashed("Berlin.", ["Paris.", "Paris.", "Paris."], armed=True)
    assert (v, stage) == (False, "slash")


def test_word_salad_stays_invalid_with_the_guard_on(hashed):
    (v, stage, _why, _tr), b = hashed("capital Paris France the is", ["x"], fns=_fns(coherent=False))
    assert (v, stage) == (False, "coherence") and b.max_outs == []


def test_the_legacy_one_reference_path_is_guarded_too(hashed, monkeypatch):
    monkeypatch.setattr(jw, "SELFCONSIST", False)
    (v, stage, _w, _t), _b = hashed("Berlin.", ["Paris."])
    assert (v, stage) == (None, "divergence-held")
    (v, stage, _w, _t), _b = hashed("Berlin.", ["Paris."], armed=True)
    assert (v, stage) == (False, "legacy")


def test_an_unknown_cap_abstains_before_any_generation(hashed):
    (v, stage, _why, _tr), b = hashed("Paris.", ["Paris."], max_out=None)
    assert (v, stage) == (None, "request-unreadable") and b.max_outs == []
    assert stage in jw.RETRY_STAGES


@pytest.mark.parametrize("armed", [False, True])
def test_word_salad_is_invalid_even_when_the_cap_is_unknown(hashed, armed):
    (v, stage, _why, tr), b = hashed("capital Paris France the is", ["x"], max_out=None, armed=armed,
                                     fns=_fns(coherent=False))
    assert (v, stage) == (False, "coherence") and b.max_outs == []
    assert tr == {"stage": "coherence", "refs": [], "n_gen": 0}


def test_an_unreadable_coherence_read_with_an_unknown_cap_abstains(hashed):
    (v, stage, _why, _tr), b = hashed("capital Paris France the is", ["x"], max_out=None, fns=_fns(coherent=None))
    assert (v, stage) == (None, "request-unreadable") and b.max_outs == []


def test_with_the_coherence_stage_off_an_unknown_cap_abstains(hashed, monkeypatch):
    monkeypatch.setattr(jw, "TWOSTAGE", False)
    (v, stage, _why, _tr), b = hashed("capital Paris France the is", ["x"], max_out=None, fns=_fns(coherent=False))
    assert (v, stage) == (None, "request-unreadable") and b.max_outs == []


def test_word_salad_is_only_a_definite_false():
    calls = []

    def coh(value):
        def f(text, model=None):
            calls.append((text, model))
            return value
        return f
    assert jw.word_salad("a", "m", coherent_fn=coh(False)) is True
    assert calls == [("a", "m")]
    assert jw.word_salad("a", "m", coherent_fn=coh(None)) is False
    assert jw.word_salad("a", "m", coherent_fn=coh(True)) is False
    calls.clear()
    assert jw.word_salad("a", "m", coherent_fn=coh(False), twostage=False) is False and calls == []


def test_a_request_without_a_cap_is_served_uncapped_like_the_miner_served_it(hashed):
    (v, _stage, _why, _tr), b = hashed("Paris.", ["Paris.", "Paris."], max_out=0)
    assert v is True and b.max_outs == [0, 0]


def test_a_question_that_does_not_open_its_commitment_abstains_first(hashed):
    salt = crypto.prompt_salt(crypto.gen_keypair()[0], "jobX")
    fields = {"prompt": "ab" * 32, "result": "embedding"}
    (v, stage, _why, _tr), b = hashed("Paris.", ["Paris."], get_fields=lambda k: fields,
                                      rev={"prompt": "Capital of France?", "answer": "Paris.", "psalt": salt})
    assert (v, stage) == (None, "prompt-mismatch") and b.max_outs == []


# ═══ (2b) A QUESTION THAT CANNOT BE VERIFIED IS NEVER GRADED ═════════════════════════════════════════════════
# Measured by the adversarial review of 2026-10-09 on the shipped judge_revealed: a WRONG answer revealed without
# its salt, next to a question it answers right, was voted VALID (`sc-valid`) -- with a real prompt commitment
# anchored and with a legacy one. The question gate answered None, and None fell through to the grading.
CHOSEN = "Capital of Germany?"     # the question the cheat reveals; "Berlin." is right for it, not for QUESTION


@pytest.mark.parametrize("case", ["no-salt", "legacy", "no-prompt-commit", "salt-not-text"])
def test_an_unverified_question_is_never_graded(hashed, case):
    anchor = M.answer_embedding("Berlin.")
    fields = {"prompt": jw.rv.prompt_commitment(SALT, QUESTION), "result": anchor}
    rev = {"prompt": CHOSEN, "answer": "Berlin."}
    if case == "legacy":
        rev["psalt"], fields = SALT, {"prompt": anchor, "result": anchor}
    elif case == "no-prompt-commit":
        rev["psalt"], fields = SALT, {"prompt": "", "result": anchor}
    elif case == "salt-not-text":
        rev["psalt"] = 7
    (v, stage, why, _tr), b = hashed("Berlin.", ["Berlin.", "Berlin."], rev=rev, get_fields=lambda k: fields,
                                     vote=True)
    assert (v, stage) == (None, "prompt-unverified"), why
    assert b.max_outs == [], "no reference is generated against a question nobody verified"
    assert stage not in jw.RETRY_STAGES and jw.abstain_to_vote(stage, True) is False


def test_an_unverified_question_leaves_the_coherence_stage_its_vote(hashed):
    rev = {"prompt": CHOSEN, "answer": "capital Paris France the is"}
    (v, stage, _w, _t), b = hashed("capital Paris France the is", ["x"], rev=rev, fns=_fns(coherent=False))
    assert (v, stage) == (False, "coherence") and b.max_outs == []


def test_a_commit_that_could_not_be_read_is_tried_again_before_any_coherence_read(hashed):
    reads = []
    fns = dict(_fns(), coherent_fn=lambda t, model=None: reads.append(t) or False)
    for got in ({}, None):
        (v, stage, _w, _t), b = hashed("Paris.", ["Paris."], get_fields=lambda k: got, fns=fns)
        assert (v, stage) == (None, "prompt-unreadable") and stage in jw.RETRY_STAGES
        assert reads == [] and b.max_outs == []


def test_prompt_check_says_which_none_is_final():
    pc = jw.rv.prompt_commitment(SALT, QUESTION)
    ok = {"prompt": QUESTION, "answer": "a", "psalt": SALT}
    good = lambda k: {"prompt": pc, "result": "r"}  # noqa: E731
    assert jw.prompt_check("j", "p", ok, get_fields=good) == (True, "match")
    assert jw.prompt_check("j", "p", dict(ok, prompt="other"), get_fields=good) == (False, "mismatch")
    never = lambda k: 1 / 0  # noqa: E731 -- a reveal without a salt reads nothing on chain
    assert jw.prompt_check("j", "p", {"prompt": QUESTION, "answer": "a"}, get_fields=never) == (None, "no-salt")
    assert jw.prompt_check("j", "p", ["x"], get_fields=never) == (None, "no-salt")
    assert jw.prompt_check("j", "p", ok, get_fields=lambda k: {}) == (None, "unreadable")
    assert jw.prompt_check("j", "p", ok, get_fields=lambda k: {"prompt": "", "result": "r"}) == (None, "no-prompt-commit")
    assert jw.prompt_check("j", "p", ok, get_fields=lambda k: {"prompt": "r", "result": "r"}) == (None, "legacy")
    for rev in (ok, dict(ok, prompt="other"), dict(ok, psalt=None)):
        assert jw.revealed_prompt_matches("j", "p", rev, get_fields=good) == jw.prompt_check("j", "p", rev, get_fields=good)[0]


@pytest.mark.parametrize("value,armed", [(None, True), ("", True), ("1", True), ("0", False), ("true", False)])
def test_the_ambiguity_vote_reads_an_empty_value_as_unset(value, armed):
    """deploy/testnet-miner/docker-compose.yml forwards DENDRA_JUDGE_ABSTAIN_VOTE EMPTY when the .env names none:
    read as "not 1", it would have switched option alpha-(a) off on every kit judge."""
    env = {k: v for k, v in os.environ.items() if k != "DENDRA_JUDGE_ABSTAIN_VOTE"}
    if value is not None:
        env["DENDRA_JUDGE_ABSTAIN_VOTE"] = value
    r = subprocess.run([sys.executable, "-c", "import judge_worker as j; print(j.ABSTAIN_VOTE)"], cwd=str(MODEA),
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip().splitlines()[-1:] == [str(armed)], r.stdout + r.stderr


# ═══ (2c) A RETRY IS NOT A SECOND DRAW ═══════════════════════════════════════════════════════════════════════
def test_after_its_references_agreed_a_retried_audit_never_votes_on_an_ambiguity():
    assert jw.AGREED_REFS_STAGES <= jw.RETRY_STAGES
    assert jw.ambiguity_vote_allowed("j", set(), True) is True
    assert jw.ambiguity_vote_allowed("j", {"j"}, True) is False
    assert jw.ambiguity_vote_allowed("j", {"k"}, False) is False


@pytest.mark.parametrize("max_out", [None, 96])
def test_the_coherence_of_one_answer_is_read_once_per_process(hashed, max_out):
    reads = []

    def coh(text, model=None):
        reads.append(text)
        return True
    fns = dict(_fns(), coherent_fn=coh)
    for _ in range(3):                    # three attempts at one audit
        hashed("Paris.", ["Paris."], max_out=max_out, fns=fns)
    assert reads == ["Paris."], reads
    reads.clear()
    hashed("Paris.", ["Paris."], max_out=max_out, fns=fns, job="jobY")
    assert reads == ["Paris."], "another audit of the same answer is read on its own"
    reads.clear()
    unread = dict(_fns(), coherent_fn=lambda t, model=None: reads.append(t) or None)
    for _ in range(2):
        hashed("Bonn.", ["Bonn."], max_out=max_out, fns=unread)
    assert reads == ["Bonn.", "Bonn."], "an unreadable reading decides nothing and is not kept"


def test_an_answer_other_than_the_anchored_one_abstains(hashed):
    (v, stage, _why, _tr), b = hashed("Berlin is the capital of Germany.", ["x"],
                                      anchored_answer="Paris is the capital of France.")
    assert (v, stage) == (None, "anchor-mismatch") and b.max_outs == []


@pytest.mark.parametrize("rev", [{"prompt": 3, "answer": "Paris."}, {"prompt": "q"}, ["not", "a", "dict"]])
def test_a_reveal_of_the_wrong_shape_abstains(hashed, rev):
    (v, stage, _why, _tr), _b = hashed("Paris.", ["x"], rev=rev)
    assert (v, stage) == (None, "reveal-shape")


def test_a_proven_ambiguity_votes_valid_only_when_option_alpha_is_on(hashed):
    (v, stage, _w, _t), _b = hashed("JavaScript", ["Python", "Rust"], vote=True)
    assert (v, stage) == (True, "sc-diverge")
    (v, stage, _w, _t), _b = hashed("JavaScript", ["Python", "Rust"], vote=False)
    assert (v, stage) == (None, "sc-diverge")


def test_decide_verdict_passes_the_cap_only_when_it_has_one():
    class Plain:   # an older bench backend, without a max_out parameter
        def generate(self, prompt, temperature=None):
            return "Paris."
    v = jw.decide_verdict("Paris.", "q", Plain(), "m", gen_delay=0, **_fns())
    assert v is True
    b = RecordingBackend(["Paris.", "Paris."])
    assert jw.decide_verdict("Paris.", "q", b, "m", gen_delay=0, max_out=512, **_fns()) is True
    assert b.max_outs == [512, 512]


# ═══ (3) WHERE THE JUDGE SITS, AND WHETHER IT ALREADY VOTED ══════════════════════════════════════════════════
NOT_FOUND_JURY = ("Error: rpc error: code = NotFound desc = no jury anchored for this job (the audit was never "
                  "opened, or was deferred for want of an eligible pool) -- this is NOT an empty jury")


@pytest.mark.parametrize("rc,out,err,state", [
    (0, '{"members":["m-judge","m-x"],"anchored_height":"5"}', "", "seated"),
    (0, '{"members":["m-x","m-judge2"]}', "", "not-seated"),
    (0, '{"anchored_height":"5"}', "", "not-seated"),          # proto3: no members = the EMPTY jury
    (0, "{}", "", "not-seated"),
    (0, '{"code":5,"message":"rpc error"}', "", "unknown"),    # not the jury message: never read as empty
    (0, '{"members":["m-judge"]}', "warning: a newer toolchain", "seated"),   # stderr never spoils stdout
    (1, "", NOT_FOUND_JURY, "no-jury"),
    (1, "", "Error: rpc error: code = Unavailable desc = connection refused", "unknown"),
    (None, "", "FileNotFoundError: dendrad", "unknown"),
    (0, "not json", "", "unknown"),
    (0, '{"members":"m-judge"}', "", "unknown"),
    (0, "[1, 2]", "", "unknown"),
])
def test_the_seat_reading(rc, out, err, state):
    got, why = jw.seat_reading(rc, out, err, "m-judge")
    assert got == state, why


def test_the_seat_is_read_from_the_anchored_jury_not_the_work_committee():
    seen = []

    def runner(argv):
        seen.append(argv)
        return 0, '{"members":["m-judge"]}', ""
    assert jw.read_seat("job-1", "m-judge", runner=runner)[0] == "seated"
    argv = seen[0]
    assert "audit-committee" in argv and "assigned-committee" not in argv
    assert argv[argv.index("--") + 1:] == ["job-1"]


@pytest.mark.parametrize("rc,out,err,state", [
    (0, '{"commit":{"index":"j__verdict__m","result":"1"}}', "", "present"),
    (0, '{"commit":{"index":"j__verdict__m","result":"0"}}', "", "present"),
    (1, "", "Error: rpc error: code = NotFound desc = not found", "absent"),
    (1, "", "Error: rpc error: code = Unavailable desc = connection refused 0 1", "unknown"),
    (0, "gas estimate 100 1", "", "unknown"),                    # noise with a 0 and a 1 is never "voted"
    (0, '{"commit":null}', "", "unknown"),
    (None, "", "TimeoutExpired", "unknown"),
])
def test_the_own_verdict_reading(rc, out, err, state):
    assert jw.own_verdict_reading(rc, out, err) == state


@pytest.mark.parametrize("envelope,cap", [
    ({"max_out": 96, "ct": "aa"}, 96), ({"max_out": "128"}, 128), ({"ct": "aa"}, 0),
    (None, None), ({"max_out": None}, None), ({"max_out": "lots"}, None), ([96], None),
])
def test_the_requests_cap_is_read_as_the_miner_reads_it(envelope, cap):
    assert jw.request_max_out(envelope) == cap


def test_the_retry_pause_grows_then_caps_without_overflow():
    ds = [jw.retry_delay(n) for n in range(1, 10)]
    assert ds[:3] == [60.0, 120.0, 240.0] and ds == sorted(ds) and max(ds) == 1800.0
    assert jw.retry_delay(10 ** 9) == 1800.0 and jw.retry_delay(0) == 60.0
    assert jw.retry_delay(3, base=5.0) == 20.0 and jw.retry_delay(9, base=0.0) == 0.0


def test_only_the_audits_this_judge_could_not_judge_are_retried():
    # could not judge: an engine, the relay or the chain did not answer, or the judge model's answer was unreadable
    assert jw.RETRY_STAGES == {"judge-error", "gen-fail", "request-unreadable", "anchor-unreadable",
                               "sc-unreadable", "multiok-unreadable", "prompt-unreadable"}
    # judged: every exit stage that IS a judgment, or a proof that this audit cannot be judged here
    for judged in ("divergence-held", "sc-unstable", "sc-diverge", "sc-valid", "multiok", "slash", "legacy",
                   "coherence", "prompt-mismatch", "anchor-mismatch", "anchor-shape", "relevance", "reveal-shape",
                   "invalid-unattributed", "prompt-unverified"):
        assert judged not in jw.RETRY_STAGES
    # an unreadable read retried is never a vote by itself
    for st in ("sc-unreadable", "multiok-unreadable"):
        assert jw.abstain_to_vote(st, True) is False


@pytest.mark.parametrize("args,named", [
    (["--passes", "-1"], "--passes"), (["--retry-pause", "-1"], "--retry-pause"),
    (["--retry-pause", "nan"], "--retry-pause"), (["--retry-pause", "inf"], "--retry-pause"),
])
def test_a_wrong_pass_count_or_pause_stops_the_worker_at_start(tmp_path, args, named):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(MODEA / "judge_worker.py"), "--id", "m", "--relay", "http://x",
                        "--keydir", str(tmp_path), *args], env=env, capture_output=True, text=True, timeout=60)
    last = (r.stderr.strip().splitlines() or [""])[-1]   # the usage line above it names every option
    assert r.returncode == 2 and "error:" in last and named in last, r.stderr[-400:]


# ═══ (4) WHAT BECAME OF A VERDICT TRANSACTION ═══════════════════════════════════════════════════════════════
H = "AB" * 32


@pytest.mark.parametrize("broadcast,block,state", [
    ("txhash: " + H + "\n", 'height: "7"\ntxhash: ' + H + "\n", "anchored"),     # proto3 omits code 0
    ("code: 0\ntxhash: " + H + "\n", 'code: 0\nheight: "7"\n', "anchored"),
    ("code: 0\ntxhash: " + H + "\n",
     "code: 18\nheight: \"7\"\nraw_log: 'failed to execute message; message index: 0: index already set: invalid request'\n",
     "already-set"),
    ("code: 0\ntxhash: " + H + "\n", "code: 11\nheight: \"7\"\nraw_log: 'out of gas'\n", "refused"),
    ("code: 4\nraw_log: 'only the miner''s operator may anchor its commit: unauthorized'\ntxhash: " + H + "\n",
     "", "refused"),
    ("code: 18\nraw_log: 'index already set'\ntxhash: " + H + "\n", "", "already-set"),
    ("code: 32\nraw_log: 'account sequence mismatch, expected 5, got 4: incorrect account sequence'\ntxhash: "
     + H + "\n", "", "unconfirmed"),                                                # contention, not a refusal
    ("Error: m-judge.info: key not found\n", "", "unconfirmed"),
    ("unknown flag: --gas-adjustement\n", "", "unconfirmed"),
    ("", "", "unconfirmed"),
    ("code: 0\ntxhash: " + H + "\n", "", "unconfirmed"),                            # inclusion not seen
])
def test_the_verdict_post_outcome(broadcast, block, state):
    got, detail = jw.verdict_post_outcome(broadcast, include=lambda out: block)
    assert got == state, detail
    if state == "refused":
        assert "code=" in detail


def test_the_transaction_code_has_three_states():
    assert jw._tx_code("code: 5\ntxhash: " + H) == 5
    assert jw._tx_code("txhash: " + H) == 0
    assert jw._tx_code("Error: connection refused") is None
    assert jw._ok("txhash: " + H) is True and jw._ok("nothing") is False


# ═══ (5) WHAT THE JUDGE COUNTS, AND WHERE ════════════════════════════════════════════════════════════════════
def test_one_abstention_line_per_audit_and_stage_every_attempt_counted():
    led = jw.JudgeLedger(now=100)
    assert led.abstain("j1", "judge-error") is True
    assert led.abstain("j1", "judge-error") is False
    assert led.abstain("j1", "gen-fail") is True
    assert led.abstain("j2", "judge-error") is True
    d = led.doc()
    assert d["abstentions"] == {"gen-fail": 1, "judge-error": 3}


def test_the_ledger_counts_seats_verdicts_and_refusals():
    led = jw.JudgeLedger(now=100)
    for job, st in (("a", "seated"), ("a", "seated"), ("b", "not-seated"), ("c", "unknown"), ("d", "no-jury")):
        led.seat(job, st)
    led.verdict("1")
    led.verdict("1")
    led.verdict("0")
    led.already_on_chain("e")
    led.already_on_chain("e")
    led.refusal("f", "code=4 unauthorized", now=200)
    d = led.doc()
    assert (d["seats_seen"], d["not_seated"], d["seats_unread"]) == (1, 1, 1)
    assert d["verdicts_anchored"] == {"0": 1, "1": 2}
    assert d["verdicts_already_on_chain"] == 1 and d["verdicts_refused"] == 1
    assert d["last_refusal"] == {"at": 200, "job": "f", "why": "code=4 unauthorized"}
    assert d["since"] == 100 and "refused 1" in led.line()


def test_the_counters_ride_on_the_state_this_process_wrote(tmp_path):
    p = str(tmp_path / "judge-state.json")
    assert hb.update_judge_counters({"x": 1}, path=p) is False
    assert not os.path.exists(p), "counts never create a state: they would be figures about nobody"
    hb.write_judge_state("qwen-judge", "chain", path=p)
    assert hb.update_judge_counters({"verdicts_refused": 0}, path=p) is True
    doc = json.loads(Path(p).read_text())
    assert (doc["model"], doc["source"], doc["pid"]) == ("qwen-judge", "chain", os.getpid())
    assert doc["counters"] == {"verdicts_refused": 0}


def test_a_state_of_another_process_is_not_written_on(tmp_path):
    p = tmp_path / "judge-state.json"
    other = {"schema": 1, "pid": os.getpid() + 1, "starttime": 1, "model": "m", "source": "chain",
             "written_at": 1}
    p.write_text(json.dumps(other))
    assert hb.update_judge_counters({"x": 1}, path=str(p)) is False
    assert json.loads(p.read_text()) == other


def test_the_judge_state_carries_counters_when_given(tmp_path):
    p = str(tmp_path / "s.json")
    hb.write_judge_state("m", "env", path=p, counters={"seats_seen": 2})
    assert json.loads(Path(p).read_text())["counters"] == {"seats_seen": 2}
    hb.write_judge_state("m", "env", path=p)
    assert "counters" not in json.loads(Path(p).read_text())


# ═══ (6) THE JUDGE CALL: its bound and its keep_alive ═══════════════════════════════════════════════════════
def test_the_judge_timeout(monkeypatch):
    monkeypatch.delenv("DENDRA_JUDGE_TIMEOUT_S", raising=False)
    assert J.judge_timeout_s() == 120.0
    monkeypatch.setenv("DENDRA_JUDGE_TIMEOUT_S", "45")
    assert J.judge_timeout_s() == 45.0
    for bad in ("0", "-3", "abc", "nan", "inf"):
        monkeypatch.setenv("DENDRA_JUDGE_TIMEOUT_S", bad)
        with pytest.raises(ValueError):
            J.judge_timeout_s()


def test_a_judge_call_uses_the_timeout_and_keeps_the_model_an_hour(monkeypatch):
    seen = {}

    class _R:
        def read(self):
            return b'{"response": "OUI"}'

    def fake_urlopen(req, timeout=None):
        seen["timeout"], seen["payload"] = timeout, json.loads(req.data)
        return _R()
    monkeypatch.setattr(J.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("DENDRA_JUDGE_KEEPALIVE", raising=False)
    monkeypatch.setenv("DENDRA_JUDGE_TIMEOUT_S", "33")
    monkeypatch.delenv("DENDRA_JUDGE_BACKEND", raising=False)
    assert J.llm_judge("a", "a", model="qwen-judge", endpoint="http://127.0.0.1:9") is True
    assert seen["timeout"] == 33.0 and seen["payload"]["keep_alive"] == "1h"
    monkeypatch.setenv("DENDRA_JUDGE_KEEPALIVE", "30m")
    J.llm_coherent("a", model="qwen-judge", endpoint="http://127.0.0.1:9", timeout=7)
    assert seen["timeout"] == 7 and seen["payload"]["keep_alive"] == "30m"


def test_a_wrong_timeout_stops_the_worker_at_start(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "DENDRA_JUDGE_TIMEOUT_S": "abc",
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(MODEA / "judge_worker.py"), "--id", "m", "--relay", "http://x",
                        "--keydir", str(tmp_path)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "REFUSED" in r.stderr and "DENDRA_JUDGE_TIMEOUT_S" in r.stderr


# ═══ (7) THE CHAIN'S OWN DEFERRALS ARE NOT REAL FAILURES ════════════════════════════════════════════════════
@pytest.mark.parametrize("raw,state", [
    ("failed to execute message; message index: 0: verdicts below the relative bar (neither slash nor "
     "vindication at 2/3 of the anchored seats) -> the adjudication does not conclude; the audit timeout will "
     "defer: invalid request", "deferred"),
    ("failed to execute message; message index: 0: no open dispute for this job: invalid request", "deferred"),
    ("failed to execute message; message index: 0: fresh committee below quorum (re-commits < 2/3 of the "
     "anchored seats and verdicts < participation floor) -> the audit timeout will decide", "deferred"),
    ("failed to execute message; message index: 0: fresh committee carries no stake: invalid request",
     "deferred"),
    ("failed to execute message; message index: 0: dispute already resolved (anti-replay)", "deferred"),
    ("failed to execute message; message index: 0: unauthorized", "FAILED"),
])
def test_the_chains_deferrals_are_deferrals(monkeypatch, raw, state):
    block = f"code: 18\nheight: \"9\"\nraw_log: '{raw}'\ntxhash: {H}\n"
    monkeypatch.setattr(jw, "_confirm_tx_text", lambda out, timeout=24: (False, block))
    got, detail = jw.adjudicate_outcome("code: 0\ntxhash: " + H + "\n")
    assert got == state, detail
