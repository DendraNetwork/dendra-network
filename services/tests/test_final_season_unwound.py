"""Bench of decision 18 of ADR-047 (owner, 2026-10-08: "pay the work delivered"): the work of a programme job
whose audit was UNWOUND -- no jury concluded it within `audit_unwind_blocks`, the chain refunded its client and
neither paid nor slashed its miner -- is paid by the season as a verified request when its answer reached the
programme AND the season's grading does not say incoherent: ITS OWN answer's grade when it was sampled and
graded (a coherent grade of another answer of the day says nothing of it), else its miner's day by the void rule;
never when an audit concluded against the miner; never when unanswered, ungraded, graded incoherent or
unreadable, each said in the published proof under a name that says what was read. The rule is a new rule SET
with its own fingerprint, applied from one day on -- its finality rule included: a day before it keeps the
rules it was ranked under, the payment checks each day under its own set, and the service fixes that day once,
never on a published day, and never rewrites the record of it.

The rank-level cases (the rule through `final_season_rank.rank_day`, its proof, the command line) are in
tests/test_final_season_rank.py, beside the fake chain they share.
"""
import json
import os
import sys

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# `DENDRA_MODEA_DIR` puts a MUTATED copy of one module first on the path (dendra_saison_vague1_test.sh).
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

import final_season_facts as F  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, compute_day, to_json  # noqa: E402

GEN = "dendra1generator"
DAY = R.RULES["day_blocks"]
END0 = R.day_bounds(0, 1)[1]
UNWOUND = "open+paid+optimistic+disputed+resolved+unwound"     # a drawn jury stayed below quorum
UNWOUND_NO_JURY = "open+paid+optimistic+resolved+unwound"      # the jury pool too small, or no seed
# The fingerprint the service PUBLISHED before decision 18, read on its status on 2026-10-08
# (https://testnet-api.dendranetwork.com/final-season/v1/status). The days ranked before decision 18 carry it.
PUBLISHED_BEFORE_18 = "c591851fb5dfd170fb4b9e1dd09d4a6027bb0bd60ea73b3730a39f25b6aeacd5"


def J(state, jid="u1", mid="m1", slashed=False, client=GEN):
    return {"id": jid, "state": state, "miner_id": mid, "client": client, "slashed_primary": slashed}


def rec(t, mid, day=0, **kw):
    return dict(kw, type=t, miner_id=mid, day=day)


def seal(day=0, **answered):
    return {"type": "work_sealed", "day": day, "answers": sum(len(v) for v in answered.values()), "drawn": 0,
            "answered": {m: list(j) for m, j in answered.items()}}


def graded(mid, grades, sampled=None, day=0):
    """`sampled` answers of `mid` on `day` in the evidence, the first len(grades) of them graded. These are OTHER
    answers of the day (`s{mid}{k}`), never the unwound job's: `own` grades that one."""
    n = len(grades) if sampled is None else sampled
    out = [rec("work_answer", mid, day=day, job_id=f"s{mid}{k}", prompt="p", answer="a") for k in range(n)]
    return out + [rec("work_grade", mid, day=day, job_id=f"s{mid}{k}", coherent=c, model="x")
                  for k, c in enumerate(grades)]


def own(coherent, jid="u1", mid="m1", day=0):
    """The unwound job's OWN answer, sampled into the evidence and graded `coherent` (None: sampled, not graded)."""
    out = [rec("work_answer", mid, day=day, job_id=jid, prompt="p", answer="a")]
    if coherent is not None:
        out.append(rec("work_grade", mid, day=day, job_id=jid, coherent=coherent, model="x"))
    return out


def one(state=UNWOUND, records=(), answered=("u1",), **kw):
    """`unwound_work` of one unwound job u1 of m1, settled on day 0."""
    unwound = {"u1": {"miner_id": kw.get("mid", "m1"), "state": F.job_unwound(J(state, **kw))}}
    return F.unwound_work(0, list(records), unwound, set(answered))


# ── the rule sets ──────────────────────────────────────────────────────────────────────────────────────────
def test_the_rules_before_decision_18_are_the_ones_published_and_kept_unedited():
    assert R.fingerprint(R.RULES_17) == PUBLISHED_BEFORE_18
    # decision 18 is TWO keys more and nothing else -- the rule, and when a day is final: no rate, cap, volume or
    # end moves
    assert {k: v for k, v in R.RULES.items() if k not in ("unwound_audit_work", "finality")} == R.RULES_17
    assert R.RULES["unwound_audit_work"] == R.UNWOUND_AUDIT_WORK and R.RULES["finality"] == R.FINALITY
    assert R.fingerprint(R.RULES) != PUBLISHED_BEFORE_18 and R.fingerprint() == R.fingerprint(R.RULES)
    assert R.RULE_HISTORY == (R.RULES_17, R.RULES)


def test_the_finality_rule_is_the_sets_and_an_unknown_one_is_applied_neither_way():
    # RULES_17 name no finality: they are final at the unwind bound plus 200, as the service published their days
    assert R.finality_rule(R.RULES_17) == R.FINALITY_17
    assert R.finality_rule(R.RULES) == R.FINALITY
    with pytest.raises(ValueError, match="unknown finality"):
        R.finality_rule(dict(R.RULES, finality="audit_unwind_blocks"))
    # a set without the key that is NOT RULES_17 is no published set: refused, never given the older rule
    with pytest.raises(ValueError, match="names no finality"):
        R.finality_rule(dict(R.RULES_17, work_per_request=1))
    with pytest.raises(ValueError):
        R.finality_rule({k: v for k, v in R.RULES.items() if k != "finality"})


def test_a_day_is_ranked_under_the_rules_in_force_that_day():
    assert R.rules_for_day(0, 0) is R.RULES
    assert R.rules_for_day(0, 1) is R.RULES_17 and R.rules_for_day(1, 1) is R.RULES
    assert R.rules_for_day(30, 3) is R.RULES and R.rules_for_day(2, 3) is R.RULES_17
    # None: decision 18 applies to no day (a service that predates it), so every day keeps the earlier rules
    assert R.rules_for_day(7, None) is R.RULES_17
    for bad in (-1, "1", 1.0, True):
        with pytest.raises(ValueError):
            R.rules_for_day(3, bad)
    with pytest.raises(ValueError):
        R.rules_for_day(-1, 0)


def test_which_set_pays_unwound_work_is_read_from_the_set_and_an_unknown_rule_is_applied_neither_way():
    assert R.pays_unwound_work(R.RULES) is True and R.pays_unwound_work(R.RULES_17) is False
    with pytest.raises(ValueError):
        R.pays_unwound_work(dict(R.RULES, unwound_audit_work="paid_always"))
    assert R.rule_set_of(R.fingerprint(R.RULES_17)) == (0, R.RULES_17)
    assert R.rule_set_of(R.fingerprint(R.RULES)) == (1, R.RULES)
    assert R.rule_set_of("0" * 64) == (None, None)


def test_a_ranking_carries_the_fingerprint_of_the_set_it_was_computed_with():
    ids = [Identity("m1", verified_requests=1, payout_address="dendra1a")]
    assert to_json(compute_day(0, ids, 0, R.RULES_17))["rules_fingerprint"] == PUBLISHED_BEFORE_18
    assert to_json(compute_day(0, ids, 0, R.RULES))["rules_fingerprint"] == R.fingerprint(R.RULES)
    # same rates: the same figures under either set, only the fingerprint differs
    a, b = to_json(compute_day(0, ids, 0, R.RULES_17)), to_json(compute_day(0, ids, 0, R.RULES))
    a.pop("rules_fingerprint"), b.pop("rules_fingerprint")
    assert a == b


# ── what the chain says of an unwound job ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("job,out", [
    (J("open+paid+optimistic"), None),                                          # not audited: the other rules
    (J("open+paid+optimistic+disputed"), None),                                 # audit still open
    (J("open+paid+optimistic+disputed+resolved+vindicated+quorum"), None),      # concluded: the other rules
    (J("open+paid+optimistic+disputed+resolved+clawed+quorum"), None),          # convicted: never work at all
    (J(UNWOUND), ""),
    (J(UNWOUND_NO_JURY), ""),
    (J(UNWOUND, slashed=True), F.CONVICTED),
    (J("open+paid+optimistic+disputed+resolved+clawed+unwound"), F.CONVICTED),
    (J("open+resolved+unwound"), F.UNEXPECTED_STATE),                           # never paid on chain
    (J("open+paid+optimistic+unwound"), F.UNEXPECTED_STATE),                    # not resolved
    (J("open+paid+optimistic+disputed+resolved+vindicated+quorum+unwound"), F.UNEXPECTED_STATE),
    (J("open+paid+expired+refunded+resolved+unwound"), F.UNEXPECTED_STATE),
    (J(UNWOUND, mid=""), F.NO_PRIMARY),
    (J("open+paid+optimistic+resolved+unwoundx"), None),                       # a marker, never a substring
])
def test_job_unwound(job, out):
    assert F.job_unwound(job) == out
    # and never counted twice: a job decision 18 can pay is never ordinary work
    if out == "":
        assert F.job_is_final_positive(job) is False


def test_the_slash_of_an_unwound_job_is_read_never_defaulted():
    job = J(UNWOUND)
    del job["slashed_primary"]
    with pytest.raises(KeyError):
        F.job_unwound(job)
    for v in (None, 0, "false"):
        with pytest.raises(ValueError):
            F.job_unwound(J(UNWOUND, slashed=v))


def test_the_unwound_jobs_of_a_day_are_the_generators_settled_in_its_window():
    jobs = [J(UNWOUND, "u1"), J(UNWOUND, "u2", client="dendra1someone"), J(UNWOUND, "u3"),
            J("open+paid+optimistic", "g1"), J(UNWOUND_NO_JURY, "u4", mid="m2"), J(UNWOUND, "u5")]
    heights = {"u1": 10, "u2": 11, "u3": DAY + 5, "g1": 12, "u4": END0}            # u5 never settled
    out = F.unwound_programme_jobs(0, 1, jobs, heights, GEN, 100, 10 * DAY, last_height=END0)
    assert out == {"u1": {"miner_id": "m1", "state": ""}, "u4": {"miner_id": "m2", "state": ""}}
    # the window and the finality of chain_day_facts, refused the same way
    with pytest.raises(ValueError, match="not final"):
        F.unwound_programme_jobs(0, 1, jobs, heights, GEN, 17_280, END0 + 100, last_height=END0)
    with pytest.raises(ValueError):
        F.unwound_programme_jobs(0, 1, jobs, heights, GEN, 100, 10 * DAY, last_height=END0 + 1)
    with pytest.raises(TypeError):
        F.unwound_programme_jobs(0, 1, jobs, heights, GEN, 100, 10 * DAY)


# ── the rule: answered AND not graded incoherent -- its own answer first, else its miner's day ──────────────
def test_an_unwound_job_answered_and_graded_coherent_is_paid():
    paid, proof = one(records=graded("m1", [True]))
    assert paid == {"m1": 1}
    assert proof == {"u1": {"miner_id": "m1", "answered": True, "graded": "day_coherent", "paid": True}}
    # the same without `+disputed` (a jury pool too small, or no seed): the chain says unwound all the same
    assert one(UNWOUND_NO_JURY, records=graded("m1", [True]))[0] == {"m1": 1}
    # its OWN answer sampled and graded coherent: paid, and the proof says it was its own answer
    paid, proof = one(records=own(True))
    assert paid == {"m1": 1} and proof["u1"]["graded"] == "own_answer_coherent"


def test_an_unwound_job_whose_own_answer_is_graded_incoherent_is_not_paid_whatever_its_day_says():
    # The relecture's P1: u1's own answer graded incoherent, another answer of the day graded coherent. The day
    # keeps its ordinary work (one coherent grade), but the grade OF THIS ANSWER says no: never paid, and the
    # published proof never calls it coherent.
    paid, proof = one(records=own(False) + graded("m1", [True]))
    assert paid == {}
    assert proof["u1"] == {"miner_id": "m1", "answered": True, "graded": "own_answer_incoherent", "paid": False,
                           "why": "own_answer_incoherent"}
    # P3: the day's only grade is u1's own, incoherent, with three answers sampled -- below the void count, so
    # the day keeps its work; this answer is not paid all the same
    recs = own(False) + graded("m1", [], sampled=2)
    assert F._graded_out([False], 3) is False
    assert one(records=recs)[1]["u1"]["why"] == "own_answer_incoherent"
    # the converse: its own answer graded coherent on a day whose other grade says incoherent -- paid
    assert one(records=own(True) + graded("m1", [False]))[0] == {"m1": 1}
    # two grades of its own answer (the service grades once; a log that says twice is read strictly): one
    # incoherent is enough to say no
    twice = own(True) + [rec("work_grade", "m1", job_id="u1", coherent=False, model="x")]
    assert one(records=twice)[1]["u1"]["why"] == "own_answer_incoherent"


def test_an_unwound_job_whose_answer_never_reached_the_programme_is_not_paid():
    paid, proof = one(records=graded("m1", [True]), answered=())
    assert paid == {}
    assert proof["u1"] == {"miner_id": "m1", "answered": False, "graded": "day_coherent", "paid": False,
                           "why": "not_answered"}


def test_an_unwound_job_of_a_day_graded_incoherent_is_not_paid():
    # the rule that takes a day's work away (`_graded_out`): two incoherent grades of two or more sampled
    paid, proof = one(records=graded("m1", [False, False], sampled=3))
    assert paid == {} and proof["u1"]["graded"] == "day_graded_out" and proof["u1"]["why"] == "day_graded_out"
    # one sampled answer, graded incoherent: min(2, 1) = 1 grade is enough to void
    assert one(records=graded("m1", [False]))[1]["u1"]["why"] == "day_graded_out"


def test_an_unwound_job_of_a_day_never_graded_is_not_paid():
    # No grade never voids ordinary work (the void rule); it never pays the work no jury judged either.
    paid, proof = one(records=graded("m1", [], sampled=3))
    assert paid == {} and proof["u1"]["graded"] == "not_graded" and proof["u1"]["why"] == "not_graded"
    assert one(records=())[1]["u1"]["why"] == "not_graded"
    # its own answer sampled but never graded, and no other grade: not graded either
    assert one(records=own(None))[1]["u1"]["why"] == "not_graded"


def test_not_graded_itself_the_criterion_is_the_void_rule_and_the_proof_says_no_more_than_was_read():
    # The relecture's P2: three answers sampled, ONE grade, incoherent, of ANOTHER answer. The void rule keeps
    # the day's work (it needs two), so by the same criterion the unwound job is paid -- the rule borrows the
    # void rule, it does not write a second one. But nothing in that day was judged coherent, and the proof
    # says exactly what was read: "day_not_graded_out", never "coherent".
    assert F._graded_out([False], 3) is False
    paid, proof = one(records=graded("m1", [False], sampled=3))
    assert paid == {"m1": 1}
    assert proof["u1"]["graded"] == "day_not_graded_out" and "coherent" not in proof["u1"]["graded"].split("_")
    # one coherent grade of the day, the job itself not graded: the day says coherent
    assert one(records=graded("m1", [False, True], sampled=3))[1]["u1"]["graded"] == "day_coherent"


def test_a_convicted_job_is_never_paid_answered_and_coherent_as_it_is():
    for state, slashed in (("open+paid+optimistic+disputed+resolved+clawed+unwound", False), (UNWOUND, True)):
        paid, proof = one(state, records=graded("m1", [True]) + own(True), slashed=slashed)
        assert paid == {} and proof["u1"]["why"] == "convicted" and proof["u1"]["paid"] is False


def test_an_unexpected_state_or_no_primary_is_not_paid():
    assert one("open+paid+optimistic+unwound", records=graded("m1", [True]))[1]["u1"]["why"] == "unexpected_state"
    unwound = {"u1": {"miner_id": "", "state": F.job_unwound(J(UNWOUND, mid=""))}}
    paid, proof = F.unwound_work(0, graded("", [True]), unwound, {"u1"})
    assert paid == {} and proof["u1"]["why"] == "no_primary"


def test_the_grades_are_the_miners_own_and_of_the_day_the_job_counts_on():
    # m2's coherent grade says nothing of m1; nor does m1's coherent grade of day 1 for a job of day 0.
    assert one(records=graded("m2", [True]))[1]["u1"]["why"] == "not_graded"
    assert one(records=graded("m1", [True], day=1))[1]["u1"]["why"] == "not_graded"
    # a grade of u1 filed under ANOTHER identity is not its miner's own answer: it decides nothing of m1's job
    assert one(records=own(False, mid="m2") + graded("m1", [True]))[1]["u1"]["graded"] == "day_coherent"
    # its own grade is read on whatever day it was filed: answered and sealed on day 0, settled on day 1, where
    # the day's other grade says coherent
    unwound = {"u1": {"miner_id": "m1", "state": ""}}
    paid, proof = F.unwound_work(1, own(False, day=0) + graded("m1", [True], day=1), unwound, {"u1"})
    assert paid == {} and proof["u1"]["why"] == "own_answer_incoherent"


def test_an_unreadable_evidence_line_is_unknown_never_a_no_and_never_a_yes():
    torn = {"type": "_unreadable_lines", "count": 1, "day": 0}
    # not in a seal, but a line could not be read: the seal may be there -- unknown, not "not answered"
    paid, proof = one(records=graded("m1", [True]) + [torn], answered=())
    assert paid == {} and proof["u1"]["answered"] is None and proof["u1"]["why"] == "answer_unreadable"
    # a torn line on ANOTHER day hides a seal as well (an answer is sealed on the day it arrives)
    assert one(records=graded("m1", [True]) + [dict(torn, day=3)], answered=())[1]["u1"]["answered"] is None
    # no coherent grade and a torn line that day: the hidden line may be the grade -- unreadable
    for grades in ([], [False], [False, False]):
        p = one(records=graded("m1", grades, sampled=3) + [torn])[1]["u1"]
        assert p["graded"] == "grading_unreadable" and p["paid"] is False, grades
    # ITS OWN GRADE FIRST, so a coherent grade of the day no longer settles it: the torn line of the day its
    # answer was sealed may be ITS incoherent grade -- unreadable, not paid
    recs = [seal(0, m1=["u1"])] + graded("m1", [False, True]) + [torn]
    assert one(records=recs)[1]["u1"]["graded"] == "grading_unreadable"
    # its seal not found in the records: any torn line may be the day of its grade
    assert one(records=graded("m1", [True]) + [dict(torn, day=5)])[1]["u1"]["graded"] == "grading_unreadable"
    # its seal on day 0, the torn line on day 2: its own grade is not hidden there, the day's coherent grade pays
    recs = [seal(0, m1=["u1"])] + graded("m1", [False, True]) + [dict(torn, day=2)]
    assert one(records=recs)[0] == {"m1": 1}
    # its own grade read: it decides, whatever the hidden line holds (the service grades an answer once)
    assert one(records=own(True) + [torn])[0] == {"m1": 1}
    assert one(records=own(False) + graded("m1", [True]) + [torn])[1]["u1"]["why"] == "own_answer_incoherent"
    # a torn line of ANOTHER day says nothing of this day's grading
    recs = [seal(0, m1=["u1"])] + graded("m1", [False, False]) + [dict(torn, day=2)]
    assert one(records=recs)[1]["u1"]["graded"] == "day_graded_out"
    # sealed on day 0, counted on day 1: a torn line of the COUNTED day may hide a grade the void rule reads --
    # unreadable while no grade of that day says coherent
    unwound = {"u1": {"miner_id": "m1", "state": ""}}
    recs = [seal(0, m1=["u1"])] + graded("m1", [False], sampled=3, day=1) + [dict(torn, day=1)]
    assert F.unwound_work(1, recs, unwound, {"u1"})[1]["u1"]["graded"] == "grading_unreadable"
    assert F.unwound_work(1, recs[:-1], unwound, {"u1"})[1]["u1"]["graded"] == "day_not_graded_out"


def test_every_unwound_job_is_in_the_proof_paid_or_not_and_counted_per_miner():
    unwound = {"a": {"miner_id": "m1", "state": ""}, "b": {"miner_id": "m1", "state": ""},
               "c": {"miner_id": "m2", "state": ""}, "d": {"miner_id": "m3", "state": "convicted"}}
    recs = graded("m1", [True]) + graded("m2", [False, False], sampled=2) + graded("m3", [True]) + own(False, "b")
    paid, proof = F.unwound_work(0, recs, unwound, {"a", "b", "c", "d"})
    assert paid == {"m1": 1}
    assert sorted(proof) == ["a", "b", "c", "d"]
    assert [proof[j]["paid"] for j in "abcd"] == [True, False, False, False]
    assert [proof[j]["graded"] for j in "abcd"] == ["day_coherent", "own_answer_incoherent", "day_graded_out",
                                                     "day_coherent"]
    # read once for the day, and the same answer per job as asked one by one
    assert all(F.unwound_grading(j, unwound[j]["miner_id"], 0, recs) == proof[j]["graded"] for j in "abcd")


def test_the_day_identities_are_unchanged_by_the_refactor_of_the_grades():
    # `day_identities` reads its grades through the same `_work_grades` the rule reads: the void rule as before
    recs = graded("m", [False], sampled=1)
    chain = {"work": {"m": 4}, "verdicts": {}, "presence": {}}
    assert F.day_identities(0, recs, chain, {"m": "a"})[0].verified_requests == 0
    assert F.day_identities(1, recs, chain, {"m": "a"})[0].verified_requests == 4
    assert F.day_identities(0, graded("m", [False], sampled=3), chain, {"m": "a"})[0].verified_requests == 4


# ── the payment: each day under its own set ───────────────────────────────────────────────────────────────
def _publish(tmp_path, sets):
    """Day d ranked under `sets[d]`: m1 one verified request, m2 one, as `final_season_rank` publishes them."""
    from modea import cosmos_addr
    a = cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([1] * 20), 8, 5))
    b = cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([2] * 20), 8, 5))
    paid = 0
    for d, rules in enumerate(sets):
        r = compute_day(d, [Identity("m1", verified_requests=2, payout_address=a),
                            Identity("m2", verified_requests=1, payout_address=b)], paid, rules)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": (d + 1) * 17_280, "season_end_height": None}
        (tmp_path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def test_a_week_ranked_partly_before_decision_18_is_paid_each_day_under_its_own_rules(tmp_path):
    import final_season_payout as P
    _publish(tmp_path, [R.RULES_17, R.RULES_17] + [R.RULES] * 5)
    payable, aside = P.plan(0, str(tmp_path))
    assert sum(payable.values()) == 7 * 3 * R.RULES["work_per_request"] and not aside
    _publish(tmp_path, [R.RULES] * 7)
    assert P.plan(0, str(tmp_path))[0] == payable


def test_a_day_under_the_earlier_rules_after_a_day_under_decision_18_is_refused(tmp_path):
    import final_season_payout as P
    _publish(tmp_path, [R.RULES_17, R.RULES, R.RULES, R.RULES_17, R.RULES, R.RULES, R.RULES])
    with pytest.raises(SystemExit) as e:
        P.plan(0, str(tmp_path))
    assert "day 3" in str(e.value) and "earlier than the rules of a day before it" in str(e.value)


def test_each_day_is_checked_against_the_set_its_fingerprint_names(tmp_path, monkeypatch):
    # A set whose work rate differs: its days are checked against ITS rate, never the current one.
    import final_season_payout as P
    other = dict(R.RULES, work_per_request=2 * R.RULES["work_per_request"])
    monkeypatch.setattr(R, "RULE_HISTORY", (R.RULES_17, R.RULES, other))
    _publish(tmp_path, [R.RULES_17, R.RULES] + [other] * 5)
    assert P.check_days([P.load_day(str(tmp_path), d) for d in range(7)]) == ""
    # the same figures under a set that is not the season's: refused, never checked against the current one
    monkeypatch.setattr(R, "RULE_HISTORY", (R.RULES_17, R.RULES))
    bad = P.check_days([P.load_day(str(tmp_path), d) for d in range(7)])
    assert bad.startswith("day 2: ranked under rules") and "none of the season's" in bad


# ── the service fixes the first day once, never on a published day ─────────────────────────────────────────
def _service():
    import final_season_server as S
    return S


def _ranked(data, d, rules):
    os.makedirs(os.path.join(data, "ranking"), exist_ok=True)
    with open(os.path.join(data, "ranking", f"day-{d:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"day": d, "rules_fingerprint": R.fingerprint(rules), "total_paid_udndr": 0}, f)


def _file(data):
    with open(os.path.join(data, "decision-18.json"), encoding="utf-8") as f:
        return json.load(f)


def test_the_first_day_is_the_first_day_not_ranked_when_the_service_first_starts(tmp_path):
    S = _service()
    data = str(tmp_path)
    assert S.State(data, 1, "").unwound_from == 0                        # nothing ranked yet: from day 0
    assert _file(data)["from_day"] == 0 and _file(data)["set_by"] == "first_unranked_day"
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    _ranked(data2, 0, R.RULES_17)
    _ranked(data2, 1, R.RULES_17)
    assert S.State(data2, 1, "").unwound_from == 2                       # days 0 and 1 published: from day 2
    doc = _file(data2)
    assert doc == {"from_day": 2, "set_by": "first_unranked_day", "first_unranked_day": 2,
                   "rule": R.UNWOUND_AUDIT_WORK, "rules_fingerprint": R.fingerprint(R.RULES),
                   "rules_fingerprint_before": PUBLISHED_BEFORE_18}


def test_the_first_day_is_kept_across_restarts_and_never_moves(tmp_path):
    S = _service()
    data = str(tmp_path)
    _ranked(data, 0, R.RULES_17)
    assert S.State(data, 1, "").unwound_from == 1
    _ranked(data, 1, R.RULES)                                             # day 1 published under decision 18
    _ranked(data, 2, R.RULES)
    # a restart later: the first day not ranked is now 3, and the day stays 1
    assert S.State(data, 1, "").unwound_from == 1
    assert S.State(data, 1, "1").unwound_from == 1                        # the same day named: accepted
    with pytest.raises(SystemExit, match="names another day"):
        S.State(data, 1, "3")


def test_the_record_of_the_first_day_is_read_at_a_restart_never_rewritten(tmp_path, monkeypatch, capsys):
    # The relecture: the record read back was written again at every start, so a data directory that can no
    # longer be written stopped a restart (FATAL), and the record's bytes were never "the ones of the first start".
    S = _service()
    data = str(tmp_path)
    _ranked(data, 0, R.RULES_17)
    assert S.State(data, 1, "").unwound_from == 1
    path = os.path.join(data, "decision-18.json")
    with open(path, "rb") as f:
        first = f.read()
    assert "applies from day 1" in capsys.readouterr().out
    os.utime(path, (1_000_000_000, 1_000_000_000))

    def refused(*a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(S.os, "replace", refused)
    _ranked(data, 1, R.RULES)
    assert S.State(data, 1, "").unwound_from == 1                         # no write attempted, no FATAL
    assert S.State(data, 1, "1").unwound_from == 1
    with open(path, "rb") as f:
        assert f.read() == first
    assert os.stat(path).st_mtime == 1_000_000_000
    assert not os.path.exists(path + ".tmp")
    assert "applies from day" not in capsys.readouterr().out             # said once, at the start that fixed it
    # and the FIRST start still refuses to run when it cannot keep the day it fixed
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    with pytest.raises(SystemExit, match="cannot be written"):
        S.State(data2, 1, "")


def test_an_explicit_day_is_taken_and_a_day_already_ranked_is_refused(tmp_path):
    S = _service()
    data = str(tmp_path)
    _ranked(data, 0, R.RULES_17)
    _ranked(data, 1, R.RULES_17)
    for bad in ("0", "1"):
        with pytest.raises(SystemExit, match="already ranked"):
            S.State(data, 1, bad)
        assert not os.path.exists(os.path.join(data, "decision-18.json"))
    for bad in ("x", "-1", "1.0", " 2"):
        with pytest.raises(SystemExit, match="not a day number"):
            S.State(data, 1, bad)
    st = S.State(data, 1, "5")
    assert st.unwound_from == 5 and _file(data)["set_by"] == "parameter"


def test_a_published_day_under_other_rules_than_the_ones_in_force_that_day_stops_the_service(tmp_path):
    S = _service()
    data = str(tmp_path)
    _ranked(data, 0, R.RULES_17)
    assert S.State(data, 1, "").unwound_from == 1
    _ranked(data, 1, R.RULES_17)                                          # day 1 ranked WITHOUT decision 18
    with pytest.raises(SystemExit, match="day 1 is published under rules"):
        S.State(data, 1, "")
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    _ranked(data2, 0, R.RULES)                                            # a day ranked under 18 before it applied
    with pytest.raises(SystemExit, match="day 0 is published under rules"):
        S.State(data2, 1, "1")
    assert not os.path.exists(os.path.join(data2, "decision-18.json"))   # checked before anything is written


def test_an_unreadable_or_foreign_record_of_the_first_day_stops_the_service(tmp_path):
    S = _service()
    data = str(tmp_path)
    S.State(data, 1, "")
    path = os.path.join(data, "decision-18.json")
    good = _file(data)
    for bad in ("{", "[]", json.dumps(dict(good, from_day=None)), json.dumps(dict(good, from_day=-1)),
                json.dumps(dict(good, from_day=True)), json.dumps(dict(good, rules_fingerprint="0" * 64)),
                json.dumps({k: v for k, v in good.items() if k != "from_day"})):
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        with pytest.raises(SystemExit):
            S.State(data, 1, "")


def test_a_gap_in_the_ranked_days_stops_the_service(tmp_path):
    S = _service()
    data = str(tmp_path)
    _ranked(data, 0, R.RULES_17)
    _ranked(data, 2, R.RULES_17)
    with pytest.raises(SystemExit, match="not 0..N"):
        S.State(data, 1, "")
