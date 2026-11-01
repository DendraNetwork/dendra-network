"""Bench of the second grader (owner's decision of 2026-10-15, question 6 of the work plan): every grade names
the grader that posted it, each grader token is bound to one name, two graders grade the same answers, and the
facts layer reads an answer as incoherent only when every grader that graded it says so.

Cases: one grader (the days before the second grader are read as before), two graders agreeing, two graders
disagreeing, a grade without a name (the first grader's, filed before the names existed), and the replay of
the evidence log by the service (which grader graded what, what each is still offered, and the alert of a
ranked day with no grade).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_facts as F  # noqa: E402
from final_season_calc import gross_of  # noqa: E402
from test_final_season_server import AUTH, GRADER, _rank, _sealed_day, call, srv, work  # noqa: E402,F401

CPU = "c" * 40                                         # the second grader's token
AUTH_CPU = {"Authorization": "Bearer " + CPU}


def rec(t, mid, day=0, **kw):
    return dict(kw, type=t, miner_id=mid, day=day)


def grade(jid, coherent, grader=None, mid="m", day=0):
    """One grade of answer `jid`: by `grader`, or with no name as the first grader filed them before names."""
    g = rec("work_grade", mid, day=day, job_id=jid, coherent=coherent, model="x")
    if grader is not None:
        g["grader"] = grader
    return g


def sampled(n, mid="m", day=0):
    out = [rec("work_answer", mid, day=day, job_id=f"j{k}", prompt="p", answer="a") for k in range(n)]
    out.append({"type": "work_sealed", "day": day, "answers": n, "drawn": n,
                "answered": {mid: [f"j{k}" for k in range(n)]}})
    return out


def verified(records, work=6):
    chain = {"work": {"m": work}, "verdicts": {}, "presence": {"m": 10}}
    return F.day_identities(0, records, chain, {"m": "a"})[0].verified_requests


def unwound(records):
    """`unwound_work` of one unwound job j0 of m, answered: (paid, its grading)."""
    paid, proof = F.unwound_work(0, records, {"j0": {"miner_id": "m", "state": ""}}, {"j0"})
    return paid, proof["j0"]["graded"]


# -- the combination ---------------------------------------------------------------------------------------
def test_an_answer_is_incoherent_only_when_every_grader_that_graded_it_says_so():
    assert F.answer_incoherent({}) is False                                  # no grade: never incoherent
    assert F.answer_incoherent({"gpu": [False]}) is True
    assert F.answer_incoherent({"gpu": [True]}) is False
    assert F.answer_incoherent({"gpu": [False], "cpu": [False]}) is True     # both say no
    assert F.answer_incoherent({"gpu": [False], "cpu": [True]}) is False     # one says yes: the doubt favours the miner
    assert F.answer_incoherent({"gpu": [True], "cpu": [False]}) is False
    assert F.answer_incoherent({"gpu": [False, False], "cpu": [False]}) is True


def test_a_grade_without_a_name_is_the_first_graders():
    assert F.FIRST_GRADER == "gpu"
    assert F.grader_of({"type": "work_grade", "coherent": True}) == F.FIRST_GRADER
    assert F.grader_of({"type": "work_grade", "coherent": True, "grader": ""}) == F.FIRST_GRADER
    assert F.grader_of({"type": "work_grade", "coherent": True, "grader": "cpu"}) == "cpu"


# -- one grader: unchanged ---------------------------------------------------------------------------------
def test_one_grader_reads_as_before_with_or_without_its_name():
    # the void rule of the days before the second grader (test_final_season_facts), on nameless grades
    assert verified(sampled(1) + [grade("j0", False)]) == 0
    assert verified(sampled(3) + [grade("j0", False)]) == 6
    assert verified(sampled(3) + [grade("j0", False), grade("j1", False)]) == 0
    assert verified(sampled(3) + [grade("j0", False), grade("j1", True)]) == 6
    # the same grades, named by the first grader as the service files them now: the same rows
    named = lambda g: dict(g, grader=F.FIRST_GRADER)  # noqa: E731
    for grades in ([grade("j0", False)], [grade("j0", False), grade("j1", False)], [grade("j0", False), grade("j1", True)]):
        assert verified(sampled(3) + grades) == verified(sampled(3) + [named(g) for g in grades])
    # and the grading of an unwound job's own answer
    assert unwound(sampled(1) + [grade("j0", True)]) == ({"m": 1}, "own_answer_coherent")
    assert unwound(sampled(1) + [grade("j0", False)]) == ({}, "own_answer_incoherent")
    assert unwound(sampled(1) + [grade("j0", True, F.FIRST_GRADER)]) == ({"m": 1}, "own_answer_coherent")


# -- two graders -------------------------------------------------------------------------------------------
def test_two_graders_agreeing_on_incoherent_void_the_day_and_count_one_graded_answer():
    both = [grade("j0", False, "gpu"), grade("j0", False, "cpu")]
    assert verified(sampled(1) + both) == 0
    # three sampled: two grades of the same answer are one graded answer, below the two the void rule needs
    assert verified(sampled(3) + both) == 6
    assert verified(sampled(3) + both + [grade("j1", False, "gpu"), grade("j1", False, "cpu")]) == 0
    assert unwound(sampled(1) + both) == ({}, "own_answer_incoherent")
    # agreeing on coherent keeps everything
    assert verified(sampled(1) + [grade("j0", True, "gpu"), grade("j0", True, "cpu")]) == 6


def test_two_graders_disagreeing_keep_the_answer_coherent():
    split = [grade("j0", False, "gpu"), grade("j0", True, "cpu")]
    assert verified(sampled(1) + split) == 6
    assert verified(sampled(1) + [grade("j0", True, "gpu"), grade("j0", False, "cpu")]) == 6
    # a day of three answers all split: nothing is voided, whichever grader said no
    recs = sampled(3) + [grade(f"j{k}", k % 2 == 0, "gpu") for k in range(3)] + \
        [grade(f"j{k}", k % 2 == 1, "cpu") for k in range(3)]
    assert verified(recs) == 6
    # the unwound job's own answer: paid on the one coherent grade, and the proof calls it coherent
    assert unwound(sampled(1) + split) == ({"m": 1}, "own_answer_coherent")
    # the day's reading for another unwound job: one answer split is one coherent grade of the day
    paid, proof = F.unwound_work(0, sampled(2) + split, {"j1": {"miner_id": "m", "state": ""}}, {"j1"})
    assert paid == {"m": 1} and proof["j1"]["graded"] == "day_coherent"


def test_a_nameless_grade_and_a_named_one_combine_as_two_graders():
    # a nameless incoherent grade (the first grader, before names) and the second grader's coherent one
    assert verified(sampled(1) + [grade("j0", False), grade("j0", True, "cpu")]) == 6
    assert verified(sampled(1) + [grade("j0", False), grade("j0", False, "cpu")]) == 0
    # a nameless grade and a grade named by the first grader are the same grader
    own = F._own_grades([grade("j0", False), grade("j0", False, F.FIRST_GRADER)])
    assert own == {("j0", "m"): {F.FIRST_GRADER: [False, False]}}


def test_a_voided_day_still_takes_its_presence_only_when_both_graders_say_incoherent():
    chain = {"work": {"m": 6}, "verdicts": {}, "presence": {"m": 10}}
    both = F.day_identities(0, sampled(1) + [grade("j0", False, "gpu"), grade("j0", False, "cpu")], chain, {"m": "a"})[0]
    assert both.verified_requests == 0 and gross_of(both) == 0
    split = F.day_identities(0, sampled(1) + [grade("j0", False, "gpu"), grade("j0", True, "cpu")], chain, {"m": "a"})[0]
    assert split.verified_requests == 6 and gross_of(split) > 0


# -- the service: tokens bound to names, one grade per grader per answer, replay ---------------------------
def test_each_token_is_bound_to_a_name_and_each_grader_is_offered_what_it_has_not_graded(srv, monkeypatch):
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", CPU)
    assert S.grader_tokens() == {S.FIRST_GRADER: GRADER, S.CPU_GRADER: CPU}
    day = _sealed_day(S, base, monkeypatch, work("job1"), work("job2", miner_id="m2"))
    code, d = call(base, f"grading/sample?day={day}", headers=AUTH_CPU)
    assert code == 200 and d["grader"] == "cpu" and sorted(x["job_id"] for x in d["sample"]) == ["job1", "job2"]
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False, "model": "m"}
    # the first grader grades job1; the second is still offered it, the first is not
    assert call(base, "grading/result", res, headers=AUTH) == (200, {"ok": True, "grader": "gpu"})
    assert [x["job_id"] for x in call(base, f"grading/sample?day={day}", headers=AUTH)[1]["sample"]] == ["job2"]
    assert sorted(x["job_id"] for x in call(base, f"grading/sample?day={day}", headers=AUTH_CPU)[1]["sample"]) == ["job1", "job2"]
    # the second grader grades job1 the other way: taken, once; a second grade from the same grader is refused
    code, r = call(base, "grading/result", dict(res, coherent=True), headers=AUTH_CPU)
    assert (code, r) == (200, {"ok": True, "grader": "cpu"})
    assert call(base, "grading/result", dict(res, coherent=True), headers=AUTH_CPU)[0] == 409
    assert call(base, "grading/result", res, headers=AUTH)[0] == 409
    assert [x["job_id"] for x in call(base, f"grading/sample?day={day}", headers=AUTH_CPU)[1]["sample"]] == ["job2"]
    grades = [x for x in S.ST.ev.read(day) if x["type"] == "work_grade"]
    assert [(g["job_id"], g["grader"], g["coherent"]) for g in grades] == [("job1", "gpu", False), ("job1", "cpu", True)]
    # the records carry the names, never a token
    raw = json.dumps(grades)
    assert GRADER not in raw and CPU not in raw
    # the facts layer reads the disagreement as coherent
    assert F.answer_incoherent(F._own_grades(grades)[("job1", "m1")]) is False
    # one token closed: its grader is refused, the other goes on
    monkeypatch.setattr(S, "GRADER_TOKEN", "")
    assert call(base, f"grading/sample?day={day}", headers=AUTH)[0] == 403
    assert call(base, f"grading/sample?day={day}", headers=AUTH_CPU)[0] == 200
    # the second grader's rate limit exemption holds like the first's
    assert S.ST.work_graded == {"job1": {"gpu", "cpu"}}


def test_the_replay_of_the_evidence_log_rebuilds_who_graded_what(srv, monkeypatch, tmp_path):
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", CPU)
    day = _sealed_day(S, base, monkeypatch, work("job1"), work("job2", miner_id="m2"), work("job3", miner_id="m2"))
    # a nameless grade, as the service filed them before the names: the first grader's
    S.ST.ev.append(day, {"type": "work_grade", "job_id": "job1", "miner_id": "m1", "coherent": False, "model": "m"})
    res = {"kind": "work", "job_id": "job2", "miner_id": "m2", "coherent": True, "model": "m"}
    assert call(base, "grading/result", res, headers=AUTH_CPU)[0] == 200
    fresh = S.State(S.ST.data, 1)
    assert fresh.work_graded == {"job1": {"gpu"}, "job2": {"cpu"}}
    assert fresh.work_grades == {day: 2}
    # what the replayed service offers each grader: what it has not graded itself
    S.ST = fresh
    S.ST.height = S.day_bounds(day, 1)[1] + 1
    S.ST.latest_time = S.END - 10 * 86400
    assert sorted(x["job_id"] for x in call(base, f"grading/sample?day={day}", headers=AUTH)[1]["sample"]) == ["job2", "job3"]
    assert sorted(x["job_id"] for x in call(base, f"grading/sample?day={day}", headers=AUTH_CPU)[1]["sample"]) == ["job1", "job3"]
    # the nameless grade is the first grader's: it cannot grade job1 again
    assert call(base, "grading/result", {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": True},
                headers=AUTH)[0] == 409
    # the same records, read by the facts layer as the ranking reads them
    records = [r for d in range(day + 1) for r in S.ST.ev.read(d)]
    assert F._own_grades(records) == {("job1", "m1"): {"gpu": [False]}, ("job2", "m2"): {"cpu": [True]}}


def test_the_status_names_the_ranked_days_with_sampled_answers_and_no_grade(srv, monkeypatch):
    # The alert of a day ranked with no grade: the service's log says it when it ranks (`rank_days`), the status
    # repeats it so the graders' cron relays it (dendra_grade_daily.sh). A ranked day without sampled answers
    # is not in it: there was nothing to grade.
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    day = _sealed_day(S, base, monkeypatch, work("job1"))
    assert call(base, "status")[1]["ungraded_ranked_days"] == []       # not ranked yet
    _rank(S, day)
    assert call(base, "status")[1]["ungraded_ranked_days"] == [day]
    # (a restarted service reads the same from its replay: `work_count` and `work_grades`, benched above)
    # a day with one grade, from either grader, is not in it
    os.remove(os.path.join(S.ST.data, "ranking", f"day-{day:03d}.json"))
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": True, "model": "m"}
    assert call(base, "grading/result", res, headers=AUTH)[0] == 200
    _rank(S, day)
    assert call(base, "status")[1]["ungraded_ranked_days"] == []
    # a ranked day with nothing sampled: not in it either
    _rank(S, day + 1)
    assert call(base, "status")[1]["ungraded_ranked_days"] == []


def test_the_ranking_loop_prints_the_alert_when_it_ranks_a_day_with_no_grade(srv, monkeypatch, capsys):
    from test_final_season_server import _loop
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    day = _sealed_day(S, base, monkeypatch, work("job1"))
    monkeypatch.setattr(S.RK, "rank_day", lambda d, records, *a, **k: {"ranking": [], "total_paid_udndr": 0})
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    S.ST.unwound_from, S.ST.doubled_from = 0, 1
    _loop(S, monkeypatch, S.rank_days)
    out = capsys.readouterr().out
    assert f"[final-season] alert: day {day} ranked with no grade of its 1 sampled answers" in out
    assert S.ST.ungraded_ranked_days() == [day]
    # graded before its ranking: no alert
    os.remove(os.path.join(S.ST.data, "ranking", f"day-{day:03d}.json"))
    S.ST.height = S.day_bounds(day, 1)[1] + 1
    S.ST.finality = None
    res = {"kind": "work", "job_id": "job1", "miner_id": "m1", "coherent": False, "model": "m"}
    assert call(base, "grading/result", res, headers=AUTH)[0] == 200
    _loop(S, monkeypatch, S.rank_days)
    assert "[final-season] alert:" not in capsys.readouterr().out


def test_two_graders_with_one_token_refuse_the_start(srv, monkeypatch, capsys):
    # The same token in both variables would name the first grader for every grade of the second
    # (`Handler._grader` returns the first match): the service says so and refuses to start, before it opens
    # its state or binds its port. Distinct tokens, or one empty, start as before.
    S, _base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", GRADER)
    assert "equal" in S.grader_token_conflict()
    monkeypatch.setattr(S, "START", "1")
    monkeypatch.setattr(S, "State", lambda *a, **k: pytest.fail("the state was opened under a token conflict"))
    assert S.main() == 2
    out = capsys.readouterr().out
    assert "FATAL" in out and "GRADER_CPU_TOKEN are equal" in out
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", CPU)
    assert S.grader_token_conflict() == ""
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", "")
    assert S.grader_token_conflict() == ""
    monkeypatch.setattr(S, "GRADER_TOKEN", "")
    assert S.grader_token_conflict() == ""                 # no token at all is not a conflict (main warns, starts)


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer "}, {"Authorization": "Bearer " + "x" * 40}])
def test_no_token_and_a_token_of_nobody_name_no_grader(srv, monkeypatch, header):
    S, base = srv
    monkeypatch.setattr(S, "GRADER_TOKEN", GRADER)
    monkeypatch.setattr(S, "GRADER_CPU_TOKEN", CPU)
    assert call(base, "grading/sample?day=0", headers=header)[0] == 403
    assert call(base, "grading/result", {"kind": "work", "job_id": "j", "miner_id": "m1", "coherent": True},
                headers=header)[0] == 403
