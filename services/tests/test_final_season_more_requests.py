"""Bench of decision 23 (`final_season_rules.RULES_MORE_REQUESTS`): more requests a day, from one day to the end of
the season, at the rates and caps of the doubled days. The three sets published before stay as they were (their
fingerprints are the ones the service published), a day is ranked under the set of its own day, a day under the new
set pays what the same counts pay under the doubled rates, the payer's one argument (`--doubled-from-day`) still
checks every week and counts the new set's days as doubled, the service fixes the new day once (never on a published
day, never before the first doubled day nor without one, never moved, never a default when the parameter is absent),
and the generator takes each day's number from the set the service names for that day, never from a number the
network sends, a restart in the middle of a day included.

The rank-level cases (a day under the new set, the days before it, the command line) are in
tests/test_final_season_rank.py, beside the fake chain they share.
"""
import json
import os
import sys
import tempfile
import types

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, compute_day, gross_of, to_json  # noqa: E402

UDNDR = 1_000_000
# The fingerprints the service published, as its status served them
# (https://testnet-api.dendranetwork.com/final-season/v1/status): RULES_17 before decision 18, RULES after it, and
# RULES_DOUBLED from day 4 (read on 2026-10-12, day 5).
PUBLISHED_17 = "c591851fb5dfd170fb4b9e1dd09d4a6027bb0bd60ea73b3730a39f25b6aeacd5"
PUBLISHED_18 = "532100a10124e53ea09c6069f41f5ba880f610a81302b75e40ac8f84e5a52472"
PUBLISHED_DOUBLED = "a7248db8ab3ff26107f7339c543b3584a1be8b7a16a2ac162c403f9667323cb2"


def _owner(day):
    """The owner's case: decision 18 from day 1, the doubled rates from day 4, decision 23 from day 6."""
    return R.rules_for_day(day, 1, 4, 6, None)


# -- the rule sets ------------------------------------------------------------------------------------------
def test_the_published_sets_are_unchanged_and_decision_23_changes_the_daily_volume_only():
    assert R.fingerprint(R.RULES_17) == PUBLISHED_17 and R.fingerprint(R.RULES) == PUBLISHED_18
    assert R.fingerprint(R.RULES_DOUBLED) == PUBLISHED_DOUBLED
    changed = {k: (R.RULES_DOUBLED[k], v) for k, v in R.RULES_MORE_REQUESTS.items() if R.RULES_DOUBLED.get(k) != v}
    assert changed == {"requests_per_day": (600, 900)}                      # the owner's choice: 900
    assert set(R.RULES_MORE_REQUESTS) == set(R.RULES_DOUBLED)
    # decision 18 and its finality hold under the new set
    assert R.pays_unwound_work(R.RULES_MORE_REQUESTS) is True and R.finality_rule(R.RULES_MORE_REQUESTS) == R.FINALITY
    assert R.PUBLISHED_SETS == (R.RULES_17, R.RULES, R.RULES_DOUBLED, R.RULES_MORE_REQUESTS)
    assert R.RULE_HISTORY[:4] == R.PUBLISHED_SETS
    assert R.rule_set_of(R.fingerprint(R.RULES_MORE_REQUESTS)) == (3, R.RULES_MORE_REQUESTS)
    assert len({R.fingerprint(x) for x in R.RULE_HISTORY}) == len(R.RULE_HISTORY) >= 4


def test_a_day_takes_the_set_of_decision_23_from_its_first_day_and_never_before():
    got = [_owner(d) for d in range(10)]
    assert got[0] is R.RULES_17 and all(r is R.RULES for r in got[1:4])
    assert got[4] is R.RULES_DOUBLED and got[5] is R.RULES_DOUBLED
    assert all(r is R.RULES_MORE_REQUESTS for r in got[6:])
    # None: decision 23 applies to no day, and every day keeps the set it had without it
    for d in range(10):
        assert R.rules_for_day(d, 1, 4, None, None) is R.rules_for_day(d, 1, 4, 10 ** 6, None) is ([R.RULES_17] + [R.RULES] * 3
                                                                                      + [R.RULES_DOUBLED] * 6)[d]
    # the same day as the doubled rates is allowed: both start together
    assert R.rules_for_day(4, 1, 4, 4, None) is R.RULES_MORE_REQUESTS and R.rules_for_day(3, 1, 4, 4, None) is R.RULES


def test_the_first_day_of_decision_23_is_required_and_anything_but_a_day_or_none_is_refused():
    with pytest.raises(TypeError):
        R.rules_for_day(6, 1, 4, None)                              # no default for the fifth
    for bad in (-1, "6", 6.0, True, [6]):
        with pytest.raises(ValueError, match="decision 23"):
            R.rules_for_day(6, 1, 4, bad, None)
    # never before the doubled rates, nor without them
    for doubled_from, more_requests_from in ((4, 3), (None, 6), (5, 4)):
        with pytest.raises(ValueError, match="before the doubled rates"):
            R.rules_for_day(9, 1, doubled_from, more_requests_from, None)
    # a malformed doubled day or decision 18 day is refused whatever decision 23's day says
    with pytest.raises(ValueError, match="doubled rates"):
        R.rules_for_day(9, 1, "4", 6, None)
    with pytest.raises(ValueError, match="decision 18"):
        R.rules_for_day(9, "1", 4, 6, None)


# -- the calculation: the same counts pay the same --------------------------------------------------------------
def test_a_day_under_decision_23_pays_exactly_what_the_doubled_rates_pay():
    # `requests_per_day` is read by no figure: the gross, the budget and the pro rata are those of RULES_DOUBLED.
    rows = [Identity("m1", presence=50, verified_requests=900, verdicts=12, payout_address="dendra1a"),
            Identity("m2", presence=7, verified_requests=3, verdicts=0, payout_address="dendra1b"),
            Identity("m3", presence=50, verified_requests=0, verdicts=4, payout_address="")]
    for row in rows:
        assert gross_of(row, R.RULES_MORE_REQUESTS) == gross_of(row, R.RULES_DOUBLED)
    for paid_before in (0, 1_200 * UDNDR, 2_950 * UDNDR, 3_000 * UDNDR):
        more = to_json(compute_day(6, rows, paid_before, R.RULES_MORE_REQUESTS))
        doubled = to_json(compute_day(6, rows, paid_before, R.RULES_DOUBLED))
        assert more.pop("rules_fingerprint") == R.fingerprint(R.RULES_MORE_REQUESTS)
        assert doubled.pop("rules_fingerprint") == R.fingerprint(R.RULES_DOUBLED)
        assert more == doubled


# -- the payment: the payer's doubled day checks every week, decision 23's days included ------------------------
def _addresses():
    from modea import cosmos_addr
    return [cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5)) for n in (1, 2)]


def _season(path, sets, requests):
    """Days 0..N-1, day d ranked under `sets[d]` with m1 and m2 serving `requests[d]` each, as
    `final_season_rank` publishes them (the earlier days' total carried into each budget)."""
    a, b = _addresses()
    paid, docs = 0, []
    for d, rules in enumerate(sets):
        r = compute_day(d, [Identity("m1", verified_requests=requests[d], payout_address=a),
                            Identity("m2", verified_requests=requests[d], payout_address=b)], paid, rules)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": (d + 1) * 17_280, "season_end_height": None}
        (path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")
        docs.append(out)
    return docs


def test_a_week_across_decision_23_is_paid_with_the_payers_doubled_day_alone(tmp_path):
    import final_season_payout as P
    _season(tmp_path, [_owner(d) for d in range(14)], [300] * 14)
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    assert P.check_days(loaded) == "" and P.check_doubled_from(loaded, 4) == ""
    # 300 requests each for two identities: 30 DNDR a day at the first rates, 60 at the doubled ones
    weeks = [sum(P.plan(w, str(tmp_path), 4)[0].values()) for w in range(2)]
    assert weeks == [(4 * 30 + 3 * 60) * UDNDR, 7 * 60 * UDNDR]
    # the weekly command is unchanged: the doubled day, and no argument for decision 23
    for w in ("0", "1"):
        assert P.main(["--week", w, "--rankings", str(tmp_path), "--doubled-from-day", "4"]) == 0
    with pytest.raises(SystemExit):
        P.main(["--week", "1", "--rankings", str(tmp_path), "--doubled-from-day", "4", "--more-requests-from-day", "6"])
    # a payer who names decision 23's day as the first doubled one is refused: day 4 is doubled
    for named, said in ((6, "day 4: ranked under the doubled rates, but the payer names day 6"),
                        (None, "day 4: ranked under the doubled rates, but the payer names no doubled day")):
        assert P.check_doubled_from(loaded, named).startswith(said)
        with pytest.raises(SystemExit, match="Week 1 is not paid"):
            P.plan(1, str(tmp_path), named)


def test_a_day_of_decision_23_counts_as_a_doubled_day_for_the_payer(tmp_path):
    import final_season_payout as P
    # decision 23 from the first doubled day: every doubled day is under the new set
    _season(tmp_path, [R.rules_for_day(d, 1, 4, 4, None) for d in range(7)], [300] * 7)
    loaded = [P.load_day(str(tmp_path), d) for d in range(7)]
    assert P.check_days(loaded) == "" and P.check_doubled_from(loaded, 4) == ""
    assert P.check_doubled_from(loaded, 5).startswith("day 4: ranked under the doubled rates, but the payer names day 5")
    # a service that put the new set on day 3, a day the payer does not double: refused, never paid at double rates
    early = tmp_path / "early"
    early.mkdir()
    _season(early, [R.RULES_17, R.RULES, R.RULES, R.RULES_MORE_REQUESTS, R.RULES_MORE_REQUESTS,
                    R.RULES_MORE_REQUESTS, R.RULES_MORE_REQUESTS], [300] * 7)
    loaded = [P.load_day(str(early), d) for d in range(7)]
    assert P.check_days(loaded) == ""
    assert P.check_doubled_from(loaded, 4).startswith("day 3: ranked under the doubled rates, but the payer names day 4")
    with pytest.raises(SystemExit, match="Week 0 is not paid"):
        P.plan(0, str(early), 4)


def test_a_doubled_day_after_a_day_of_decision_23_is_refused(tmp_path):
    import final_season_payout as P
    sets = [_owner(d) for d in range(7)] + [R.RULES_DOUBLED] * 7          # day 7 goes back to the doubled set
    _season(tmp_path, sets, [300] * 14)
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    bad = P.check_days(loaded)
    assert bad.startswith("day 7: ranked under rules") and "earlier than the rules of a day before it" in bad
    with pytest.raises(SystemExit, match="Week 1 is not paid"):
        P.plan(1, str(tmp_path), 4)


def test_moving_the_first_day_of_decision_23_moves_no_amount_the_payer_pays(tmp_path):
    # Why the payer needs no argument for decision 23: a service that published the new set from another day, or
    # not at all, makes the payment pay the same amounts to the same addresses.
    import final_season_payout as P
    plans = []
    for name, more_from in (("owner", 6), ("early", 4), ("late", 12), ("never", None)):
        d = tmp_path / name
        d.mkdir()
        _season(d, [R.rules_for_day(n, 1, 4, more_from, None) for n in range(14)], [700] * 14)
        plans.append([P.plan(w, str(d), 4) for w in range(2)])
    assert all(p == plans[0] for p in plans[1:])


# -- the service fixes the first day of decision 23 once ------------------------------------------------------
def _service():
    import final_season_server as S
    return S


def _ranked(data, d, rules):
    os.makedirs(os.path.join(data, "ranking"), exist_ok=True)
    with open(os.path.join(data, "ranking", f"day-{d:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"day": d, "rules_fingerprint": R.fingerprint(rules), "total_paid_udndr": 0}, f)


def _file(data):
    with open(os.path.join(data, "more-requests.json"), encoding="utf-8") as f:
        return json.load(f)


def _live(data):
    """The owner's service: day 0 ranked under RULES_17, decision 18 fixed from day 1 and the doubled rates from
    day 4, days 1 to 3 ranked under RULES, day 4 under RULES_DOUBLED."""
    S = _service()
    _ranked(data, 0, R.RULES_17)
    st = S.State(data, 1, "", "4", "none")
    assert (st.unwound_from, st.doubled_from, st.more_requests_from) == (1, 4, None)
    for d in (1, 2, 3):
        _ranked(data, d, R.RULES)
    _ranked(data, 4, R.RULES_DOUBLED)
    return S


def test_absent_the_parameter_stops_the_service_and_none_must_be_said(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    # absent or empty with no record: neither "no day" nor a day, the service stops and writes nothing
    with pytest.raises(SystemExit, match="DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY is not set and no earlier"):
        S.State(data, 1, "", "", "")
    assert not os.path.exists(os.path.join(data, "more-requests.json"))
    # the module reads the variable once; empty there is the same refusal
    assert S.MORE_REQUESTS_FROM_PARAM == "none"
    old = S.MORE_REQUESTS_FROM_PARAM
    try:
        S.MORE_REQUESTS_FROM_PARAM = ""
        with pytest.raises(SystemExit, match="MORE_REQUESTS_FROM_DAY is not set"):
            S.State(data, 1, "", "")
    finally:
        S.MORE_REQUESTS_FROM_PARAM = old
    # `none` said: no day, nothing written, and a later start may still name one
    st = S.State(data, 1, "", "", "none")
    assert (st.unwound_from, st.doubled_from, st.more_requests_from) == (1, 4, None)
    assert not os.path.exists(os.path.join(data, "more-requests.json"))
    assert S.State(data, 1, "", "", "6").more_requests_from == 6
    # once a day is recorded, absent reads the record, and `none` contradicts it
    assert S.State(data, 1, "", "", "").more_requests_from == 6
    with pytest.raises(SystemExit, match="already apply from day 6"):
        S.State(data, 1, "", "", "none")
    assert _file(data)["from_day"] == 6


def test_a_named_day_not_ranked_yet_is_fixed_and_kept(tmp_path, capsys):
    data = str(tmp_path)
    S = _live(data)
    st = S.State(data, 1, "", "", "6")
    assert (st.unwound_from, st.doubled_from, st.more_requests_from) == (1, 4, 6)
    assert _file(data) == {"from_day": 6, "set_by": "parameter", "first_unranked_day": 5,
                           "rules_fingerprint": R.fingerprint(R.RULES_MORE_REQUESTS),
                           "rules_fingerprint_before": PUBLISHED_DOUBLED}
    assert "the rules of decision 23 (900 requests a day) apply from day 6" in capsys.readouterr().out
    # days 5 to 7 ranked since; a restart keeps the day, with or without the parameter
    _ranked(data, 5, R.RULES_DOUBLED)
    _ranked(data, 6, R.RULES_MORE_REQUESTS)
    _ranked(data, 7, R.RULES_MORE_REQUESTS)
    assert S.State(data, 1, "", "", "").more_requests_from == 6
    assert S.State(data, 1, "", "4", "6").more_requests_from == 6
    with pytest.raises(SystemExit, match="names another day"):
        S.State(data, 1, "", "", "8")
    assert _file(data)["from_day"] == 6


def test_a_day_already_ranked_or_before_the_doubled_day_or_malformed_is_refused_and_nothing_is_written(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    for bad in ("0", "3", "4"):
        with pytest.raises(SystemExit, match="already ranked"):
            S.State(data, 1, "", "", bad)
    for bad in ("x", "-1", "6.0", " 6", "6 ", "1e1", "None", "NONE", " none", "none "):
        with pytest.raises(SystemExit, match="neither a day number nor"):
            S.State(data, 1, "", "", bad)
    assert not os.path.exists(os.path.join(data, "more-requests.json"))
    # before the first doubled day, or with no doubled day at all: refused before any day is written
    for doubled, more, said in (("4", "3", "from day 4"), ("none", "6", "fixed for no day")):
        fresh = str(tmp_path / ("b" + doubled))
        os.makedirs(fresh)
        with pytest.raises(SystemExit, match="would take effect before the doubled rates") as e:
            S.State(fresh, 1, "1", doubled, more)
        assert said in str(e.value)
        for name in ("more-requests.json", "doubled-rates.json", "decision-18.json"):
            assert not os.path.exists(os.path.join(fresh, name)), name


def test_a_lost_doubled_record_cannot_carry_decision_23_to_no_doubled_day(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "", "6")
    os.remove(os.path.join(data, "doubled-rates.json"))
    # read as no doubled day, the doubled rates would leave decision 23 with no day before it: refused
    with pytest.raises(SystemExit, match="would take effect before the doubled rates"):
        S.State(data, 1, "", "none", "")
    assert _file(data)["from_day"] == 6


def test_a_lost_record_after_a_day_of_decision_23_is_published_stops_the_service_whatever_the_parameter(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "", "6")
    _ranked(data, 5, R.RULES_DOUBLED)
    _ranked(data, 6, R.RULES_MORE_REQUESTS)
    os.remove(os.path.join(data, "more-requests.json"))
    with pytest.raises(SystemExit, match="is not set"):
        S.State(data, 1, "", "", "")
    with pytest.raises(SystemExit, match="day 6 is published under rules"):
        S.State(data, 1, "", "", "none")
    with pytest.raises(SystemExit, match="already ranked"):
        S.State(data, 1, "", "", "6")
    with pytest.raises(SystemExit, match="day 6 is published under rules"):
        S.State(data, 1, "", "", "7")


def test_a_published_day_under_other_rules_than_the_three_days_give_stops_the_service(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "", "6")
    _ranked(data, 5, R.RULES_DOUBLED)
    _ranked(data, 6, R.RULES_DOUBLED)                                    # day 6 ranked without decision 23
    with pytest.raises(SystemExit, match="day 6 is published under rules"):
        S.State(data, 1, "", "", "")
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    S = _live(data2)
    _ranked(data2, 5, R.RULES_MORE_REQUESTS)                             # a day of decision 23 before its first day
    with pytest.raises(SystemExit, match="day 5 is published under rules"):
        S.State(data2, 1, "", "", "7")
    assert not os.path.exists(os.path.join(data2, "more-requests.json"))  # checked before anything is written


def test_an_unreadable_or_foreign_record_of_decision_23_stops_the_service(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "", "6")
    path = os.path.join(data, "more-requests.json")
    good = _file(data)
    for bad in ("{", "[]", "null", json.dumps(dict(good, from_day=None)), json.dumps(dict(good, from_day=-1)),
                json.dumps(dict(good, from_day=True)), json.dumps(dict(good, from_day="6")),
                json.dumps(dict(good, rules_fingerprint=PUBLISHED_DOUBLED)),
                json.dumps(dict(good, rules_fingerprint_before=PUBLISHED_18)),
                json.dumps({k: v for k, v in good.items() if k != "from_day"}),
                json.dumps(dict(good, from_day=3))):                     # before the doubled rates
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        with pytest.raises(SystemExit):
            S.State(data, 1, "", "", "")


def test_the_record_of_decision_23_is_read_at_a_restart_never_rewritten(tmp_path, monkeypatch):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "", "6")
    path = os.path.join(data, "more-requests.json")
    with open(path, "rb") as f:
        first = f.read()

    def refused(*a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(S.os, "replace", refused)
    assert S.State(data, 1, "", "", "").more_requests_from == 6
    with open(path, "rb") as f:
        assert f.read() == first
    # and the start that would fix it refuses to run when it cannot keep it: the two earlier days are recorded, only
    # decision 23's record is to be written
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    with open(os.path.join(data2, "decision-18.json"), "w", encoding="utf-8") as f:
        json.dump({"from_day": 0, "set_by": "parameter", "first_unranked_day": 0, "rule": R.UNWOUND_AUDIT_WORK,
                   "rules_fingerprint": PUBLISHED_18, "rules_fingerprint_before": PUBLISHED_17}, f)
    with open(os.path.join(data2, "doubled-rates.json"), "w", encoding="utf-8") as f:
        json.dump({"from_day": 0, "set_by": "parameter", "first_unranked_day": 0,
                   "rules_fingerprint": PUBLISHED_DOUBLED, "rules_fingerprint_before": PUBLISHED_18}, f)
    with pytest.raises(SystemExit, match="more-requests.json cannot be written"):
        S.State(data2, 1, "", "", "0")


# -- the status and the ranking loop ----------------------------------------------------------------------------
def test_the_status_names_the_first_day_of_decision_23_and_the_rules_of_each_day(tmp_path, monkeypatch):
    S = _service()
    st = S.State(str(tmp_path), 1, "1", "4", "6")
    monkeypatch.setattr(S, "ST", st)
    st.latest_time = S.END - 10 * 86400
    for day in (0, 3, 4, 5, 6, 7, 30):
        st.height = 1 + day * 17_280 + 5
        got = S.Handler._status(None)
        assert got["day"] == day and got["rules"] == _owner(day)
        assert got["rules_fingerprint"] == R.fingerprint(_owner(day))
        firsts = (got["unwound_audit_work_from_day"], got["doubled_rates_from_day"], got["more_requests_from_day"])
        assert firsts == (1, 4, 6)
    assert got["rules"]["requests_per_day"] == R.RULES_MORE_REQUESTS["requests_per_day"]
    st2 = S.State(str(tmp_path / "none"), 1, "1", "4", "none")
    monkeypatch.setattr(S, "ST", st2)
    st2.height, st2.latest_time = 1 + 6 * 17_280, S.END - 10 * 86400
    got = S.Handler._status(None)
    assert got["more_requests_from_day"] is None and got["rules"] is R.RULES_DOUBLED


def test_the_ranking_loop_hands_the_first_day_of_decision_23_and_the_days_own_set(tmp_path, monkeypatch):
    S = _service()
    data = str(tmp_path)
    _live(data)
    _ranked(data, 5, R.RULES_DOUBLED)
    st = S.State(data, 1, "", "", "6")
    monkeypatch.setattr(S, "ST", st)
    seen, finality = [], []

    def rank_day(d, records, *a, **k):
        seen.append((d, k))
        raise ValueError("not final")

    def finality_blocks(params, rules):
        finality.append(R.fingerprint(rules))
        return 100

    class Stop(BaseException):
        pass
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > 1:
            raise Stop
    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", finality_blocks)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    with pytest.raises(Stop):
        S.rank_days()
    assert seen == [(6, {"unwound_from": 1, "doubled_from": 4, "more_requests_from": 6, "kit_rules_from": None,
                         "relay_store": S.RELAY_STORE})]
    assert finality == [R.fingerprint(R.RULES_MORE_REQUESTS)]


# -- the generator: the day's number is the set's, named by the service ---------------------------------------
class _Stop(BaseException):
    """Ends the generator's endless loop. A BaseException, so none of its `except Exception` can swallow it."""


_DROP = object()


def _generator(monkeypatch, polls, state=None, outcome=None):
    """Runs the shipped generator loop against a scripted status: poll i reports `polls[i]`, a (day, rules) pair
    whose rules are a set (its fingerprint computed when the status is read) or a raw fingerprint value (`_DROP`
    leaves the field out). The clock jumps far ahead at every reading, so a poll with requests left sends one.
    Returns the day of every request sent; every spacing asked for is in `_generator.spacing`, and the index of
    the poll each request was sent on in `_generator.sent_at`."""
    import final_season_generator as G
    served, spacing, sent_at, polled = [], [], [], []
    it = iter(polls)

    def get(url):
        assert url.endswith("/status"), url
        nxt = next(it, None)
        if nxt is None:
            raise _Stop()
        polled.append(nxt)
        day, rules = nxt
        st = {"season": "final-testnet", "start_height": 1, "height": 1 + day * 17_280 + 5, "day": day,
              "ended": False, "latest_block_time": G.END - 10 * 86400,
              # what the status serves beside the fingerprint, which the generator must not read
              "rules": dict(R.RULES_DOUBLED, requests_per_day=10 ** 6)}
        if isinstance(rules, dict):
            st["rules_fingerprint"] = R.fingerprint(rules)
        elif rules is not _DROP:
            st["rules_fingerprint"] = rules
        return json.dumps(st).encode()

    class Clock:
        t = 0.0

        def time(self):
            self.t += 10 ** 6
            return self.t

        def sleep(self, s):
            pass

    real_spacing = G.spacing_s

    def spy(quota, sent, *a, **k):
        spacing.append((quota, sent))
        return real_spacing(quota, sent, *a, **k)

    def run_one(dc, day, n, rnd):
        served.append(day)
        sent_at.append(len(polled) - 1)
        return (outcome or (lambda k: ("served by dm1x, answer forwarded", True)))(len(served))

    monkeypatch.setattr(G, "_get", get)
    monkeypatch.setattr(G, "time", Clock())
    monkeypatch.setattr(G, "INTERNAL_TOKEN", "t" * 40)
    monkeypatch.setattr(G, "run_one", run_one)
    monkeypatch.setattr(G, "spacing_s", spy)
    monkeypatch.setitem(sys.modules, "client", types.ModuleType("client"))
    monkeypatch.setattr(G, "STATE", state or os.path.join(tempfile.mkdtemp(), "sent.json"))
    _generator.spacing, _generator.sent_at = spacing, sent_at
    try:
        G.main()
    except _Stop:
        pass
    return served


def test_the_generator_takes_the_days_number_from_the_set_the_status_names(monkeypatch, capsys):
    # Day 5 under the doubled rates, day 6 under decision 23: each day its own number, read from the set its
    # fingerprint names, never from the `rules` the status carries.
    before, after = R.RULES_DOUBLED["requests_per_day"], R.RULES_MORE_REQUESTS["requests_per_day"]
    served = _generator(monkeypatch, [(5, _owner(5))] * (before + 3) + [(6, _owner(6))] * (after + 3))
    assert served == [5] * before + [6] * after
    out = capsys.readouterr().out
    assert f"day 5: {before} answered requests" in out and f"day 5: {before} of {before} answered" in out
    assert f"day 6: {after} answered requests" in out
    assert f"requests_per_day of rules {R.fingerprint(R.RULES_MORE_REQUESTS)[:12]}" in out


def test_a_deploy_in_the_middle_of_day_6_still_serves_the_days_number_over_what_is_left_of_it(monkeypatch, capsys,
                                                                                              tmp_path):
    # The old generator sent part of day 6 under the old number and wrote its count; the new one, started mid-day,
    # resumes that count against the day's number and spreads what is left over what is left of the day.
    import final_season_generator as G
    after = R.RULES_MORE_REQUESTS["requests_per_day"]
    state = str(tmp_path / "sent.json")
    G.save_sent(state, 1, 6, 300, 290)
    served = _generator(monkeypatch, [(6, _owner(6))] * (after - 290 + 5), state=state)
    assert served == [6] * (after - 290)
    assert _generator.spacing[0] == (after, 290)
    out = capsys.readouterr().out
    assert f"day 6: {after} answered requests, 290 answered and 300 sent before a restart" in out
    with open(state, encoding="utf-8") as f:
        assert json.load(f) == {"start_height": 1, "day": 6, "sent": 300 + after - 290, "answered": after}


def test_a_set_fixed_from_the_current_day_takes_effect_within_the_day_and_is_said(monkeypatch, capsys):
    # The service restarted during day 6 with decision 23 from day 6: the day is ranked under the new set, so its
    # number is the new one, the requests already answered counted against it.
    monkeypatch.setitem(R.RULES_DOUBLED, "requests_per_day", 3)
    monkeypatch.setitem(R.RULES_MORE_REQUESTS, "requests_per_day", 5)
    served = _generator(monkeypatch, [(6, R.RULES_DOUBLED)] * 2 + [(6, R.RULES_MORE_REQUESTS)] * 6)
    assert served == [6] * 5
    out = capsys.readouterr().out
    assert out.count("5 answered requests instead of 3, with 2 answered and 2 sent so far") == 1


def test_the_days_bound_on_what_is_sent_follows_the_days_set(monkeypatch, capsys):
    import final_season_generator as G
    monkeypatch.setitem(R.RULES_MORE_REQUESTS, "requests_per_day", 2)
    bound = G.ATTEMPTS_PER_REQUEST * 2
    served = _generator(monkeypatch, [(6, R.RULES_MORE_REQUESTS)] * (bound + 4),
                        outcome=lambda k: ("refused: insufficient funds", False))
    assert served == [6] * bound
    assert f"day 6: {bound} requests sent, the day's bound ({G.ATTEMPTS_PER_REQUEST} x 2)" in capsys.readouterr().out


@pytest.mark.parametrize("first, then", [("doubled", "more"), ("more", "doubled")])
def test_a_set_that_changes_within_the_day_moves_the_days_bound_with_it(monkeypatch, capsys, first, then):
    # Every request refused, so only the bound stops the day. A set named mid-day brings its own bound, up or
    # down: the requests sent before the change count against it, and the bound of the old set no longer applies.
    import final_season_generator as G
    sets = {"doubled": R.RULES_DOUBLED, "more": R.RULES_MORE_REQUESTS}
    monkeypatch.setitem(R.RULES_DOUBLED, "requests_per_day", 3)
    monkeypatch.setitem(R.RULES_MORE_REQUESTS, "requests_per_day", 5)
    old, new = sets[first]["requests_per_day"], sets[then]["requests_per_day"]
    before = 4
    assert before < G.ATTEMPTS_PER_REQUEST * min(old, new)
    served = _generator(monkeypatch, [(6, sets[first])] * before + [(6, sets[then])] * (G.ATTEMPTS_PER_REQUEST * 5 + 4),
                        outcome=lambda k: ("refused: insufficient funds", False))
    assert served == [6] * (G.ATTEMPTS_PER_REQUEST * new)
    out = capsys.readouterr().out
    assert f"{new} answered requests instead of {old}, with 0 answered and {before} sent so far" in out
    assert out.count(f"day 6: {G.ATTEMPTS_PER_REQUEST * new} requests sent, the day's bound "
                     f"({G.ATTEMPTS_PER_REQUEST} x {new})") == 1
    assert "the day's bound" not in out.split("instead of")[0]


def test_a_set_that_becomes_unknown_within_the_day_stops_the_sending_and_says_so_once_per_outage(monkeypatch,
                                                                                                 capsys):
    # A known set, then a status that names none, twice: nothing is sent while the set is unknown, whatever the
    # day's number was a moment before, and the log reports each outage once.
    monkeypatch.setitem(R.RULES_MORE_REQUESTS, "requests_per_day", 10)
    known = [(6, R.RULES_MORE_REQUESTS)] * 2
    polls = known + [(6, "0" * 64)] * 3 + known + [(6, _DROP)] * 3 + known
    served = _generator(monkeypatch, polls)
    assert served == [6] * 6
    assert _generator.sent_at == [0, 1, 5, 6, 10, 11]
    out = capsys.readouterr().out
    assert out.count("names no rule set this generator knows") == 2
    assert out.count("day 6: 10 answered requests") == 1


@pytest.mark.parametrize("unknown", ["0" * 64, _DROP, None, 7, R.fingerprint(R.RULES_MORE_REQUESTS)[:12]])
def test_a_status_that_names_no_known_set_sends_nothing_and_says_so_once(monkeypatch, capsys, unknown):
    monkeypatch.setitem(R.RULES_MORE_REQUESTS, "requests_per_day", 2)
    served = _generator(monkeypatch, [(6, unknown)] * 4 + [(6, R.RULES_MORE_REQUESTS)] * 3)
    assert served == [6, 6]
    out = capsys.readouterr().out
    assert out.count("names no rule set this generator knows") == 1
    assert "day 6: 2 answered requests" in out
