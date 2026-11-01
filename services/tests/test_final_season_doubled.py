"""Bench of the doubled rates (`final_season_rules.RULES_DOUBLED`): twice the rewards, the daily cap and the season
cap, from one day to the end of the season, every other rule unchanged. The two sets published before stay as they
were (their fingerprints are the ones the service published), a day is ranked under the set of its own day, the
season cap is one total over days of different sets and is never paid past, the payment checks each day under the
set its fingerprint names, the payer names the first doubled day and every day is checked against it, and the
service fixes that day once: never on a published day, never before decision 18, never moved, and never a default
when the parameter is absent (a day, or `none`, is required until the day is recorded).

The rank-level cases (a day under the doubled rates, a day before them, the command line) are in
tests/test_final_season_rank.py, beside the fake chain they share.
"""
import json
import os
import sys

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, compute_day, to_json, week_totals  # noqa: E402

UDNDR = 1_000_000
# The fingerprints the service published, as its status served them
# (https://testnet-api.dendranetwork.com/final-season/v1/status): RULES_17 before decision 18, RULES after it.
PUBLISHED_17 = "c591851fb5dfd170fb4b9e1dd09d4a6027bb0bd60ea73b3730a39f25b6aeacd5"
PUBLISHED_18 = "532100a10124e53ea09c6069f41f5ba880f610a81302b75e40ac8f84e5a52472"


# -- the rule sets ------------------------------------------------------------------------------------------
def test_the_published_sets_are_unchanged_and_the_doubled_set_doubles_five_values_only():
    assert R.fingerprint(R.RULES_17) == PUBLISHED_17 and R.fingerprint(R.RULES) == PUBLISHED_18
    changed = {k: (R.RULES[k], v) for k, v in R.RULES_DOUBLED.items() if R.RULES.get(k) != v}
    assert changed == {"presence_per_window": (2_000, 4_000), "work_per_request": (50_000, 100_000),
                       "juror_per_verdict": (8_000, 16_000), "cap_programme_day": (50 * UDNDR, 100 * UDNDR),
                       "cap_season": (1_500 * UDNDR, 3_000 * UDNDR)}
    assert set(R.RULES_DOUBLED) == set(R.RULES)
    # decision 18 and its finality hold under the doubled rates
    assert R.pays_unwound_work(R.RULES_DOUBLED) is True and R.finality_rule(R.RULES_DOUBLED) == R.FINALITY
    assert R.RULE_HISTORY[2] is R.RULES_DOUBLED
    assert R.rule_set_of(R.fingerprint(R.RULES_DOUBLED)) == (2, R.RULES_DOUBLED)
    assert len({R.fingerprint(x) for x in R.RULE_HISTORY}) == len(R.RULE_HISTORY)


def test_a_day_takes_the_doubled_set_from_its_first_day_and_never_before():
    # the owner's case: decision 18 from day 1, the doubled rates from day 4
    got = [R.rules_for_day(d, 1, 4, None, None) for d in range(8)]
    assert got[0] is R.RULES_17 and all(r is R.RULES for r in got[1:4])
    assert all(r is R.RULES_DOUBLED for r in got[4:])
    # None: the doubled rates apply to no day, and every day keeps the set it had without them
    for d in range(8):
        assert (R.rules_for_day(d, 1, None, None, None) is R.rules_for_day(d, 1, 10 ** 6, None, None)
                is ([R.RULES_17] + [R.RULES] * 7)[d])
    # the same day as decision 18 is allowed: both start together
    assert R.rules_for_day(3, 3, 3, None, None) is R.RULES_DOUBLED and R.rules_for_day(2, 3, 3, None, None) is R.RULES_17


def test_the_first_doubled_day_is_required_and_anything_but_a_day_or_none_is_refused():
    with pytest.raises(TypeError):
        R.rules_for_day(4, 1)                                       # no default
    with pytest.raises(TypeError):
        R.rules_for_day(4, 1, 4)                                    # decision 23's day has none either
    for bad in (-1, "4", 4.0, True, [4]):
        with pytest.raises(ValueError, match="doubled rates"):
            R.rules_for_day(4, 1, bad, None, None)
    # never before decision 18, nor without it
    for unwound_from, doubled_from in ((5, 4), (None, 4), (1, 0)):
        with pytest.raises(ValueError, match="before decision 18"):
            R.rules_for_day(9, unwound_from, doubled_from, None, None)
    # a malformed decision 18 day is refused whatever the doubled day says
    with pytest.raises(ValueError, match="decision 18"):
        R.rules_for_day(9, "1", 4, None, None)


# -- the calculation: one season cap over days of different sets ---------------------------------------------
def _ids(requests, address="dendra1a"):
    return [Identity("m1", verified_requests=requests, payout_address=address)]


def test_a_doubled_day_has_its_own_daily_cap_and_what_remains_of_the_doubled_season_cap():
    # 2 000 requests: 100 DNDR gross under RULES (capped at 50), 200 under the doubled rates (capped at 100)
    assert compute_day(1, _ids(2_000), 0, R.RULES).total_paid == 50 * UDNDR
    assert compute_day(4, _ids(2_000), 0, R.RULES_DOUBLED).total_paid == 100 * UDNDR
    # the cap crossed in the middle of a day: 2 975 DNDR already paid leaves 25 of the 3 000
    d = compute_day(32, _ids(2_000), 2_975 * UDNDR, R.RULES_DOUBLED)
    assert d.day_budget == d.total_paid == 25 * UDNDR and (d.scale_num, d.scale_den) == (25 * UDNDR, 200 * UDNDR)
    assert d.rows[0].reason.endswith("daily budget shared pro rata")
    # past the doubled cap nothing more; and a day under RULES compares the same total to its own cap of 1 500
    assert compute_day(33, _ids(2_000), 3_000 * UDNDR, R.RULES_DOUBLED).total_paid == 0
    assert compute_day(3, _ids(2_000), 1_600 * UDNDR, R.RULES).total_paid == 0


# -- the payment: each day under its own set, the season cap never paid past ---------------------------------
def _addresses():
    from modea import cosmos_addr
    return [cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5)) for n in (1, 2)]


def _season(tmp_path, sets, requests):
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
        (tmp_path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")
        docs.append(out)
    return docs


def _owner_case(n):
    """The owner's case: day 0 under RULES_17, days 1 to 3 under RULES, the doubled rates from day 4."""
    return [R.rules_for_day(d, 1, 4, None, None) for d in range(n)]


def test_a_week_across_the_switch_is_paid_each_day_under_its_own_set(tmp_path):
    import final_season_payout as P
    _season(tmp_path, _owner_case(7), [2_000] * 7)        # every day past its daily cap
    payable, aside = P.plan(0, str(tmp_path), 4)
    assert not aside and sum(payable.values()) == (4 * 50 + 3 * 100) * UDNDR
    assert P.check_days([P.load_day(str(tmp_path), d) for d in range(7)]) == ""


def test_the_payer_names_the_first_doubled_day_and_a_week_doubled_on_other_days_is_refused(tmp_path):
    import final_season_payout as P
    _season(tmp_path, _owner_case(7), [2_000] * 7)
    assert P.plan(0, str(tmp_path), 4)[0]
    loaded = [P.load_day(str(tmp_path), d) for d in range(7)]
    assert P.check_doubled_from(loaded, 4) == ""
    # the service moved the day, or the payer names another one: refused before anything is planned
    for named, said in ((3, "day 3: not ranked under the doubled rates"), (5, "day 4: ranked under the doubled"),
                        (None, "day 4: ranked under the doubled rates, but the payer names no doubled day")):
        assert P.check_doubled_from(loaded, named).startswith(said)
        with pytest.raises(SystemExit, match="Week 0 is not paid"):
            P.plan(0, str(tmp_path), named)
    # a season doubled from day 3: every day checks under its own set, the payer's day refuses it
    data = tmp_path / "early"
    data.mkdir()
    _season(data, [R.rules_for_day(d, 1, 3, None, None) for d in range(7)], [2_000] * 7)
    early = [P.load_day(str(data), d) for d in range(7)]
    assert P.check_days(early) == "" and P.check_doubled_from(early, 4).startswith("day 3: ranked under")
    # no default, and nothing but a day or None
    with pytest.raises(TypeError):
        P.plan(0, str(tmp_path))
    for bad in (-1, "4", 4.0, True):
        with pytest.raises(ValueError, match="doubled rates"):
            P.plan(0, str(tmp_path), bad)
    # the command line: required, a day number or none
    for argv in ([], ["--doubled-from-day", "four"], ["--doubled-from-day", " 4"], ["--doubled-from-day", "None"]):
        with pytest.raises(SystemExit):
            P.main(["--week", "0", "--rankings", str(tmp_path)] + argv)
    assert P.main(["--week", "0", "--rankings", str(tmp_path), "--doubled-from-day", "4"]) == 0


def test_the_season_cap_is_crossed_once_and_no_week_pays_past_it(tmp_path):
    import final_season_payout as P
    # day 0 pays 25 DNDR, days 1 to 3 50 each, then 100 a day: the total reaches 2 975 after day 31, day 32 gets
    # the last 25 shared pro rata, and every later day nothing
    requests = [250] + [2_000] * 34
    docs = _season(tmp_path, _owner_case(35), requests)
    paid = [d["total_paid_udndr"] for d in docs]
    assert paid[0] == 25 * UDNDR and paid[1:4] == [50 * UDNDR] * 3 and paid[4:32] == [100 * UDNDR] * 28
    assert paid[32] == 25 * UDNDR and paid[33:] == [0, 0] and sum(paid) == 3_000 * UDNDR
    assert docs[32]["pro_rata"] == [25 * UDNDR, 400 * UDNDR]
    loaded = [P.load_day(str(tmp_path), d) for d in range(35)]
    assert P.check_days(loaded) == ""
    # each week pays its own days, once: the five weeks add up to the season, never past the doubled cap
    weeks = [sum(P.plan(w, str(tmp_path), 4)[0].values()) for w in range(5)]
    assert weeks == [475 * UDNDR, 700 * UDNDR, 700 * UDNDR, 700 * UDNDR, 425 * UDNDR]
    assert sum(weeks) == sum(sum(week_totals([d]).values()) for d in loaded) == 3_000 * UDNDR


def test_a_doubled_day_checked_against_another_total_or_another_set_is_refused(tmp_path):
    import final_season_payout as P
    _season(tmp_path, _owner_case(35), [250] + [2_000] * 34)
    loaded = [P.load_day(str(tmp_path), d) for d in range(35)]
    # day 32 recomputed as if the earlier days had paid nothing: its budget is not what remains of the cap
    a, b = _addresses()
    alone = compute_day(32, [Identity("m1", verified_requests=2_000, payout_address=a),
                             Identity("m2", verified_requests=2_000, payout_address=b)], 0, R.RULES_DOUBLED)
    bad = P.check_days(loaded[:32] + [alone])
    assert bad.startswith("day 32: budget") and "the earlier days paid" in bad
    # a day under RULES after a doubled day: an amendment is never undone for one day
    under_18 = compute_day(5, [Identity("m1", verified_requests=2_000, payout_address=a)], 0, R.RULES)
    bad = P.check_days(loaded[:5] + [under_18])
    assert bad.startswith("day 5: ranked under rules") and "earlier than the rules of a day before it" in bad
    # a doubled day's figures with the fingerprint of RULES: its amounts are not the formula of that set
    forged = loaded[4]
    forged.rules_fingerprint = R.fingerprint(R.RULES)
    bad = P.check_days(loaded[:4] + [forged])
    assert bad.startswith("day 4: ") and "the formula on its counts gives" in bad


def test_a_published_day_keeps_its_figures_when_a_later_day_takes_the_doubled_rates(tmp_path):
    before = _season(tmp_path, _owner_case(4), [2_000] * 4)
    after = _season(tmp_path, _owner_case(8), [2_000] * 8)
    assert after[:4] == before


# -- the service fixes the first doubled day once ------------------------------------------------------------
def _service():
    import final_season_server as S
    return S


def _ranked(data, d, rules):
    os.makedirs(os.path.join(data, "ranking"), exist_ok=True)
    with open(os.path.join(data, "ranking", f"day-{d:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"day": d, "rules_fingerprint": R.fingerprint(rules), "total_paid_udndr": 0}, f)


def _file(data):
    with open(os.path.join(data, "doubled-rates.json"), encoding="utf-8") as f:
        return json.load(f)


def _live(data):
    """The owner's service: day 0 ranked under RULES_17, decision 18 fixed from day 1, days 1 and 2 ranked."""
    S = _service()
    _ranked(data, 0, R.RULES_17)
    assert S.State(data, 1, "", "none").unwound_from == 1
    _ranked(data, 1, R.RULES)
    _ranked(data, 2, R.RULES)
    return S


def test_absent_the_parameter_stops_the_service_and_none_must_be_said(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    # absent or empty with no record: neither "no day" nor a day, the service stops and writes nothing
    with pytest.raises(SystemExit, match="is not set and no earlier start fixed"):
        S.State(data, 1, "", "")
    assert not os.path.exists(os.path.join(data, "doubled-rates.json"))
    # the module reads the variable once; empty there is the same refusal
    assert S.DOUBLED_FROM_PARAM == "none"
    old = S.DOUBLED_FROM_PARAM
    try:
        S.DOUBLED_FROM_PARAM = ""
        with pytest.raises(SystemExit, match="is not set"):
            S.State(data, 1, "")
    finally:
        S.DOUBLED_FROM_PARAM = old
    # `none` said: no doubled day, nothing written, and a later start may still name one
    st = S.State(data, 1, "", "none")
    assert (st.unwound_from, st.doubled_from) == (1, None)
    assert not os.path.exists(os.path.join(data, "doubled-rates.json"))
    assert S.State(data, 1, "", "4").doubled_from == 4
    # once a day is recorded, absent reads the record, and `none` contradicts it
    assert S.State(data, 1, "", "").doubled_from == 4
    with pytest.raises(SystemExit, match="already apply from day 4"):
        S.State(data, 1, "", "none")
    assert _file(data)["from_day"] == 4


def test_a_lost_record_after_a_doubled_day_is_published_stops_the_service_whatever_the_parameter(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "4")
    _ranked(data, 3, R.RULES)
    _ranked(data, 4, R.RULES_DOUBLED)
    os.remove(os.path.join(data, "doubled-rates.json"))
    with pytest.raises(SystemExit, match="is not set"):
        S.State(data, 1, "", "")
    with pytest.raises(SystemExit, match="day 4 is published under rules"):
        S.State(data, 1, "", "none")
    with pytest.raises(SystemExit, match="already ranked"):
        S.State(data, 1, "", "4")
    with pytest.raises(SystemExit, match="day 4 is published under rules"):
        S.State(data, 1, "", "5")


def test_a_named_day_not_ranked_yet_is_fixed_and_kept(tmp_path, capsys):
    data = str(tmp_path)
    S = _live(data)
    st = S.State(data, 1, "", "4")
    assert (st.unwound_from, st.doubled_from) == (1, 4)
    assert _file(data) == {"from_day": 4, "set_by": "parameter", "first_unranked_day": 3,
                           "rules_fingerprint": R.fingerprint(R.RULES_DOUBLED),
                           "rules_fingerprint_before": PUBLISHED_18}
    assert "doubled rates apply from day 4" in capsys.readouterr().out
    # days 3 to 5 ranked since; a restart keeps the day, with or without the parameter
    _ranked(data, 3, R.RULES)
    _ranked(data, 4, R.RULES_DOUBLED)
    _ranked(data, 5, R.RULES_DOUBLED)
    assert S.State(data, 1, "", "").doubled_from == 4
    assert S.State(data, 1, "", "4").doubled_from == 4
    with pytest.raises(SystemExit, match="names another day"):
        S.State(data, 1, "", "6")
    assert _file(data)["from_day"] == 4


def test_a_day_already_ranked_or_before_decision_18_or_malformed_is_refused_and_nothing_is_written(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    for bad in ("0", "1", "2"):
        with pytest.raises(SystemExit, match="already ranked"):
            S.State(data, 1, "", bad)
    for bad in ("x", "-1", "4.0", " 4", "4 ", "1e1", "None", "NONE", " none", "none "):
        with pytest.raises(SystemExit, match="neither a day number nor"):
            S.State(data, 1, "", bad)
    assert not os.path.exists(os.path.join(data, "doubled-rates.json"))
    # before decision 18: refused before either day is written
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    with pytest.raises(SystemExit, match="before decision 18"):
        S.State(data2, 1, "5", "3")
    assert not os.path.exists(os.path.join(data2, "doubled-rates.json"))
    assert not os.path.exists(os.path.join(data2, "decision-18.json"))


def test_a_published_day_under_other_rules_than_both_days_give_stops_the_service(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "4")
    _ranked(data, 3, R.RULES)
    _ranked(data, 4, R.RULES)                                            # day 4 ranked without the doubled rates
    with pytest.raises(SystemExit, match="day 4 is published under rules"):
        S.State(data, 1, "", "")
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    S = _live(data2)
    _ranked(data2, 3, R.RULES_DOUBLED)                                   # a doubled day before its first day
    with pytest.raises(SystemExit, match="day 3 is published under rules"):
        S.State(data2, 1, "", "5")
    assert not os.path.exists(os.path.join(data2, "doubled-rates.json"))  # checked before anything is written


def test_an_unreadable_or_foreign_record_of_the_doubled_day_stops_the_service(tmp_path):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "4")
    path = os.path.join(data, "doubled-rates.json")
    good = _file(data)
    for bad in ("{", "[]", "null", json.dumps(dict(good, from_day=None)), json.dumps(dict(good, from_day=-1)),
                json.dumps(dict(good, from_day=True)), json.dumps(dict(good, from_day="4")),
                json.dumps(dict(good, rules_fingerprint=PUBLISHED_18)),
                json.dumps(dict(good, rules_fingerprint_before=PUBLISHED_17)),
                json.dumps({k: v for k, v in good.items() if k != "from_day"}),
                json.dumps(dict(good, from_day=0))):                     # before decision 18
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        with pytest.raises(SystemExit):
            S.State(data, 1, "", "")


def test_the_record_of_the_doubled_day_is_read_at_a_restart_never_rewritten(tmp_path, monkeypatch):
    data = str(tmp_path)
    S = _live(data)
    S.State(data, 1, "", "4")
    path = os.path.join(data, "doubled-rates.json")
    with open(path, "rb") as f:
        first = f.read()

    def refused(*a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(S.os, "replace", refused)
    assert S.State(data, 1, "", "").doubled_from == 4
    with open(path, "rb") as f:
        assert f.read() == first
    # and the start that would fix it refuses to run when it cannot keep it
    data2 = str(tmp_path / "b")
    os.makedirs(data2)
    _ranked(data2, 0, R.RULES)
    with open(os.path.join(data2, "decision-18.json"), "w", encoding="utf-8") as f:
        json.dump({"from_day": 0, "set_by": "parameter", "first_unranked_day": 0, "rule": R.UNWOUND_AUDIT_WORK,
                   "rules_fingerprint": PUBLISHED_18, "rules_fingerprint_before": PUBLISHED_17}, f)
    with pytest.raises(SystemExit, match="cannot be written"):
        S.State(data2, 1, "", "3")
