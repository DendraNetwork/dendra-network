"""Bench of the facts layer: what counts as a verified request, a consistent verdict and a presence, and
what the evidence log can do to them: take a day of work away (its graded answers all incoherent) and
set the payout address (the declaration, else the operator)."""
import os
import sys
from dataclasses import fields

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, MODEA or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from final_season_calc import Identity, gross_of  # noqa: E402
from final_season_facts import (GRADES_TO_VOID, answered_jobs, audit_outcome, chain_day_facts,  # noqa: E402
                           day_identities, job_is_final_positive)
from final_season_rules import RULES, day_bounds  # noqa: E402

GEN = "dendra1generator"
DAY = RULES["day_blocks"]
# The full end of day 0 from start height 1: the `last_height` of a day wholly before the season's end.
END0 = day_bounds(0, 1)[1]


def J(state, slashed=False, **kw):
    return dict(kw, state=state, slashed_primary=slashed)


@pytest.mark.parametrize("job,ok", [
    (J("open+paid+optimistic"), True),
    (J("open+paid+optimistic+disputed+resolved+vindicated+quorum"), True),
    (J("open+paid+optimistic+disputed+resolved"), True),                          # silent redo, cleared
    (J("open+paid+optimistic+disputed+resolved+adjudicated"), True),              # adjudicated, cleared
    (J("open+paid+optimistic+disputed+resolved+adjudicated", slashed=True), False),   # adjudicated, convicted
    (J("open+paid+optimistic+disputed+resolved+clawed+quorum"), False),
    (J("open+paid+optimistic+resolved+unwound"), False),
    (J("open+paid+optimistic+disputed"), False),                                  # audit still open
    (J("open+expired+refunded"), False),
    (J("open"), False),
    (J(""), False),
])
def test_final_positive(job, ok):
    assert job_is_final_positive(job) is ok


@pytest.mark.parametrize("job,out", [
    (J("open+paid+optimistic"), None),
    (J("open+paid+optimistic+disputed"), None),
    (J("open+paid+optimistic+disputed+resolved+clawed+quorum"), "0"),
    (J("open+paid+optimistic+disputed+resolved+vindicated+quorum"), "1"),
    (J("open+paid+optimistic+disputed+resolved+adjudicated", slashed=True), "0"),
    (J("open+paid+optimistic+disputed+resolved+unwound"), None),
])
def test_audit_outcome(job, out):
    assert audit_outcome(job) == out


def test_work_counts_only_generator_jobs_settled_that_day():
    jobs = [J("open+paid+optimistic", id="a", miner_id="m1", client=GEN),
            J("open+paid+optimistic", id="b", miner_id="m1", client="someone"),
            J("open+paid+optimistic", id="c", miner_id="m2", client=GEN),
            J("open+paid+optimistic+resolved+unwound", id="d", miner_id="m2", client=GEN),
            J("open+paid+optimistic", id="e", miner_id="m3", client=GEN)]           # never settled
    heights = {"a": 10, "b": 11, "c": DAY + 5, "d": 12}
    f = chain_day_facts(0, 1, jobs, heights, {}, GEN, finality_height=100, current_height=10 * DAY,
                        answered={"a", "b", "c", "d", "e"}, last_height=END0)
    assert f["work"] == {"m1": 1}


def test_a_programme_job_whose_answer_never_reached_the_programme_is_not_work():
    # Paid and settled on the chain, but the generator never received its answer: a miner can anchor a
    # commit and anyone can settle, so the chain alone would pay a request nobody answered.
    jobs = [J("open+paid+optimistic", id="a", miner_id="m1", client=GEN),
            J("open+paid+optimistic", id="b", miner_id="m1", client=GEN)]
    heights = {"a": 10, "b": 11}
    f = chain_day_facts(0, 1, jobs, heights, {}, GEN, 100, 10 * DAY, answered={"a"}, last_height=END0)
    assert f["work"] == {"m1": 1}
    assert chain_day_facts(0, 1, jobs, heights, {}, GEN, 100, 10 * DAY, answered=set(),
                           last_height=END0)["work"] == {}


def test_the_answered_jobs_are_read_from_the_day_seals_only():
    records = [{"type": "work_sealed", "day": 0, "answers": 2, "drawn": 1, "answered": {"m1": ["a", "b"]}},
               {"type": "work_sealed", "day": 1, "answers": 1, "drawn": 1, "answered": {"m2": ["c"]}},
               {"type": "work_sealed", "day": 2, "answers": 0, "drawn": 0},          # an older seal: none
               rec("work_answer", "m3", job_id="d", prompt="p", answer="a"),          # not a seal
               {"type": "sealed", "answered": {"m4": ["e"]}}]                        # another type
    assert answered_jobs(records) == {"a", "b", "c"}


def test_the_bounds_of_a_day_are_inclusive():
    first, last = day_bounds(1, 1)
    names = ("before", "first", "last", "after")
    jobs = [J("open+paid+optimistic", id=n, miner_id=n, client=GEN) for n in names]
    heights = dict(zip(names, (first - 1, first, last, last + 1)))
    f = chain_day_facts(1, 1, jobs, heights, {}, GEN, finality_height=100, current_height=last + 100,
                        answered=set(names), last_height=last)
    assert f["work"] == {"first": 1, "last": 1}


def test_a_day_is_refused_before_it_is_final():
    _, last = day_bounds(0, 1)
    with pytest.raises(ValueError):
        chain_day_facts(0, 1, [], {}, {}, GEN, finality_height=17_280, current_height=DAY + 100,
                        answered=set(), last_height=last)
    with pytest.raises(ValueError):
        chain_day_facts(0, 1, [], {}, {}, GEN, finality_height=17_280, current_height=last + 17_279,
                        answered=set(), last_height=last)
    assert chain_day_facts(0, 1, [], {}, {}, GEN, finality_height=17_280, current_height=last + 17_280,
                           answered=set(), last_height=last) == {"work": {}, "verdicts": {}}


def test_the_last_counted_block_is_required_and_has_no_default():
    # A default would be the full day: on the day the season ends, settlements after its end would be paid.
    with pytest.raises(TypeError):
        chain_day_facts(0, 1, [], {}, {}, GEN, 100, 10 * DAY, set())


def test_a_last_counted_block_outside_the_day_is_refused():
    first, nominal_last = day_bounds(1, 1)
    for bad in (first - 1, nominal_last + 1, 0, -1, nominal_last + DAY):
        with pytest.raises(ValueError):
            chain_day_facts(1, 1, [], {}, {}, GEN, 100, 10 * DAY, set(), last_height=bad)
    # both ends of the day are valid: the season may end on the day's first block, or after its last
    for ok in (first, first + 1, nominal_last - 1, nominal_last):
        assert chain_day_facts(1, 1, [], {}, {}, GEN, 100, 10 * DAY, set(),
                               last_height=ok) == {"work": {}, "verdicts": {}}


def test_a_settlement_after_the_last_counted_block_is_not_counted():
    # The day the season ends: its last counted block is the season's last, before the day's full end.
    first, nominal_last = day_bounds(1, 1)
    cut = first + 100
    names = ("first", "cut", "after_cut", "day_end")
    jobs = [J("open+paid+optimistic", id=n, miner_id=n, client=GEN) for n in names]
    heights = dict(zip(names, (first, cut, cut + 1, nominal_last)))
    f = chain_day_facts(1, 1, jobs, heights, {}, GEN, 100, 10 * DAY, set(names), last_height=cut)
    assert f["work"] == {"first": 1, "cut": 1}
    # the same jobs with the full day: the two late settlements count, so the cut is what removed them
    full = chain_day_facts(1, 1, jobs, heights, {}, GEN, 100, 10 * DAY, set(names), last_height=nominal_last)
    assert full["work"] == {n: 1 for n in names}
    # a verdict on an audit settled after the season's end earns nothing either
    audits = [J("open+paid+optimistic+disputed+resolved+clawed+quorum", id="in", miner_id="p", client=GEN),
              J("open+paid+optimistic+disputed+resolved+clawed+quorum", id="out", miner_id="p", client=GEN)]
    votes = {"in": {"j1": "0"}, "out": {"j1": "0", "j2": "0"}}
    g = chain_day_facts(1, 1, audits, {"in": cut, "out": cut + 1}, votes, GEN, 100, 10 * DAY,
                        {"in", "out"}, last_height=cut)
    assert g["verdicts"] == {"j1": 1}


def test_finality_is_measured_from_the_last_counted_block():
    # A cut day is final `finality_height` blocks after its LAST COUNTED block, not after its full end:
    # the season's last day must not wait for blocks that are not in it.
    first, nominal_last = day_bounds(1, 1)
    cut = first + 100
    fin = 17_280
    jobs = [J("open+paid+optimistic", id="a", miner_id="m", client=GEN)]
    with pytest.raises(ValueError):
        chain_day_facts(1, 1, jobs, {"a": cut}, {}, GEN, fin, cut + fin - 1, {"a"}, last_height=cut)
    f = chain_day_facts(1, 1, jobs, {"a": cut}, {}, GEN, fin, cut + fin, {"a"}, last_height=cut)
    assert f["work"] == {"m": 1}
    assert cut + fin < nominal_last + fin            # earlier than the full day's finality would allow
    # and the full day still waits for its full end
    with pytest.raises(ValueError):
        chain_day_facts(1, 1, jobs, {"a": cut}, {}, GEN, fin, cut + fin, {"a"}, last_height=nominal_last)


def test_consistent_verdicts_count_on_programme_jobs_only():
    # The juror reward has the work reward's restriction: a matching verdict on someone else's job (x)
    # earns nothing, and neither does a verdict on an audit settled on another day (w).
    jobs = [J("open+paid+optimistic+disputed+resolved+clawed+quorum", id="x", miner_id="p", client="anyone"),
            J("open+paid+optimistic+disputed+resolved+vindicated+quorum", id="y", miner_id="p", client=GEN),
            J("open+paid+optimistic+resolved+unwound", id="z", miner_id="p", client=GEN),
            J("open+paid+optimistic+disputed+resolved+clawed+quorum", id="w", miner_id="q", client=GEN)]
    votes = {"x": {"j1": "0", "j2": "1"}, "y": {"j1": "1", "j2": "1", "p": "1"}, "z": {"j1": "1"},
             "w": {"j1": "0", "j2": "0"}}
    f = chain_day_facts(0, 1, jobs, {"x": 5, "y": 6, "z": 7, "w": DAY + 7}, votes, GEN, 100, 10 * DAY,
                        answered={"x", "y", "z", "w"}, last_height=END0)
    assert f["verdicts"] == {"j1": 1, "j2": 1}          # the primary never counts for its own job
    assert f["work"] == {"p": 1}


def rec(t, mid, day=0, **kw):
    return dict(kw, type=t, miner_id=mid, day=day)


def chain(work=None, verdicts=None, presence=None):
    return {"work": work or {}, "verdicts": verdicts or {}, "presence": presence or {}}


def test_a_row_carries_presence_work_verdicts_and_an_address_only():
    assert {f.name for f in fields(Identity)} == {"miner_id", "presence", "verified_requests", "verdicts",
                                                  "payout_address"}


def test_the_identities_are_the_chains_and_the_evidence_adds_none():
    # The evidence log sets an address and can take work away; it never makes an identity appear.
    records = [rec("payout", "ghost", address="dendra1ghost"),
               rec("work_answer", "ghost2", job_id="j", prompt="p", answer="a"),
               rec("work_grade", "ghost2", job_id="j", coherent=True),
               {"type": "work_sealed", "day": 0, "answers": 1, "drawn": 1}]
    rows = day_identities(0, records, chain(work={"w": 1}, verdicts={"v": 2}, presence={"p": 3}), {})
    assert [r.miner_id for r in rows] == ["p", "v", "w"]


def test_an_identity_known_only_by_its_presence_appears_with_no_work():
    rows = day_identities(0, [], chain(presence={"p": 12}), {"p": "dendra1op"})
    assert len(rows) == 1
    r = rows[0]
    assert (r.miner_id, r.presence, r.verified_requests, r.verdicts, r.payout_address) == \
        ("p", 12, 0, 0, "dendra1op")
    assert gross_of(r) == 0           # windows proven, no verified request: no presence paid that day


def test_the_presence_stands_beside_the_work_of_the_same_identity():
    r = day_identities(0, [], chain(work={"m": 2}, verdicts={"m": 1}, presence={"m": 12}), {"m": "a"})[0]
    assert (r.presence, r.verified_requests, r.verdicts) == (12, 2, 1)
    assert gross_of(r) == (12 * RULES["presence_per_window"] + 2 * RULES["work_per_request"]
                           + RULES["juror_per_verdict"])


def test_the_payout_address_is_the_declaration_else_the_operator():
    operators = {"a": "dendra1opa", "b": "dendra1opb", "c": "dendra1opc"}
    records = [rec("payout", "a", day=0, address="dendra1first"),
               rec("payout", "b", day=0, address="dendra1chosen"),
               rec("payout", "a", day=1, address="dendra1latest")]
    rows = day_identities(2, records, chain(work={"a": 1, "b": 1, "c": 1}), operators)
    # a declaration lasts until the next one; an identity without one is paid at its operator, never at
    # an address another identity declared
    assert {r.miner_id: r.payout_address for r in rows} == \
        {"a": "dendra1latest", "b": "dendra1chosen", "c": "dendra1opc"}


def work_day(sampled, grades, mid="m", day=0):
    recs = [rec("work_answer", mid, day=day, job_id=f"j{n}", prompt="p", answer="a") for n in range(sampled)]
    recs += [rec("work_grade", mid, day=day, job_id=f"j{n}", coherent=c, model="x") for n, c in enumerate(grades)]
    recs.append({"type": "work_sealed", "day": day, "answers": sampled, "drawn": sampled})
    return recs


def verified(records, day=0, work=6):
    return day_identities(day, records, chain(work={"m": work}), {"m": "a"})[0].verified_requests


def test_work_is_taken_away_only_when_every_graded_answer_is_incoherent():
    assert GRADES_TO_VOID == 2
    assert verified(work_day(1, [False])) == 0         # one sampled: min(2, 1) = 1 grade is enough
    assert verified(work_day(1, [True])) == 6
    assert verified(work_day(3, [False])) == 6         # three sampled: one grade is not enough
    assert verified(work_day(3, [False, False])) == 0
    assert verified(work_day(3, [False, False, False])) == 0
    assert verified(work_day(3, [False, True])) == 6   # one coherent answer keeps the work
    assert verified(work_day(3, [True, False, False])) == 6
    assert verified(work_day(2, [])) == 6              # no grade never takes the work away
    assert verified(work_day(0, [])) == 6              # nothing sampled: no grade, no verdict


def test_a_voided_work_day_takes_its_presence_with_it():
    graded_out = work_day(2, [False, False])
    c = chain(work={"m": 6}, verdicts={"m": 3}, presence={"m": 40})
    r = day_identities(0, graded_out, c, {"m": "a"})[0]
    assert r.verified_requests == 0
    assert r.presence == 40                            # the windows proven stay on the row, as measured,
    assert gross_of(r) == 3 * RULES["juror_per_verdict"]   # and are not paid: no verified request counted that day
    kept = day_identities(0, work_day(2, [False, True]), c, {"m": "a"})[0]
    assert gross_of(kept) == (6 * RULES["work_per_request"] + 3 * RULES["juror_per_verdict"]
                              + 40 * RULES["presence_per_window"])


def test_grades_count_only_for_their_own_identity_and_their_own_day():
    day0 = work_day(1, [False])
    assert verified(day0, day=0) == 0
    assert verified(day0, day=1) == 6                  # day 0's grades say nothing about day 1
    rows = day_identities(0, work_day(1, [False], mid="n"), chain(work={"m": 6, "n": 4}), {})
    assert {r.miner_id: r.verified_requests for r in rows} == {"m": 6, "n": 0}


def test_only_the_work_types_of_the_evidence_are_read_as_work():
    # The evidence log holds payout, work_answer, work_grade and work_sealed records; a record of any
    # other type, whatever its fields, neither samples an answer nor grades one.
    near = [rec("answer", "m", job_id="j0", prompt="p", answer="a"),
            rec("grade", "m", job_id="j0", coherent=False),
            rec("work_grades", "m", job_id="j0", coherent=False)]
    assert verified(near) == 6
    # one sampled answer graded incoherent voids the day; an answer of another type does not raise the
    # number of grades needed
    assert verified(work_day(1, [False]) + [rec("answer", "m", job_id="j9", prompt="p", answer="a")]) == 0
