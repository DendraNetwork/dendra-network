"""Bench of decision 24 of ADR-047 (`final_season_rules.RULES_KIT`): the judge presence reward of ADR-060, the
stale-kit refusal of ADR-058 decision 5 and the probation of new identities, from one day to the end of the season.

The four sets published before stay as they were (their fingerprints are the ones the service published, the
fourth read on 2026-10-16 at day 8). A day before the set keeps its bytes. Under the set: an identity seated on a
programme audit drawn that day, with a reveal sealed to it on the relay, a timely verdict committed after the
reveal's arrival, no verdict contradicted by a conviction concluded that day, and a signed capacity report that
says `active` on a kit at or above the floor, earns 0.5 DNDR once a day (`final_season_facts.judge_presence`); a
verdict dated before its reveal, a mute seat, a contradicted verdict on the day the conviction concludes, a
`mute`, `off`, `unknown` or absent report, a day without a programme audit and an absent relay store each pay
nothing, and the ranking says which (ADR-060 decision 2). An identity whose latest signed report at or before the
day's end declares a kit below the floor, or that has none, is paid nothing for the day
(`final_season_calc.kit_allows_pay`). A new identity earns 60 percent of its gross on its first full season day
after its registration, and on that day only (`final_season_facts.first_full_day`). The payer plans a week that
mixes the sets with its one argument and refuses a return to an earlier set; the service fixes the set's day once,
with the floor the set carries, and the faucet refuses a stale kit from the deadline (faucet.py).

`DENDRA_MODEA_DIR` puts another copy of the modules first on the path (the mutation bench of the development
repository, dendra_saison_vague1_test.sh, points it at a mutated copy and expects red).
"""
import io
import json
import os
import re
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.environ.get("DENDRA_MODEA_DIR"):
    sys.path.insert(0, os.environ["DENDRA_MODEA_DIR"])

import pytest  # noqa: E402

import final_season_chain as C  # noqa: E402
import final_season_facts as F  # noqa: E402
import final_season_rank as RK  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, compute_day, gross_of, kit_allows_pay, to_json  # noqa: E402

UDNDR = 1_000_000
# The fingerprints the service published, as its status served them
# (https://testnet-api.dendranetwork.com/final-season/v1/status).
PUBLISHED_17 = "c591851fb5dfd170fb4b9e1dd09d4a6027bb0bd60ea73b3730a39f25b6aeacd5"
PUBLISHED_18 = "532100a10124e53ea09c6069f41f5ba880f610a81302b75e40ac8f84e5a52472"
PUBLISHED_DOUBLED = "a7248db8ab3ff26107f7339c543b3584a1be8b7a16a2ac162c403f9667323cb2"
PUBLISHED_MORE_REQUESTS = "58769883109f2ccef8d385acfbd93e3623511180cb6619669fc713f2e593ba0f"   # read 2026-10-16, day 8
# The bench's floor: the kit number of v0.2.2 (docker/KIT_VERSION 2 at v0.2.1, plus the bump of ADR-058). The
# set's fingerprint for it is what ADR-047 decision 24 cites; another floor is another fingerprint.
KIT_MIN = 3

GEN = "dendra1generator"
NODE = "tcp://n:26657"
RPC = C.rpc_url(NODE)
DAY = R.RULES["day_blocks"]
START = 1
FINALITY = 1_000
TIP = 30 * DAY
END = R.end_epoch()
KIT_DAY = 10                           # the owner's case here: decision 18 from 1, doubled 4, decision 23 6, kit 10
FIRST, LAST = R.day_bounds(KIT_DAY, START)
FIRSTS = dict(unwound_from=1, doubled_from=4, more_requests_from=6, kit_rules_from=KIT_DAY)


def block_time(h):
    return END - 100 * 86400 + 5 * h


def _floor():
    """The bench's floor, fixed once per process (`final_season_rules.set_kit_min` refuses another)."""
    if R.KIT_MIN is None:
        R.set_kit_min(KIT_MIN)
    assert R.KIT_MIN == KIT_MIN
    return R.RULES_KIT


FINGERPRINT_KIT_3 = R.fingerprint(R.kit_rules(KIT_MIN))


# -- the rule set ----------------------------------------------------------------------------------------------
def test_the_published_sets_are_unchanged_and_the_kit_set_adds_three_keys_and_carries_its_floor():
    assert R.fingerprint(R.RULES_17) == PUBLISHED_17 and R.fingerprint(R.RULES) == PUBLISHED_18
    assert R.fingerprint(R.RULES_DOUBLED) == PUBLISHED_DOUBLED
    assert R.fingerprint(R.RULES_MORE_REQUESTS) == PUBLISHED_MORE_REQUESTS
    assert R.PUBLISHED_SETS == (R.RULES_17, R.RULES, R.RULES_DOUBLED, R.RULES_MORE_REQUESTS)
    kit = _floor()
    added = {k: v for k, v in kit.items() if k not in R.RULES_MORE_REQUESTS}
    assert added == {"judge_active_per_day": 500_000, "probation_first_day_bps": 6_000, "kit_min": KIT_MIN}
    assert all(kit[k] == v for k, v in R.RULES_MORE_REQUESTS.items())           # nothing else changed
    assert R.kit_rules(KIT_MIN) == kit and R.fingerprint(kit) == FINGERPRINT_KIT_3
    assert R.fingerprint(R.kit_rules(KIT_MIN + 1)) != FINGERPRINT_KIT_3        # the fingerprint names the floor
    assert R.RULE_HISTORY == R.PUBLISHED_SETS + (kit,) and R.rule_set_of(FINGERPRINT_KIT_3) == (4, kit)
    assert R.pays_judge_presence(kit) is True and R.pays_unwound_work(kit) and R.finality_rule(kit) == R.FINALITY
    for s in R.PUBLISHED_SETS:
        assert R.pays_judge_presence(s) is False
    with pytest.raises(ValueError, match="decision 24"):
        R.pays_judge_presence(dict(R.RULES_MORE_REQUESTS, kit_min=3))             # one key without the others
    for bad in (0, -1, "3", 3.0, True):
        with pytest.raises(ValueError):
            R.kit_rules(bad)
    assert R.kit_min_setting("") is None and R.kit_min_setting(None) is None and R.kit_min_setting(" 3 ") == 3
    for bad in ("0", "-1", "x", "3.0", "03"):
        with pytest.raises(ValueError, match="DENDRA_KIT_MIN"):
            R.kit_min_setting(bad)


def test_a_day_takes_the_kit_set_from_its_first_day_and_never_before(monkeypatch):
    kit = _floor()
    got = [R.rules_for_day(d, 1, 4, 6, 10) for d in range(14)]
    assert got[:6] == [R.rules_for_day(d, 1, 4, 6, None) for d in range(6)]
    assert all(r is R.RULES_MORE_REQUESTS for r in got[6:10]) and all(r is kit for r in got[10:])
    for d in range(14):                 # None: the set applies to no day
        assert R.rules_for_day(d, 1, 4, 6, None) is R.rules_for_day(d, 1, 4, 6, 10 ** 6)
    assert R.rules_for_day(6, 1, 4, 6, 6) is kit                              # the same day as decision 23
    with pytest.raises(TypeError):
        R.rules_for_day(10, 1, 4, 6)                                            # no default
    for bad in (-1, "10", 10.0, True, [10]):
        with pytest.raises(ValueError, match="decision 24"):
            R.rules_for_day(10, 1, 4, 6, bad)
    for more, kit_from in ((6, 5), (None, 10)):                                 # never before decision 23, nor without it
        with pytest.raises(ValueError, match="before decision 23"):
            R.rules_for_day(12, 1, 4, more, kit_from)
    # without a floor the set does not exist: a day under it is refused and says what to set
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    with pytest.raises(ValueError, match="DENDRA_KIT_MIN"):
        R.rules_for_day(10, 1, 4, 6, 10)
    assert R.rules_for_day(9, 1, 4, 6, 10) is R.RULES_MORE_REQUESTS            # a day before it needs none
    with pytest.raises(ValueError, match="already fixed"):
        R.set_kit_min(KIT_MIN) and R.set_kit_min(KIT_MIN + 1)


# -- the calculation ---------------------------------------------------------------------------------------------
def test_the_gross_adds_the_judge_day_applies_the_probation_last_and_the_floor_zeroes_the_payable():
    kit = _floor()
    base = Identity("m", presence=10, verified_requests=4, verdicts=2, payout_address="dendra1a", kit_version=KIT_MIN)
    plain = 10 * 4_000 + 4 * 100_000 + 2 * 16_000
    assert gross_of(base, kit) == plain == gross_of(Identity("m", 10, 4, 2, "dendra1a"), R.RULES_MORE_REQUESTS)
    judge = Identity("m", 10, 4, 2, "dendra1a", judge_active=1, kit_version=KIT_MIN)
    assert gross_of(judge, kit) == plain + 500_000
    # a judge day alone ranks: no work, no presence paid, the flat amount
    assert gross_of(Identity("j", presence=5, judge_active=1, kit_version=KIT_MIN), kit) == 500_000
    # the probation takes 60 percent of the whole gross, the judge day included, floor
    assert gross_of(Identity("m", 10, 4, 2, "dendra1a", 1, True, KIT_MIN), kit) == (plain + 500_000) * 6_000 // 10_000
    assert gross_of(Identity("m", 0, 0, 1, "dendra1a", 0, True, KIT_MIN), kit) == 16_000 * 6_000 // 10_000 == 9_600
    # under an earlier set the four fields are refused, never ignored
    for bad in (dict(judge_active=1), dict(probation=True), dict(registration_not_dated=True)):
        with pytest.raises(ValueError, match="rule set that has none"):
            gross_of(Identity("m", 1, 1, 0, "dendra1a", **bad), R.RULES_MORE_REQUESTS)
    with pytest.raises(ValueError):
        gross_of(Identity("m", 1, 1, 0, "dendra1a", judge_active=2), kit)
    # a 1 is not a true: 1 == True in Python, and a flag that is not a bool is refused, never read as one
    for bad in (dict(probation=1), dict(probation=0), dict(registration_not_dated=1)):
        with pytest.raises(ValueError, match="neither true nor false"):
            gross_of(Identity("m", 1, 1, 0, "dendra1a", kit_version=KIT_MIN, **bad), kit)
    # an undated registration: the gross is the formula, the payable is 0, under the kit set only
    from final_season_calc import registration_allows_pay
    assert registration_allows_pay(Identity("m", kit_version=KIT_MIN), kit)
    assert not registration_allows_pay(Identity("m", kit_version=KIT_MIN, registration_not_dated=True), kit)
    assert registration_allows_pay(Identity("m"), R.RULES_MORE_REQUESTS)
    # the floor: a kit at or above pays, below or none does not, under the kit set only
    assert kit_allows_pay(Identity("m", kit_version=KIT_MIN), kit) and kit_allows_pay(Identity("m", kit_version=9), kit)
    assert not kit_allows_pay(Identity("m", kit_version=KIT_MIN - 1), kit) and not kit_allows_pay(Identity("m"), kit)
    assert kit_allows_pay(Identity("m"), R.RULES_MORE_REQUESTS) and kit_allows_pay(Identity("m", kit_version=0), R.RULES)
    with pytest.raises(ValueError):
        kit_allows_pay(Identity("m", kit_version="3"), kit)
    day = compute_day(10, [Identity("ok", 0, 2, 0, "dendra1a", kit_version=KIT_MIN),
                           Identity("old", 0, 2, 0, "dendra1b", kit_version=KIT_MIN - 1),
                           Identity("none", 0, 2, 0, "dendra1c"),
                           Identity("new", 0, 2, 0, "dendra1d", probation=True, kit_version=KIT_MIN),
                           Identity("undated", 0, 2, 0, "dendra1e", kit_version=KIT_MIN, registration_not_dated=True)],
                      0, kit)
    rows = {r.miner_id: r for r in day.rows}
    assert (rows["ok"].gross, rows["ok"].payable, rows["ok"].paid) == (200_000, 200_000, 200_000)
    assert (rows["old"].gross, rows["old"].payable, rows["old"].paid) == (200_000, 0, 0)
    assert "kit 2 below the floor 3" in rows["old"].reason
    assert (rows["none"].payable, rows["none"].paid) == (0, 0) and "no signed capacity report" in rows["none"].reason
    assert (rows["new"].gross, rows["new"].paid) == (120_000, 120_000) and "probation" in rows["new"].reason
    assert (rows["undated"].gross, rows["undated"].payable, rows["undated"].paid) == (200_000, 0, 0)
    assert rows["undated"].reason == "registration not datable: not paid"
    # the published row carries the four fields under the kit set, and under no earlier set
    pub = {r["miner_id"]: r for r in to_json(day)["ranking"]}
    assert (pub["old"]["judge_active"], pub["old"]["probation"], pub["old"]["kit_version"]) == (0, False, 2)
    assert pub["old"]["registration_not_dated"] is False and pub["undated"]["registration_not_dated"] is True
    assert pub["none"]["kit_version"] is None and pub["new"]["probation"] is True
    before = to_json(compute_day(9, [Identity("ok", 0, 2, 0, "dendra1a")], 0, R.RULES_MORE_REQUESTS))["ranking"][0]
    assert not {"judge_active", "probation", "kit_version", "registration_not_dated"} & set(before)


def test_a_day_over_its_budget_shares_the_judge_term_pro_rata_within_the_day_and_season_caps():
    kit = _floor()
    a = _addresses()[0]
    judges = [Identity(f"j{n:03d}", judge_active=1, payout_address=a, kit_version=KIT_MIN) for n in range(250)]
    day = compute_day(10, judges, 0, kit)
    assert day.total_payable == 250 * 500_000 == 125 * UDNDR and day.day_budget == 100 * UDNDR
    assert (day.scale_num, day.scale_den) == (100 * UDNDR, 125 * UDNDR)
    assert all(r.paid == 400_000 and "pro rata" in r.reason for r in day.rows) and day.total_paid == 100 * UDNDR
    # the season cap: 1 DNDR left after the earlier days, shared among the same rows, the judge term included
    day = compute_day(10, judges, kit["cap_season"] - UDNDR, kit)
    assert day.day_budget == UDNDR and all(r.paid == 4_000 for r in day.rows) and day.total_paid == UDNDR
    # a work row and a judge row together: one ratio for both terms, never the judge term on top of the budget
    rows = [Identity("w", verified_requests=999, payout_address=a, kit_version=KIT_MIN),
            Identity("j", judge_active=1, payout_address=a, kit_version=KIT_MIN)]
    day = compute_day(10, rows, 0, kit)
    by = {r.miner_id: r for r in day.rows}
    assert by["w"].payable == 99_900_000 and by["j"].payable == 500_000 and day.total_payable == 100_400_000
    assert by["j"].paid == 500_000 * 100 * UDNDR // 100_400_000 < 500_000
    assert by["w"].paid == 99_900_000 * 100 * UDNDR // 100_400_000
    assert day.total_paid == by["w"].paid + by["j"].paid <= day.day_budget == 100 * UDNDR


# -- the first full day ----------------------------------------------------------------------------------------------
def test_the_first_full_day_is_the_first_day_whose_first_block_follows_the_registration():
    first_n, last_n = R.day_bounds(KIT_DAY - 1, START)
    assert F.first_full_day(last_n, START) == KIT_DAY                 # registered in the last block of day 9
    assert F.first_full_day(last_n + 1, START) == KIT_DAY + 1         # in the first block of day 10
    assert F.first_full_day(first_n, START) == KIT_DAY                # anywhere in day 9
    assert F.first_full_day(None, START) == 0 == F.first_full_day(0, START)   # before the season: day 0
    assert F.first_full_day(START, START) == 1                        # registered in the season's first block
    for bad in (-1, "5", 5.0, True):
        with pytest.raises(ValueError):
            F.first_full_day(bad, START)


# -- the readers -----------------------------------------------------------------------------------------------------
def capacity(mid, kit, role, height, day=0):
    return {"type": F.CAPACITY, "day": day, "miner_id": mid, "kit_version": kit, "judge_role": role, "height": height}


def test_the_kit_report_is_the_latest_signed_report_at_or_before_the_days_end():
    recs = [capacity("m", 2, "off", 100), capacity("m", 3, "active", 200), capacity("m", 3, "mute", 400),
            capacity("other", 3, "active", 150)]
    assert F.kit_report(recs, "m", 300) == {"kit_version": 3, "judge_role": "active", "height": 200}
    assert F.kit_report(recs, "m", 199)["kit_version"] == 2 and F.kit_report(recs, "m", 99) is None
    assert F.kit_report(recs, "m", 400)["judge_role"] == "mute"       # at the bound: counted
    assert F.kit_report(recs, "nobody", 10 ** 9) is None
    # a record with no readable number or role reads None there, never a default
    odd = [capacity("m", None, None, 50), {"type": F.CAPACITY, "miner_id": "m", "height": 60}]
    assert F.kit_report(odd, "m", 70) == {"kit_version": None, "judge_role": None, "height": 60}
    assert F.kit_report([capacity("m", "3", True, 50)], "m", 70) == {"kit_version": None, "judge_role": None, "height": 50}
    assert F.kit_report([capacity("m", 3, "active", "50")], "m", 70) is None   # a height that is not one


def _store(path, records):
    """A relay store with the given reveal records, written by the store's own writer."""
    import relay_store
    st = relay_store.DiskStore(str(path), [F.REVEAL_KIND])
    for key, t in records.items():
        st.write(F.REVEAL_KIND, key, b"sealed", ts=t)
    return str(path)


def test_the_relay_store_reader_answers_three_ways(tmp_path):
    root = _store(tmp_path / "store", {"j1__a__p": 1_000.5, "j2__a__p": 2_000.0})
    got = F.reveal_arrivals(root, ["j1__a__p", "j2__a__p", "j3__a__p"])
    assert got == {"j1__a__p": 1_000.5, "j2__a__p": 2_000.0, "j3__a__p": None}
    # no store named, or a store that is not there: unknown for every key, never "no reveal"
    assert F.reveal_arrivals("", ["k"]) == {"k": F.STORE_UNREAD}
    assert F.reveal_arrivals(str(tmp_path / "absent"), ["k"]) == {"k": F.STORE_UNREAD}
    # a torn record, or one stored under another key, is unknown for that key alone
    import relay_store
    with open(os.path.join(root, F.REVEAL_KIND, relay_store._fname(F.REVEAL_KIND, "j2__a__p")), "wb") as f:
        f.write(b"{")
    assert F.reveal_arrivals(root, ["j1__a__p", "j2__a__p"]) == {"j1__a__p": 1_000.5, "j2__a__p": F.STORE_UNREAD}
    state, arr = RK.read_reveals(root, ["j1__a__p"])
    assert state == "read" and arr == {"j1__a__p": 1_000.5}
    assert RK.read_reveals("", [])[0] == F.STORE_UNREAD == RK.read_reveals(str(tmp_path / "no"), [])[0]
    # replayed from a published ranking: what it says, and unknown for a key it does not name
    pub = {"inputs": {"judge_presence": {"relay_store": "read", "reveals": {"a": 5.0, "b": None, "c": "?"}}}}
    assert RK.published_reveals(pub, ["a", "b", "c", "d"]) == ("read", {"a": 5.0, "b": None, "c": "?", "d": "?"})
    assert RK.published_reveals({}, ["a"]) == (F.STORE_UNREAD, {"a": F.STORE_UNREAD})


OP = "dendra1" + "k" * 38


def test_the_registration_reader_takes_the_first_create_miner_of_the_identity_signed_by_its_operator(monkeypatch):
    txs = [{"height": "70", "tx": {"body": {"messages": [{"@type": C.CREATE_MINER_ACTION, "minerId": "a",
                                                          "creator": OP}]}}},
           {"height": "50", "tx": {"body": {"messages": [{"@type": C.CREATE_MINER_ACTION, "miner_id": "a",
                                                          "creator": OP}]}}},
           {"height": "40", "tx": {"body": {"messages": [{"@type": "/other", "miner_id": "a", "creator": OP},
                                                         {"@type": C.CREATE_MINER_ACTION, "miner_id": "c",
                                                          "creator": OP}]}}},
           {"height": "30", "tx": {"body": {"messages": [{"@type": C.CREATE_MINER_ACTION, "miner_id": "a",
                                                          "creator": "dendra1" + "z" * 38}]}}},
           {"height": "20", "code": 5, "tx": {"body": {"messages": [{"@type": C.CREATE_MINER_ACTION, "miner_id": "a",
                                                                     "creator": OP}]}}}]
    queries, count = [], [len(txs)]

    def cli(args, node, timeout=60):
        queries.append(args)
        return {"txs": txs, "total_count": str(count[0])}
    monkeypatch.setattr(C, "_cli", cli)
    # the first accepted registration of `a` signed by its operator: not the later one (70), not another message
    # type (40), not another signer (30), not a refused transaction (20)
    assert C.miner_registration(NODE, "a", OP, 1, 100) == 50
    q = queries[0][3]
    assert C.CREATE_MINER_ACTION in q and f"message.sender='{OP}'" in q and "tx.height>=1" in q and "tx.height<=100" in q
    assert C.miner_registration(NODE, "c", OP, 1, 100) == 40
    assert C.miner_registration(NODE, "a", OP, 60, 100) == 70                 # the window bounds what is read
    assert C.miner_registration(NODE, "nobody", OP, 1, 100) is None           # searched, and none: before the window
    # a signer that floods its own search: SearchTooLong, for the caller to name (never the day unreadable)
    count[0] = 200 * 100 + 1
    with pytest.raises(C.SearchTooLong):
        C.miner_registration(NODE, "a", OP, 1, 100)
    # a signer that cannot be placed in a query is refused, never searched
    for bad in ("", None, "x' OR 1", "cosmos1abc"):
        with pytest.raises(C.ChainUnreadable, match="cannot be searched"):
            C.miner_registration(NODE, "a", bad, 1, 100)


# -- the five conditions, pure ---------------------------------------------------------------------------------------
def _presence(**over):
    seats = {"a1": ["j1", "j2", "j3"]}
    primaries = {"a1": "p1"}
    arrivals = {"a1__j1__p1": 150.0, "a1__j2__p1": 150.0, "a1__j3__p1": 150.0}
    votes = {"a1": {"j1": "1", "j3": "1"}}                       # j2 is mute
    committed = {"a1": {"j1": 200, "j3": 100}}
    commit_times = {200: 200.0, 100: 100.0}                       # j3 answered before its reveal arrived
    reports = {m: {"kit_version": KIT_MIN, "judge_role": "active", "height": 90} for m in ("j1", "j2", "j3")}
    args = dict(seats=seats, primaries=primaries, arrivals=arrivals, votes=votes, committed=committed,
                commit_times=commit_times, contradicted=set(), reports=reports, kit_min=KIT_MIN)
    args.update(over)
    return F.judge_presence(**args)


def test_a_seated_judge_with_a_reveal_and_a_later_timely_verdict_is_paid_once_and_the_proof_names_its_inputs():
    paid, proof = _presence()
    assert paid == {"j1": {"job": "a1", "reveal_key": "a1__j1__p1", "reveal_arrival": 150.0, "commit_height": 200,
                           "commit_time": 200.0, "report_height": 90}}
    assert proof["j1"]["paid"] is True and "why" not in proof["j1"] and proof["j1"]["audits"] == {"a1": "ok"}
    assert proof["j2"] == {"audits": {"a1": F.JUDGE_NO_TIMELY_VERDICT}, "report_height": 90, "judge_role": "active",
                           "kit_version": KIT_MIN, "paid": False, "why": F.JUDGE_NO_TIMELY_VERDICT}
    assert proof["j3"]["why"] == F.JUDGE_VERDICT_BEFORE_REVEAL and proof["j3"]["paid"] is False
    # a verdict committed in the very second the reveal arrived is not later than it
    paid, _ = _presence(commit_times={200: 150.0, 100: 100.0})
    assert paid == {}
    # two audits the same day: paid once, the first that satisfies the conditions is the one published
    paid, proof = _presence(seats={"a0": ["j1"], "a1": ["j1", "j2", "j3"]}, primaries={"a0": "p0", "a1": "p1"},
                            arrivals={"a0__j1__p0": None, "a1__j1__p1": 150.0, "a1__j2__p1": 150.0, "a1__j3__p1": 150.0})
    assert list(paid) == ["j1"] and paid["j1"]["job"] == "a1" and proof["j1"]["audits"] == {"a0": F.JUDGE_NO_REVEAL,
                                                                                               "a1": "ok"}


def test_no_reveal_an_unread_store_a_contradiction_or_a_report_that_is_not_active_pays_nothing_and_is_named():
    assert _presence(arrivals={"a1__j1__p1": None, "a1__j2__p1": None, "a1__j3__p1": None})[1]["j1"]["why"] == F.JUDGE_NO_REVEAL
    unread = {k: F.STORE_UNREAD for k in ("a1__j1__p1", "a1__j2__p1", "a1__j3__p1")}
    paid, proof = _presence(arrivals=unread)
    assert paid == {} and {p["why"] for p in proof.values()} == {F.JUDGE_STORE_UNREAD}
    paid, proof = _presence(contradicted={"j1"})
    assert paid == {} and proof["j1"]["why"] == F.JUDGE_CONTRADICTED and proof["j1"]["job"] == "a1"
    for role in ("mute", "off", "unknown", None, "Active"):
        reports = {m: {"kit_version": KIT_MIN, "judge_role": role, "height": 90} for m in ("j1", "j2", "j3")}
        paid, proof = _presence(reports=reports)
        assert paid == {} and proof["j1"]["why"] == F.JUDGE_ROLE_NOT_ACTIVE, role
    paid, proof = _presence(reports={"j1": None, "j2": None, "j3": None})
    assert paid == {} and proof["j1"]["why"] == F.JUDGE_NO_SIGNED_REPORT and "report_height" not in proof["j1"]
    for kv in (KIT_MIN - 1, 0, None):
        reports = {m: {"kit_version": kv, "judge_role": "active", "height": 90} for m in ("j1", "j2", "j3")}
        paid, proof = _presence(reports=reports)
        assert paid == {} and proof["j1"]["why"] == F.JUDGE_KIT_BELOW_MIN, kv
    # no seat at all: nobody, and nothing to say
    assert F.judge_presence({}, {}, {}, {}, {}, {}, set(), {}, KIT_MIN) == ({}, {})
    # an arrival not read for a seat is an error, never a default
    with pytest.raises(KeyError):
        _presence(arrivals={"a1__j1__p1": 150.0})


def test_the_contradicted_jurors_are_those_with_a_vote_against_a_concluded_outcome():
    jobs = {"c": {"state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "slashed_primary": False},
            "v": {"state": "open+paid+optimistic+disputed+resolved+vindicated+quorum", "slashed_primary": False},
            "u": {"state": "open+paid+optimistic+disputed+resolved+unwound", "slashed_primary": False}}
    commits = {"c": {"j1": {"vote": "1"}, "j2": {"vote": "0"}}, "v": {"j2": {"vote": "0"}, "j3": {"vote": "1"}},
               "u": {"j4": {"vote": "1"}}}
    assert F.contradicted_jurors({"c": 10, "v": 20, "u": 30}, commits, jobs) == {"j1", "j2"}
    assert F.contradicted_jurors({"v": 20}, commits, jobs) == {"j2"}        # the scope is the day's conclusions
    assert F.contradicted_jurors({}, commits, jobs) == set()


# -- the ranking of a day under the set, on a fake chain -------------------------------------------------------------
VINDICATED = "open+paid+optimistic+disputed+resolved+vindicated+quorum"
CONVICTED = "open+paid+optimistic+disputed+resolved+clawed+quorum"


@pytest.fixture()
def chain(monkeypatch, tmp_path):
    """Day 10 under the kit set. `a1`: a programme audit of primary p1, settled, drawn and resolved (FIRST+300)
    within the day, jury j1, j2, j3, j4; j1 votes at FIRST+200 after its reveal (FIRST+150), j2 is mute, j3 votes at
    FIRST+100 before its reveal, j4 votes at FIRST+350 after the resolution, when the outcome is public (F3). `g1`: a
    programme request p1 served. Every juror's signed report says active on the floor. `registrations`: {miner:
    height} the fake index holds; `flooded`: the miners whose registration search overflows (SearchTooLong)."""
    calls = {"tip": TIP, "jobs": [
        {"id": "g1", "state": "open+paid+optimistic", "miner_id": "p1", "client": GEN, "slashed_primary": False,
         "audit_voters": 0},
        {"id": "a1", "state": VINDICATED, "miner_id": "p1", "client": GEN, "slashed_primary": False, "audit_voters": 2}],
        "settled": {"g1": FIRST + 40, "a1": FIRST + 50}, "drawn": {"a1": FIRST + 60},
        "resolved": {"a1": (FIRST + 300, "end_block")}, "juries": {"a1": ["j1", "j2", "j3", "j4", "p1"]},
        "votes": {"a1": {"j1": "1", "j3": "1", "j4": "1"}},
        "committed": {("a1", "j1"): FIRST + 200, ("a1", "j3"): FIRST + 100, ("a1", "j4"): FIRST + 350},
        "registrations": {}, "flooded": set(), "reg_reads": [], "windows": {"p1": 2}, "height_reads": [], "index": []}

    def node_get(url, timeout=10.0):
        m = re.fullmatch(re.escape(RPC) + r"/block\?height=(\d+)", url)
        if m:
            h = int(m.group(1))
            assert 1 <= h <= calls["tip"], h
            return {"result": {"block": {"header": {"height": str(h), "time": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(block_time(h)))}}}}
        raise AssertionError("unexpected read " + url)

    def verdict_commits(node, audited, candidates):
        return {j["id"]: {m: {"vote": calls["votes"][j["id"]][m], "creator": "dendra1op" + m}
                          for m in candidates.get(j["id"], []) if m in calls["votes"].get(j["id"], {})}
                for j in audited if calls["votes"].get(j["id"])}

    def commit_height(node, key, creator, lo, hi):
        calls["height_reads"].append((key, lo, hi))
        jid, _, juror = key.partition("__verdict__")
        h = calls["committed"].get((jid, juror))
        return h if h is not None and lo <= h <= hi else None

    monkeypatch.setattr(C, "_get", node_get)
    monkeypatch.setattr(C, "_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected cli")))
    monkeypatch.setattr(C, "jobs", lambda node: calls["jobs"])
    monkeypatch.setattr(C, "settle_heights", lambda node, sender=None, max_pages=2000: dict(calls["settled"]))
    def miner_registration(node, mid, operator, lo, hi):
        calls["reg_reads"].append((mid, operator, lo, hi))
        assert operator == "dendra1op" + mid, (mid, operator)        # searched by the identity's own signer
        if mid in calls["flooded"]:
            raise C.SearchTooLong("registrations of " + mid, 20_001, 200)
        h = calls["registrations"].get(mid)
        return h if h is not None and lo <= h <= hi else None

    monkeypatch.setattr(C, "miners", lambda node: [{"id": m, "operator": "dendra1op" + m}
                                                   for m in ("p1", "j1", "j2", "j3", "j4", "j5", "p2")])
    monkeypatch.setattr(C, "require_index_from", lambda rpc, h: calls["index"].append(h))
    monkeypatch.setattr(C, "audit_draw", lambda rpc, jid: (calls["drawn"].get(jid), calls["juries"].get(jid, [])))
    monkeypatch.setattr(C, "verdict_commits", verdict_commits)
    monkeypatch.setattr(C, "audit_resolution", lambda node, rpc, jid: calls["resolved"].get(jid))
    monkeypatch.setattr(C, "commit_height", commit_height)
    monkeypatch.setattr(C, "presence_proofs", lambda node, first, last, eb: (
        dict(calls["windows"]), {m: "dendra1op" + m for m in calls["windows"]}))
    monkeypatch.setattr(C, "miner_registration", miner_registration)
    calls["store"] = _store(tmp_path / "relay-store", {"a1__j1__p1": block_time(FIRST + 150),
                                                        "a1__j2__p1": block_time(FIRST + 150),
                                                        "a1__j3__p1": block_time(FIRST + 150),
                                                        "a1__j4__p1": block_time(FIRST + 150)})
    _floor()
    return calls


def records(reports=None, day=KIT_DAY):
    """The evidence up to the day: p1's answer to g1 sealed, and one signed capacity report per juror and for p1,
    filed before the day's end. `reports`: {miner: (kit, role)} to override."""
    reps = {m: (KIT_MIN, "active") for m in ("p1", "j1", "j2", "j3", "j4")}
    reps.update(reports or {})
    out = [{"type": "payout", "day": 0, "miner_id": "p1", "address": "dendra1payee", "height": 5},
           {"type": "work_sealed", "day": day, "answers": 1, "drawn": 0, "answered": {"p1": ["g1"]}}]
    out += [capacity(m, kit, role, FIRST + 10, day) for m, (kit, role) in reps.items() if kit != "absent"]
    return out


def rank(chain, recs=None, day=KIT_DAY, store="chain", **over):
    firsts = dict(FIRSTS, **over)
    return RK.rank_day(day, records() if recs is None else recs, NODE, GEN, START, 0, FINALITY, chain["tip"], 288,
                       relay_store=chain["store"] if store == "chain" else store, **firsts)


def by_id(out):
    return {r["miner_id"]: r for r in out["ranking"]}


def shift(chain, days):
    """The same facts `days` days earlier or later: every height of the chain moved by that many days."""
    delta = days * DAY
    chain["settled"] = {k: v + delta for k, v in chain["settled"].items()}
    chain["drawn"] = {k: v + delta for k, v in chain["drawn"].items()}
    chain["resolved"] = {k: (h + delta, where) for k, (h, where) in chain["resolved"].items()}
    chain["committed"] = {k: v + delta for k, v in chain["committed"].items()}
    chain["registrations"] = {k: v + delta for k, v in chain["registrations"].items()}
    return chain


def shifted_records(days, **kw):
    """`records` for the day `days` days from KIT_DAY: the seal and the reports move with it."""
    delta = days * DAY
    out = []
    for r in records(**kw):
        r = dict(r)
        if r.get("type") == "work_sealed":
            r["day"] = KIT_DAY + days
        if r.get("type") == F.CAPACITY:
            r["day"], r["height"] = KIT_DAY + days, r["height"] + delta
        out.append(r)
    return out


def test_a_judge_that_meets_the_five_conditions_is_paid_half_a_dndr_once_and_the_ranking_publishes_its_inputs(chain):
    out = rank(chain)
    assert out["rules_fingerprint"] == FINGERPRINT_KIT_3
    rows = by_id(out)
    j1 = rows["j1"]
    assert (j1["verdicts"], j1["judge_active"], j1["probation"], j1["kit_version"]) == (1, 1, False, KIT_MIN)
    assert j1["gross_udndr"] == 16_000 + 500_000 == j1["paid_udndr"] and j1["payout_address"] == "dendra1opj1"
    j3 = rows["j3"]
    assert (j3["verdicts"], j3["judge_active"], j3["gross_udndr"]) == (1, 0, 16_000)   # its vote counts, its day does not
    assert "j2" not in rows                                                           # mute: nothing to rank
    # j4 voted after the resolution, after its reveal: the first half of condition three fails (ADR-060), its
    # verdict is listed and not counted (F3), and with no fact of the day it is not ranked at all
    assert "j4" not in rows
    assert out["inputs"]["verdicts"]["a1"]["jurors"]["j4"] == {"vote": "1", "committed_at": FIRST + 350,
                                                                 "counted": False, "why": F.AFTER_RESOLUTION}
    assert rows["p1"]["verified_requests"] == 1 and rows["p1"]["judge_active"] == 0    # the primary is no juror
    assert all(r["registration_not_dated"] is False for r in out["ranking"])
    jp = out["inputs"]["judge_presence"]
    assert jp["rate_udndr"] == 500_000 and jp["from_day"] == KIT_DAY and jp["relay_store"] == "read"
    assert jp["audits"] == {"a1": {"drawn_at": FIRST + 60, "primary": "p1", "seats": ["j1", "j2", "j3", "j4"],
                                   "resolved_at": FIRST + 300}}
    assert jp["reveals"] == {k: float(block_time(FIRST + 150)) for k in ("a1__j1__p1", "a1__j2__p1", "a1__j3__p1",
                                                                          "a1__j4__p1")}
    # the ranking says what the arrival times are: the service's reading of the store, copied by a replay
    assert jp["reveals_source"] == RK.REVEALS_SOURCE and "without verifying" in jp["reveals_source"]
    assert jp["resolved_this_day"] == {"a1": FIRST + 300} and jp["contradicted"] == []
    ids = jp["identities"]
    assert ids["j1"] == {"audits": {"a1": "ok"}, "job": "a1", "reveal_key": "a1__j1__p1",
                         "reveal_arrival": float(block_time(FIRST + 150)), "commit_height": FIRST + 200,
                         "commit_time": block_time(FIRST + 200), "report_height": FIRST + 10, "judge_role": "active",
                         "kit_version": KIT_MIN, "paid": True}
    assert ids["j2"]["why"] == F.JUDGE_NO_TIMELY_VERDICT and ids["j3"]["why"] == F.JUDGE_VERDICT_BEFORE_REVEAL
    assert ids["j4"]["why"] == F.JUDGE_NO_TIMELY_VERDICT and ids["j4"]["paid"] is False
    assert out["inputs"]["kit_floor"]["kit_min"] == KIT_MIN
    assert out["inputs"]["kit_floor"]["reports"]["j1"] == {"kit_version": KIT_MIN, "judge_role": "active",
                                                           "height": FIRST + 10}
    assert out["inputs"]["probation"] == {"bps": 6_000, "registered_at": {"j1": None, "j3": None, "p1": None},
                                          "first_full_day": {"j1": 0, "j3": 0, "p1": 0}, "not_dated": {}}
    assert START in chain["index"]                                                    # the registrations' index
    # each ranked identity's registration searched once, by its own operator, over the season up to the day's end
    assert sorted(chain["reg_reads"]) == [(m, "dendra1op" + m, START, LAST) for m in ("j1", "j3", "p1")]
    # the same facts, the same bytes: a recompute without the store replays the published arrivals
    again = RK.rank_day(KIT_DAY, records(), NODE, GEN, START, 0, FINALITY, chain["tip"], 288, replay_from=out,
                        **FIRSTS)
    assert again == out


def test_a_day_before_the_set_keeps_its_bytes_and_reads_neither_store_nor_registry(chain, monkeypatch):
    def no_store(*a, **k):
        raise AssertionError("the store was read on a day before the set")
    monkeypatch.setattr(RK, "read_reveals", no_store)
    monkeypatch.setattr(C, "miner_registration", no_store)
    shift(chain, -1)
    with_set = rank(chain, shifted_records(-1), day=KIT_DAY - 1)
    without = rank(chain, shifted_records(-1), day=KIT_DAY - 1, kit_rules_from=None)
    assert with_set == without and with_set["rules_fingerprint"] == PUBLISHED_MORE_REQUESTS
    j1, p1 = by_id(with_set)["j1"], by_id(with_set)["p1"]
    assert j1["gross_udndr"] == 16_000
    assert not {"judge_active", "probation", "kit_version", "registration_not_dated"} & set(j1)
    assert p1["paid_udndr"] == 2 * 4_000 + 100_000
    assert not {"judge_presence", "kit_floor", "probation"} & set(with_set["inputs"])
    assert START not in chain["index"]
    # and it needs no capacity record: the records are read under the set only (the evidence digest moves with
    # the records, the ranking does not)
    bare = rank(chain, [r for r in shifted_records(-1) if r.get("type") != F.CAPACITY], day=KIT_DAY - 1)
    assert bare["ranking"] == with_set["ranking"] and bare["total_paid_udndr"] == with_set["total_paid_udndr"]
    assert {k: v for k, v in bare["inputs"].items() if k != "evidence_sha256"} == {
        k: v for k, v in with_set["inputs"].items() if k != "evidence_sha256"}


def test_a_mute_seat_a_verdict_before_its_reveal_and_a_day_without_a_programme_audit_pay_no_judge(chain):
    # j2 (mute) and j3 (before its reveal) are in the first case; here nobody is seated at all
    chain["jobs"] = [j for j in chain["jobs"] if j["id"] != "a1"]
    out = rank(chain)
    assert all(r["judge_active"] == 0 for r in out["ranking"]) and by_id(out)["p1"]["paid_udndr"] == 2 * 4_000 + 100_000
    jp = out["inputs"]["judge_presence"]
    assert jp["audits"] == {} and jp["identities"] == {} and jp["reveals"] == {} and jp["relay_store"] == "read"


def test_an_audit_drawn_on_another_day_seats_nobody_this_day(chain):
    chain["drawn"]["a1"] = FIRST - 1                      # drawn in the last block of day 9: day 9's seat
    out = rank(chain)
    assert by_id(out)["j1"]["judge_active"] == 0 and out["inputs"]["judge_presence"]["audits"] == {}


def test_a_contradicted_verdict_voids_the_day_the_conviction_concludes_and_a_clean_day_is_paid(chain):
    # a2, settled on day 9, convicted p2 with its resolution inside day 10: j1 voted for p2
    chain["jobs"].append({"id": "a2", "state": CONVICTED, "miner_id": "p2", "client": GEN, "slashed_primary": False,
                          "audit_voters": 2})
    chain["settled"]["a2"], chain["drawn"]["a2"] = FIRST - 500, FIRST - 490
    chain["juries"]["a2"], chain["votes"]["a2"] = ["j1", "j3"], {"j1": "1", "j3": "0"}
    chain["committed"][("a2", "j1")], chain["committed"][("a2", "j3")] = FIRST - 400, FIRST - 400
    chain["resolved"]["a2"] = (FIRST + 400, "end_block")
    out = rank(chain)
    j1 = out["inputs"]["judge_presence"]["identities"]["j1"]
    assert j1["paid"] is False and j1["why"] == F.JUDGE_CONTRADICTED and j1["audits"] == {"a1": "ok"}
    assert by_id(out)["j1"]["judge_active"] == 0 and by_id(out)["j1"]["gross_udndr"] == 16_000
    assert out["inputs"]["judge_presence"]["resolved_this_day"] == {"a1": FIRST + 300, "a2": FIRST + 400}
    assert out["inputs"]["judge_presence"]["contradicted"] == ["j1"]
    assert "a2" not in out["inputs"]["verdicts"]                            # the day's own audits only, as before
    # the conviction concluded the day before: this day is clean, and paid
    chain["resolved"]["a2"] = (FIRST - 10, "end_block")
    out = rank(chain)
    assert by_id(out)["j1"]["judge_active"] == 1 and out["inputs"]["judge_presence"]["contradicted"] == []


@pytest.mark.parametrize("role", ["mute", "off", "unknown", "absent"])
def test_a_report_that_is_not_active_or_absent_pays_no_judge(chain, role):
    recs = records(reports={"j1": ("absent" if role == "absent" else KIT_MIN, role)})
    out = rank(chain, recs)
    j1 = out["inputs"]["judge_presence"]["identities"]["j1"]
    assert j1["paid"] is False and by_id(out)["j1"]["judge_active"] == 0
    assert j1["why"] == (F.JUDGE_NO_SIGNED_REPORT if role == "absent" else F.JUDGE_ROLE_NOT_ACTIVE)
    # and without a signed report the verdict is not paid either: the row's payable is 0 under the floor
    if role == "absent":
        assert by_id(out)["j1"]["gross_udndr"] == 16_000 and by_id(out)["j1"]["paid_udndr"] == 0
        assert by_id(out)["j1"]["kit_version"] is None


def test_a_report_filed_after_the_days_end_does_not_count_for_the_day(chain):
    recs = [r for r in records() if not (r.get("type") == F.CAPACITY and r["miner_id"] == "j1")]
    recs.append(capacity("j1", KIT_MIN, "active", LAST + 1, KIT_DAY + 1))
    out = rank(chain, recs)
    assert out["inputs"]["judge_presence"]["identities"]["j1"]["why"] == F.JUDGE_NO_SIGNED_REPORT
    assert out["inputs"]["kit_floor"]["reports"]["j1"] is None


def test_an_absent_relay_store_pays_no_judge_says_so_and_ranks_the_other_rewards(chain, tmp_path):
    for store in ("", str(tmp_path / "nowhere")):
        out = rank(chain, store=store)
        jp = out["inputs"]["judge_presence"]
        assert jp["relay_store"] == F.STORE_UNREAD and set(jp["reveals"].values()) == {F.STORE_UNREAD}
        assert {p["why"] for p in jp["identities"].values()} == {F.JUDGE_STORE_UNREAD}
        rows = by_id(out)
        assert rows["j1"]["judge_active"] == 0 and rows["j1"]["paid_udndr"] == 16_000
        assert rows["p1"]["paid_udndr"] == 2 * 4_000 + 100_000
    # the day replays byte for byte from what it published, and the store's absence with it
    again = RK.rank_day(KIT_DAY, records(), NODE, GEN, START, 0, FINALITY, chain["tip"], 288, replay_from=out,
                        **FIRSTS)
    assert again == out


def test_a_stale_kit_is_paid_nothing_from_the_sets_first_day_and_the_day_before_pays_it(chain):
    recs = records(reports={"p1": (KIT_MIN - 1, "active")})
    out = rank(chain, recs)
    p1 = by_id(out)["p1"]
    assert p1["gross_udndr"] == 2 * 4_000 + 100_000 and p1["payable_udndr"] == 0 and p1["paid_udndr"] == 0
    assert p1["kit_version"] == KIT_MIN - 1 and "below the floor" in p1["reason"]
    assert out["total_paid_udndr"] == 16_000 + 500_000 + 16_000       # the judges only
    # no signed report at all: not paid either
    none = by_id(rank(chain, records(reports={"p1": ("absent", "")})))["p1"]
    assert none["payable_udndr"] == 0 and none["kit_version"] is None and "no signed capacity report" in none["reason"]
    # a report with no number (a kit older than the field, stored as 0) is below every floor
    zero = by_id(rank(chain, records(reports={"p1": (0, "active")})))["p1"]
    assert zero["payable_udndr"] == 0 and zero["kit_version"] == 0
    # the day before the set pays the same identity in full
    shift(chain, -1)
    before = by_id(rank(chain, shifted_records(-1, reports={"p1": (KIT_MIN - 1, "active")}), day=KIT_DAY - 1))["p1"]
    assert before["paid_udndr"] == 2 * 4_000 + 100_000 and "kit_version" not in before


def test_the_probation_takes_sixty_percent_on_the_first_full_day_only_and_the_boundary_is_the_days_first_block(chain):
    full = 2 * 4_000 + 100_000
    chain["registrations"]["p1"] = FIRST - 1                           # the last block of day 9
    out = rank(chain)
    p1 = by_id(out)["p1"]
    assert p1["probation"] is True and p1["gross_udndr"] == full * 6_000 // 10_000 == p1["paid_udndr"]
    assert "probation" in p1["reason"]
    assert out["inputs"]["probation"]["registered_at"]["p1"] == FIRST - 1
    assert out["inputs"]["probation"]["first_full_day"]["p1"] == KIT_DAY
    # the next day is paid in full
    chain["settled"] = {k: v + DAY for k, v in chain["settled"].items()}
    chain["drawn"]["a1"] += DAY
    chain["resolved"]["a1"] = (FIRST + DAY + 300, "end_block")
    chain["committed"] = {k: v + DAY for k, v in chain["committed"].items()}
    later = [dict(r, day=KIT_DAY + 1) if r.get("type") == "work_sealed" else r for r in records()]
    nxt = by_id(RK.rank_day(KIT_DAY + 1, later, NODE, GEN, START, 0, FINALITY, chain["tip"], 288,
                            relay_store=chain["store"], **FIRSTS))["p1"]
    assert nxt["probation"] is False and nxt["gross_udndr"] == full
    # registered in the first block of day 10: day 10 holds the registration, day 11 is the first full day
    chain["settled"] = {k: v - DAY for k, v in chain["settled"].items()}
    chain["drawn"]["a1"] -= DAY
    chain["resolved"]["a1"] = (FIRST + 300, "end_block")
    chain["committed"] = {k: v - DAY for k, v in chain["committed"].items()}
    chain["registrations"]["p1"] = FIRST
    out = rank(chain)
    assert by_id(out)["p1"]["probation"] is False and out["inputs"]["probation"]["first_full_day"]["p1"] == KIT_DAY + 1
    # a judge in probation: the flat amount is discounted too
    chain["registrations"]["j1"] = FIRST - 5
    j1 = by_id(rank(chain))["j1"]
    assert j1["probation"] is True and j1["gross_udndr"] == (16_000 + 500_000) * 6_000 // 10_000


def test_a_registration_that_cannot_be_dated_pays_nothing_for_the_day_and_the_day_is_ranked(chain):
    full = 2 * 4_000 + 100_000
    chain["flooded"].add("p1")                   # p1's own registrations in the season outnumber what is read
    out = rank(chain)
    p1 = by_id(out)["p1"]
    assert (p1["gross_udndr"], p1["payable_udndr"], p1["paid_udndr"]) == (full, 0, 0)
    assert p1["registration_not_dated"] is True and p1["probation"] is False
    assert p1["reason"] == "registration not datable: not paid"
    pb = out["inputs"]["probation"]
    assert pb["registered_at"]["p1"] == F.NOT_DATED and pb["first_full_day"]["p1"] is None
    assert pb["not_dated"] == {"p1": F.REGISTRATION_SEARCH_TOO_LONG}
    assert by_id(out)["j1"]["paid_udndr"] == 16_000 + 500_000             # the day is ranked, the others paid
    again = RK.rank_day(KIT_DAY, records(), NODE, GEN, START, 0, FINALITY, chain["tip"], 288, replay_from=out,
                        **FIRSTS)
    assert again == out
    # an identity with no operator to search by: work served and answered, no proof that day, gone from the
    # registry, no verdict; its declared payout address does not help, the registration is not dated
    chain["flooded"].clear()
    chain["jobs"].append({"id": "g2", "state": "open+paid+optimistic", "miner_id": "p3", "client": GEN,
                          "slashed_primary": False, "audit_voters": 0})
    chain["settled"]["g2"] = FIRST + 45
    recs = [dict(r, answered={"p1": ["g1"], "p3": ["g2"]}) if r.get("type") == "work_sealed" else r
            for r in records()]
    recs += [{"type": "payout", "day": 0, "miner_id": "p3", "address": "dendra1payee3", "height": 5},
             capacity("p3", KIT_MIN, "off", FIRST + 10, KIT_DAY)]
    out = rank(chain, recs)
    p3 = by_id(out)["p3"]
    assert (p3["verified_requests"], p3["gross_udndr"], p3["payable_udndr"]) == (1, 100_000, 0)
    assert p3["registration_not_dated"] is True and p3["reason"] == "registration not datable: not paid"
    assert out["inputs"]["probation"]["not_dated"] == {"p3": F.REGISTRATION_NO_OPERATOR}
    assert not any(m == "p3" for m, _op, _lo, _hi in chain["reg_reads"])


def test_a_kit_day_with_no_capacity_record_at_or_before_its_end_is_refused_never_published_at_zero(chain):
    bare = [r for r in records() if r.get("type") != F.CAPACITY]
    with pytest.raises(C.ChainUnreadable, match="not watched during the day"):
        rank(chain, bare)
    late = bare + [capacity("j1", KIT_MIN, "active", LAST + 1, KIT_DAY + 1)]      # the first record after the day
    with pytest.raises(C.ChainUnreadable, match="not watched during the day"):
        rank(chain, late)
    # one record at or before the end, of any identity: the registry was watched, the day is ranked, and the
    # identities without a report of their own are payable 0, as the floor says
    one = bare + [capacity("j2", KIT_MIN, "active", LAST, KIT_DAY)]
    out = rank(chain, one)
    assert len(out["ranking"]) == 3 and all(r["payable_udndr"] == 0 and r["kit_version"] is None
                                            for r in out["ranking"])


def test_a_juror_whose_only_fact_of_the_day_is_a_judge_day_is_ranked_and_paid(chain):
    import relay_store
    # a0: a programme audit of p2 settled the day before, within the finality span, whose jury (j5) was drawn today
    # and which resolved today: j5 has no verdict counted on the day's own audits, and one judge day
    chain["jobs"].append({"id": "a0", "state": VINDICATED, "miner_id": "p2", "client": GEN, "slashed_primary": False,
                          "audit_voters": 1})
    chain["settled"]["a0"], chain["drawn"]["a0"] = FIRST - 500, FIRST + 60
    chain["resolved"]["a0"], chain["juries"]["a0"] = (FIRST + 300, "end_block"), ["j5", "p2"]
    chain["votes"]["a0"], chain["committed"][("a0", "j5")] = {"j5": "1"}, FIRST + 200
    relay_store.DiskStore(chain["store"], [F.REVEAL_KIND]).write(F.REVEAL_KIND, "a0__j5__p2", b"sealed",
                                                                  ts=block_time(FIRST + 150))
    out = rank(chain, records(reports={"j5": (KIT_MIN, "active")}))
    j5 = by_id(out)["j5"]
    assert (j5["verdicts"], j5["judge_active"], j5["gross_udndr"], j5["paid_udndr"]) == (0, 1, 500_000, 500_000)
    assert j5["payout_address"] == "dendra1opj5" and j5["probation"] is False
    jp = out["inputs"]["judge_presence"]
    assert jp["identities"]["j5"]["paid"] is True and jp["audits"]["a0"]["primary"] == "p2"
    assert "a0" not in out["inputs"]["verdicts"]                             # the day's own audits only, as before


def test_the_command_line_reads_the_first_day_of_decision_24_and_the_floor_from_the_status(chain, monkeypatch, capsys):
    status = {"unwound_audit_work_from_day": 1, "doubled_rates_from_day": 4, "more_requests_from_day": 6,
              "kit_rules_from_day": KIT_DAY, "rules": R.kit_rules(KIT_MIN)}

    def fetch(url):
        if url == "http://e/status":
            return json.dumps(status).encode()
        if url.endswith(".json"):
            return json.dumps({"total_paid_udndr": 0}).encode()
        raise AssertionError(url)
    monkeypatch.setattr(RK, "_fetch", fetch)
    monkeypatch.setattr(RK, "_records", lambda base, day: records())
    monkeypatch.setattr(C, "jobs_params", lambda rest: {"audit_unwind_blocks": "800", "audit_resolve_timeout": "0",
                                                        "avail_epoch_blocks": "288"})
    monkeypatch.setattr(C, "height", lambda rpc: chain["tip"])
    monkeypatch.setattr(RK, "finality_blocks", lambda params, rules: FINALITY)
    rc = RK.main(["--day", str(KIT_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START), "--relay-store", chain["store"]])
    out = capsys.readouterr().out
    assert rc == 0, out
    got = json.loads(out)
    assert got["rules_fingerprint"] == FINGERPRINT_KIT_3 and by_id(got)["j1"]["judge_active"] == 1
    # --kit-min against the floor the process already carries: refused with both numbers, never ignored; the
    # same number is accepted
    rc = RK.main(["--day", str(KIT_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START), "--kit-min", str(KIT_MIN + 1), "--relay-store", chain["store"]])
    out = capsys.readouterr().out
    assert rc == 2 and out.startswith("REFUSED:") and f"--kit-min {KIT_MIN + 1}" in out and f"({KIT_MIN}," in out
    rc = RK.main(["--day", str(KIT_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START), "--kit-min", str(KIT_MIN), "--relay-store", chain["store"]])
    assert rc == 0 and json.loads(capsys.readouterr().out)["rules_fingerprint"] == FINGERPRINT_KIT_3
    assert RK.kit_rules_from_status(status) == KIT_DAY and RK.kit_min_from_status(status) == KIT_MIN
    assert RK.kit_rules_from_status({}) is None and RK.kit_min_from_status({"rules": R.RULES}) is None
    for bad in ("10", -1, True):
        with pytest.raises(ValueError, match="kit_rules_from_day"):
            RK.kit_rules_from_status(dict(status, kit_rules_from_day=bad))
    with pytest.raises(ValueError, match="kit_min"):
        RK.kit_min_from_status({"rules": dict(R.RULES, kit_min=0)})
    # the floor missing everywhere: refused, and said
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    status["rules"] = R.RULES_MORE_REQUESTS
    rc = RK.main(["--day", str(KIT_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START)])
    out = capsys.readouterr().out
    assert rc == 2 and out.startswith("REFUSED:") and "DENDRA_KIT_MIN" in out
    # given on the command line: the set exists again
    rc = RK.main(["--day", str(KIT_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START), "--kit-min", str(KIT_MIN), "--relay-store", chain["store"]])
    assert rc == 0 and json.loads(capsys.readouterr().out)["rules_fingerprint"] == FINGERPRINT_KIT_3


# -- the payment: mixed weeks, the floor from the ranking, no return -------------------------------------------------
def _addresses():
    from modea import cosmos_addr
    return [cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5)) for n in (1, 2)]


def _season(path, sets, requests=300, judge=()):
    a, b = _addresses()
    paid = 0
    for d, rules in enumerate(sets):
        kit = R.pays_judge_presence(rules)
        rows = [Identity("m1", verified_requests=requests, payout_address=a, kit_version=KIT_MIN if kit else None,
                         judge_active=1 if (kit and d in judge) else 0),
                Identity("m2", verified_requests=requests, payout_address=b, kit_version=KIT_MIN if kit else None)]
        r = compute_day(d, rows, paid, rules)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": (d + 1) * DAY, "season_end_height": None}
        if kit:
            out["inputs"]["kit_floor"] = {"kit_min": rules["kit_min"], "reports": {}}
        (path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def _owner_sets(n, kit_from=KIT_DAY):
    return [R.rules_for_day(d, 1, 4, 6, kit_from) for d in range(n)]


def test_a_week_that_mixes_decision_23_and_the_kit_set_is_planned_with_the_payers_one_argument(tmp_path):
    import final_season_payout as P
    _floor()
    _season(tmp_path, _owner_sets(14), judge=(10, 12))
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    assert loaded[10].kit_min == KIT_MIN and loaded[9].kit_min is None
    assert loaded[10].rows[0].judge_active == 1 and loaded[10].rows[0].kit_version == KIT_MIN
    assert P.check_days(loaded) == "" and P.check_doubled_from(loaded, 4) == ""
    assert P.doubled_sets() == (R.RULES_DOUBLED, R.RULES_MORE_REQUESTS, R.RULES_KIT)
    weeks = [sum(P.plan(w, str(tmp_path), 4)[0].values()) for w in range(2)]
    # 300 requests each: 30 DNDR a day at the first rates, 60 doubled; two judge days of m1 at 0.5 DNDR
    assert weeks == [(4 * 30 + 3 * 60) * UDNDR, 7 * 60 * UDNDR + 2 * 500_000]
    for w in ("0", "1"):
        assert P.main(["--week", w, "--rankings", str(tmp_path), "--doubled-from-day", "4"]) == 0
    with pytest.raises(SystemExit):
        P.main(["--week", "1", "--rankings", str(tmp_path), "--doubled-from-day", "4", "--kit-rules-from-day", "10"])
    # the kit set's days count as doubled: a payer naming another doubled day is refused
    assert P.check_doubled_from(loaded, 10).startswith("day 4: ranked under the doubled rates, but the payer names day 10")


def test_the_payer_without_a_floor_recovers_it_from_the_published_day_and_holds_every_later_day_to_it(tmp_path,
                                                                                                        monkeypatch):
    import final_season_payout as P
    _floor()
    _season(tmp_path, _owner_sets(14))
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    assert P.check_days(loaded) == "" and R.KIT_MIN == KIT_MIN                  # the floor came from day 10
    assert P.check_doubled_from(loaded, 4) == ""
    # a published floor that is not the one the day's fingerprint carries is refused: known floor or not
    other = tmp_path / "other"
    other.mkdir()
    _season(other, _owner_sets(11))
    doc = json.loads((other / "day-010.json").read_text(encoding="utf-8"))
    doc["inputs"]["kit_floor"]["kit_min"] = KIT_MIN + 1
    (other / "day-010.json").write_text(json.dumps(doc), encoding="utf-8")
    bad = P.check_days([P.load_day(str(other), d) for d in range(11)])
    assert bad.startswith("day 10: publishes kit floor 4, but its rules carry 3")
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    bad = P.check_days([P.load_day(str(other), d) for d in range(11)])
    assert bad.startswith("day 10: ranked under rules") and "none of the season's" in bad and R.KIT_MIN is None
    # a day under the set that publishes no floor is none of the season's for a payer without one
    del doc["inputs"]["kit_floor"]
    (other / "day-010.json").write_text(json.dumps(doc), encoding="utf-8")
    bad = P.check_days([P.load_day(str(other), d) for d in range(11)])
    assert bad.startswith("day 10: ranked under rules") and "none of the season's" in bad


def test_a_return_to_decision_23_after_a_kit_day_is_refused_and_so_is_a_kit_field_under_an_earlier_set(tmp_path):
    import final_season_payout as P
    _floor()
    sets = _owner_sets(12) + [R.RULES_MORE_REQUESTS, R.RULES_MORE_REQUESTS]   # day 12 goes back
    _season(tmp_path, sets)
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    bad = P.check_days(loaded)
    assert bad.startswith("day 12: ranked under rules") and "earlier than the rules of a day before it" in bad
    with pytest.raises(SystemExit, match="Week 1 is not paid"):
        P.plan(1, str(tmp_path), 4)
    # a row that carries a judge day, a probation or a kit under decision 23 was not computed under it
    early = tmp_path / "early"
    early.mkdir()
    _season(early, _owner_sets(10, kit_from=None))
    doc = json.loads((early / "day-009.json").read_text(encoding="utf-8"))
    for field, value in (("judge_active", 1), ("probation", True), ("kit_version", KIT_MIN),
                         ("registration_not_dated", True)):
        doc["ranking"][0][field] = value
        (early / "day-009.json").write_text(json.dumps(doc), encoding="utf-8")
        bad = P.check_days([P.load_day(str(early), d) for d in range(10)])
        assert bad.startswith("day 9:") and "under a set that has none" in bad, field
        del doc["ranking"][0][field]
    # under the kit set, the stale kit's row pays 0 and a payer recomputes it so
    kitday = tmp_path / "kit"
    kitday.mkdir()
    _season(kitday, _owner_sets(11))
    doc = json.loads((kitday / "day-010.json").read_text(encoding="utf-8"))
    doc["ranking"][0]["kit_version"] = KIT_MIN - 1
    (kitday / "day-010.json").write_text(json.dumps(doc), encoding="utf-8")
    bad = P.check_days([P.load_day(str(kitday), d) for d in range(11)])
    assert "payable" in bad and ", it is 0" in bad


def test_the_payer_accepts_a_judge_term_shared_pro_rata_and_refuses_a_flag_that_is_a_number(tmp_path):
    import final_season_payout as P
    _floor()
    # 600 requests each: every day is over its budget, and day 10 holds a judge day in the same pro rata
    _season(tmp_path, _owner_sets(14), requests=600, judge=(10,))
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    assert P.check_days(loaded) == "" and P.check_doubled_from(loaded, 4) == ""
    ten = loaded[10]
    assert ten.total_payable > ten.day_budget == 100 * UDNDR and ten.total_paid <= ten.day_budget
    m1 = next(r for r in ten.rows if r.miner_id == "m1")
    assert m1.judge_active == 1 and m1.payable == 600 * 100_000 + 500_000 and m1.paid < m1.payable
    assert all(d.total_paid <= d.day_budget for d in loaded)
    assert sum(P.plan(1, str(tmp_path), 4)[0].values()) == sum(d.total_paid for d in loaded[7:14])
    # a probation, or an undated registration, published as 1: refused as malformed, though 1 == True in Python
    doc = json.loads((tmp_path / "day-010.json").read_text(encoding="utf-8"))
    for field in ("probation", "registration_not_dated"):
        doc["ranking"][0][field] = 1
        (tmp_path / "day-010.json").write_text(json.dumps(doc), encoding="utf-8")
        bad = P.check_days([P.load_day(str(tmp_path), d) for d in range(11)])
        assert bad.startswith("day 10:") and "not well formed" in bad, field
        doc["ranking"][0][field] = False
    # an undated registration: the payer recomputes the row's payable to 0
    doc["ranking"][0]["registration_not_dated"] = True
    (tmp_path / "day-010.json").write_text(json.dumps(doc), encoding="utf-8")
    bad = P.check_days([P.load_day(str(tmp_path), d) for d in range(11)])
    assert bad.startswith("day 10:") and "payable" in bad and ", it is 0" in bad


# -- the generator: the day's number under the kit set, the floor bound by the fingerprint --------------------------
def test_the_generator_takes_the_kit_sets_number_once_the_status_fingerprint_binds_the_floor(monkeypatch):
    import final_season_generator as G
    _floor()
    kit = {"rules_fingerprint": FINGERPRINT_KIT_3, "rules": {"kit_min": KIT_MIN, "requests_per_day": 10 ** 6}}
    assert G.day_quota(kit) == 900                                        # the set's number, never the status's
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    # a floor the fingerprint does not bind, or none named: unknown, nothing fixed, nothing sent
    assert G.day_quota(dict(kit, rules={"kit_min": KIT_MIN + 1})) is None and R.KIT_MIN is None
    assert G.day_quota(dict(kit, rules={"kit_min": "3"})) is None and G.day_quota(dict(kit, rules=None)) is None
    assert G.day_quota({"rules_fingerprint": FINGERPRINT_KIT_3}) is None and R.KIT_MIN is None
    # the floor the fingerprint binds: the set is known from then on
    assert G.day_quota(kit) == 900 and R.KIT_MIN == KIT_MIN and R.RULE_HISTORY[-1] == R.kit_rules(KIT_MIN)
    assert G.day_quota({"rules_fingerprint": FINGERPRINT_KIT_3}) == 900
    assert G.day_quota({"rules_fingerprint": PUBLISHED_MORE_REQUESTS}) == 900


# -- the service fixes the set's day once, with its floor ------------------------------------------------------------
def _service():
    import final_season_server as S
    return S


def _ranked(data, d, rules):
    os.makedirs(os.path.join(data, "ranking"), exist_ok=True)
    with open(os.path.join(data, "ranking", f"day-{d:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"day": d, "rules_fingerprint": R.fingerprint(rules), "total_paid_udndr": 0}, f)


def _registry(path, reports):
    """A capacity registry store as capacity_server.py keeps it: signed reports under `signed::<miner>`."""
    store = {}
    for mid, (kit, role) in reports.items():
        rep = {"node_id": "n" + mid, "_proven": mid, "judge_role": role, "ts": 1_700_000_000}
        if kit is not None:
            rep["kit_version"] = kit
        store["signed::" + mid] = rep
    store["decl::machine::node"] = {"node_id": "node", "_proven": "", "judge_role": "active", "kit_version": 9}
    store["signed::forged"] = {"node_id": "n", "_proven": "someone_else", "judge_role": "active", "kit_version": 9}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f)
    return str(path)


def _live(data, monkeypatch, tmp_path):
    """The owner's service: day 0 ranked under RULES_17, then decision 18 fixed from day 1, the doubled rates from
    day 4 and decision 23 from day 6, days 1 to 9 ranked each under its set; the registry and the relay store named."""
    S = _service()
    _ranked(data, 0, R.RULES_17)
    monkeypatch.setattr(S, "CAPACITY_DB", _registry(tmp_path / "capacity.json", {"m1": (KIT_MIN, "active")}))
    monkeypatch.setattr(S, "RELAY_STORE", str(tmp_path / "relay-store"))
    # the chain is in day 9: a start that fixes day 10 reads it (`State._kit_rules_from`) and finds it not begun
    monkeypatch.setattr(C, "height", lambda rpc: DAY_9_HEIGHT)
    st = S.State(data, 1, "", "4", "6", "none")
    assert (st.unwound_from, st.doubled_from, st.more_requests_from, st.kit_rules_from) == (1, 4, 6, None)
    for d in (1, 2, 3):
        _ranked(data, d, R.RULES)
    for d in (4, 5):
        _ranked(data, d, R.RULES_DOUBLED)
    for d in (6, 7, 8, 9):
        _ranked(data, d, R.RULES_MORE_REQUESTS)
    return S


DAY_9_HEIGHT = START + 9 * DAY + 100


def _kit_file(data):
    with open(os.path.join(data, "kit-rules.json"), encoding="utf-8") as f:
        return json.load(f)


def test_the_floor_is_required_with_the_day_recorded_with_it_and_the_record_wins_at_every_later_start(tmp_path,
                                                                                                        monkeypatch,
                                                                                                        capsys):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    # no floor in the environment: a day cannot be fixed, and nothing is written
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    with pytest.raises(SystemExit, match="DENDRA_KIT_MIN is required"):
        S.State(data, 1, "", "", "", "10")
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    # `none` needs no floor, and absent with no record is refused as for the sets before
    assert S.State(data, 1, "", "", "", "none").kit_rules_from is None
    with pytest.raises(SystemExit, match="DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY is not set and no earlier"):
        S.State(data, 1, "", "", "", "")
    # with the floor: fixed, recorded with it, said
    R.set_kit_min(KIT_MIN)
    st = S.State(data, 1, "", "", "", "10")
    assert st.kit_rules_from == 10
    assert _kit_file(data) == {"from_day": 10, "set_by": "parameter", "first_unranked_day": 10, "kit_min": KIT_MIN,
                               "fixed_at_height": DAY_9_HEIGHT, "rules_fingerprint": FINGERPRINT_KIT_3,
                               "rules_fingerprint_before": PUBLISHED_MORE_REQUESTS}
    assert f"decision 24 (judge presence reward, kit floor {KIT_MIN}, probation) apply from day 10" in capsys.readouterr().out
    # a later start without the floor reads it from the record; another floor is refused; the same one accepted
    monkeypatch.setattr(R, "KIT_MIN", None)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    assert S.State(data, 1, "", "", "", "").kit_rules_from == 10 and R.KIT_MIN == KIT_MIN
    monkeypatch.setattr(R, "KIT_MIN", KIT_MIN + 1)
    with pytest.raises(SystemExit, match="names another one"):
        S.State(data, 1, "", "", "", "10")
    monkeypatch.setattr(R, "KIT_MIN", KIT_MIN)
    monkeypatch.setattr(R, "RULES_KIT", R.kit_rules(KIT_MIN))
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS + (R.RULES_KIT,))
    assert S.State(data, 1, "", "", "", "10").kit_rules_from == 10
    with pytest.raises(SystemExit, match="names another day"):
        S.State(data, 1, "", "", "", "11")
    with pytest.raises(SystemExit, match="already apply from day 10"):
        S.State(data, 1, "", "", "", "none")
    assert _kit_file(data)["from_day"] == 10


def test_a_day_already_ranked_before_decision_23_or_without_it_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _floor()
    for bad in ("0", "5", "9"):
        with pytest.raises(SystemExit, match="already ranked"):
            S.State(data, 1, "", "", "", bad)
    for bad in ("x", "-1", "10.0", "None"):
        with pytest.raises(SystemExit, match="neither a day number nor"):
            S.State(data, 1, "", "", "", bad)
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    for more, kit, said in (("6", "5", "from day 6"), ("none", "10", "fixed for no day")):
        fresh = str(tmp_path / ("f" + more))
        os.makedirs(fresh)
        with pytest.raises(SystemExit, match="would take effect before decision 23") as e:
            S.State(fresh, 1, "1", "4", more, kit)
        assert said in str(e.value)
        assert not os.path.exists(os.path.join(fresh, "kit-rules.json"))
    # a published day under the kit set before its first day, or a later day under the set before: refused
    _ranked(data, 10, R.RULES_KIT)
    with pytest.raises(SystemExit, match="day 10 is published under rules"):
        S.State(data, 1, "", "", "", "11")
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))


def test_the_first_day_of_the_kit_set_must_not_have_begun_at_the_start_that_fixes_it(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _floor()
    # the chain is in day 11 and day 10 is over, unranked: fixed now, day 10 would be ranked on records filed from
    # now on, none at or before its end, every identity payable 0 -- refused, and nothing written
    monkeypatch.setattr(C, "height", lambda rpc: START + 11 * DAY + 5)
    with pytest.raises(SystemExit, match="names a day that has begun") as e:
        S.State(data, 1, "", "", "", "10")
    assert "day 11" in str(e.value) and "Name day 12 or later" in str(e.value)
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    # the chain is in day 10: day 10 has begun, refused; day 11 has not, fixed, with the height it was fixed at
    monkeypatch.setattr(C, "height", lambda rpc: START + 10 * DAY + 5)
    with pytest.raises(SystemExit, match="names a day that has begun"):
        S.State(data, 1, "", "", "", "10")
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    # a chain that does not answer: the day is not fixed, since not read is not "not begun"
    def unreadable(rpc):
        raise C.ChainUnreadable("no answer")
    monkeypatch.setattr(C, "height", unreadable)
    with pytest.raises(SystemExit, match="reads the chain's height .* and cannot"):
        S.State(data, 1, "", "", "", "11")
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    monkeypatch.setattr(C, "height", lambda rpc: START + 10 * DAY + 5)
    assert S.State(data, 1, "", "", "", "11").kit_rules_from == 11
    assert _kit_file(data)["fixed_at_height"] == START + 10 * DAY + 5
    # a later start reads the record and no height
    monkeypatch.setattr(C, "height", unreadable)
    assert S.State(data, 1, "", "", "", "").kit_rules_from == 11


def test_the_stores_are_required_with_the_day_and_the_registry_must_read_at_the_fixing_start(tmp_path, monkeypatch,
                                                                                               capsys):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _floor()
    monkeypatch.setattr(S, "RELAY_STORE", "")
    with pytest.raises(SystemExit, match="DENDRA_FINAL_SEASON_RELAY_STORE"):
        S.State(data, 1, "", "", "", "10")
    monkeypatch.setattr(S, "RELAY_STORE", str(tmp_path / "relay-store"))
    monkeypatch.setattr(S, "CAPACITY_DB", str(tmp_path / "no-registry.json"))
    with pytest.raises(SystemExit, match="capacity registry at .* cannot be read"):
        S.State(data, 1, "", "", "", "10")
    assert not os.path.exists(os.path.join(data, "kit-rules.json"))
    monkeypatch.setattr(S, "CAPACITY_DB", _registry(tmp_path / "capacity.json", {"m1": (KIT_MIN, "active")}))
    assert S.State(data, 1, "", "", "", "10").kit_rules_from == 10
    # once fixed, an unreadable registry at a later start is a warning: the day stands, the watcher retries
    monkeypatch.setattr(S, "CAPACITY_DB", str(tmp_path / "no-registry.json"))
    capsys.readouterr()
    assert S.State(data, 1, "", "", "", "").kit_rules_from == 10
    assert "WARNING: the capacity registry" in capsys.readouterr().out


def test_the_registry_reader_takes_the_signed_reports_and_the_watcher_files_each_change_once(tmp_path, monkeypatch):
    S = _service()
    path = _registry(tmp_path / "capacity.json", {"m1": (KIT_MIN, "active"), "m2": (None, "off"), "m3": (2, "mute")})
    got = S.read_capacity_registry(path)
    assert got == {"m1": (KIT_MIN, "active", 1_700_000_000), "m2": (None, "off", 1_700_000_000),
                   "m3": (2, "mute", 1_700_000_000)}                      # no declared row, no forged key
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        S.read_capacity_registry(str(tmp_path / "list.json"))
    with pytest.raises(OSError):
        S.read_capacity_registry(str(tmp_path / "absent.json"))
    data = str(tmp_path / "data")
    _live(data, monkeypatch, tmp_path)          # writes its own registry at the same path: rewritten below
    _floor()
    _registry(path, {"m1": (KIT_MIN, "active"), "m2": (None, "off"), "m3": (2, "mute")})
    monkeypatch.setattr(S, "CAPACITY_DB", path)
    st = S.State(data, 1, "", "", "", "10")
    monkeypatch.setattr(S, "ST", st)
    st.height = FIRST + 10
    assert S.file_capacity_changes(S.read_capacity_registry(path), st.height, 10) == 3
    assert S.file_capacity_changes(S.read_capacity_registry(path), st.height + 5, 10) == 0   # nothing changed
    _registry(path, {"m1": (KIT_MIN, "mute"), "m2": (None, "off"), "m3": (2, "mute")})
    assert S.file_capacity_changes(S.read_capacity_registry(path), st.height + 9, 10) == 1
    recs = [r for r in st.ev.read(10) if r["type"] == F.CAPACITY]
    assert [(r["miner_id"], r["kit_version"], r["judge_role"], r["height"]) for r in recs] == [
        ("m1", KIT_MIN, "active", FIRST + 10), ("m2", None, "off", FIRST + 10), ("m3", 2, "mute", FIRST + 10),
        ("m1", KIT_MIN, "mute", FIRST + 19)]
    assert all(r["reported_ts"] == 1_700_000_000 for r in recs)
    # what the ranking reads from them, and a restart rebuilds the watcher's memory from the evidence
    assert F.kit_report(st.ev.read(10), "m1", FIRST + 15) == {"kit_version": KIT_MIN, "judge_role": "active",
                                                                "height": FIRST + 10}
    assert F.kit_report(st.ev.read(10), "m1", FIRST + 19)["judge_role"] == "mute"
    again = S.State(data, 1, "", "", "", "")
    assert again.capacity == {"m1": (KIT_MIN, "mute"), "m2": (None, "off"), "m3": (2, "mute")}
    # the watcher loop: dormant without a day, one pass files the changes
    monkeypatch.setattr(S, "ST", again)
    again.height = FIRST + 30

    class Stop(BaseException):
        pass
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > 1:
            raise Stop
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    _registry(path, {"m1": (KIT_MIN, "active"), "m2": (None, "off"), "m3": (2, "mute")})
    with pytest.raises(Stop):
        S.watch_capacity()
    assert again.capacity["m1"] == (KIT_MIN, "active")
    assert [r for r in again.ev.read(10) if r["type"] == F.CAPACITY][-1]["height"] == FIRST + 30


def test_the_watcher_files_a_record_under_the_day_of_the_one_height_it_read(tmp_path, monkeypatch):
    S = _service()
    data = str(tmp_path / "data")
    _live(data, monkeypatch, tmp_path)
    _floor()
    path = _registry(tmp_path / "capacity.json", {"m1": (KIT_MIN, "active")})
    monkeypatch.setattr(S, "CAPACITY_DB", path)
    st = S.State(data, 1, "", "", "", "10")
    # `watch_chain` advances the height while the watcher runs: here the chain crosses from the last block of day
    # 10 into day 11 between two reads. A record of day 10's last block filed under day 11 is not read by the
    # ranking of day 10 (`ranking_inputs` reads the files of the days up to 10).
    heights = [LAST, LAST, LAST + 1]

    class Moving:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def height(self):
            return heights.pop(0) if len(heights) > 1 else heights[0]

        def day_now(self):
            return R.day_of_height(self.height, self.start)

    monkeypatch.setattr(S, "ST", Moving(st))

    class Stop(BaseException):
        pass
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > 1:
            raise Stop
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    with pytest.raises(Stop):
        S.watch_capacity()
    recs = [r for r in st.ev.read(KIT_DAY) if r["type"] == F.CAPACITY]
    assert [(r["miner_id"], r["height"]) for r in recs] == [("m1", LAST)]
    assert not [r for r in st.ev.read(KIT_DAY + 1) if r["type"] == F.CAPACITY]
    assert F.kit_report(st.ev.read(KIT_DAY), "m1", LAST) == {"kit_version": KIT_MIN, "judge_role": "active",
                                                               "height": LAST}


def test_the_status_names_the_first_day_of_decision_24_and_the_loop_hands_it_with_the_store(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _floor()
    st = S.State(data, 1, "", "", "", "10")
    monkeypatch.setattr(S, "ST", st)
    st.latest_time = S.END - 10 * 86400
    for day, rules in ((9, R.RULES_MORE_REQUESTS), (10, R.RULES_KIT), (20, R.RULES_KIT)):
        st.height = 1 + day * DAY + 5
        got = S.Handler._status(None)
        assert got["day"] == day and got["rules"] == rules and got["kit_rules_from_day"] == 10
        assert got["rules_fingerprint"] == R.fingerprint(rules)
    assert got["rules"]["kit_min"] == KIT_MIN
    seen = []

    def rank_day(d, records, *a, **k):
        seen.append((d, k))
        raise ValueError("not final")

    class Stop(BaseException):
        pass
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > 1:
            raise Stop
    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    with pytest.raises(Stop):
        S.rank_days()
    assert seen == [(10, {"unwound_from": 1, "doubled_from": 4, "more_requests_from": 6, "kit_rules_from": 10,
                          "relay_store": str(tmp_path / "relay-store")})]


# -- the faucet refuses a stale kit from the deadline --------------------------------------------------------------
class _Req(object):
    def __init__(self, F_, body, ip="203.0.113.4"):
        b = json.dumps(body).encode()
        self.headers = {"Content-Length": str(len(b))}
        self.rfile = io.BytesIO(b)
        self.client_address = (ip, 0)
        self.out = []
        self.do_POST = types.MethodType(F_.H.do_POST, self)
        self.do_GET = types.MethodType(F_.H.do_GET, self)

    def _send(self, code, obj):
        self.out.append((code, obj))


@pytest.fixture()
def faucet(monkeypatch):
    import faucet as FD
    monkeypatch.setattr(FD, "POW_BITS", 0)
    monkeypatch.setattr(FD, "STATE_FILE", "")
    monkeypatch.setattr(FD, "drip_amount", lambda read=True, now=None: (1_000_000, "bench"))
    monkeypatch.setattr(FD, "fund", lambda addr, amount=None: (True, "sent"))
    monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip: (True, ""))
    monkeypatch.setattr(FD, "_rate_refund", lambda addr: None)
    monkeypatch.setattr(FD, "_save_state", lambda: None)
    FD._STALE_REFUSED[0] = 0
    return FD


ADDR = "dendra1" + "q" * 38


def _post(FD, body):
    h = _Req(FD, dict(body, address=ADDR))
    h.do_POST()
    return h.out[0]


def test_the_faucet_refuses_a_stale_or_undeclared_kit_from_the_deadline_before_any_quota(faucet, monkeypatch, capsys):
    FD = faucet
    deadline = time.time() + 3_600
    monkeypatch.setattr(FD, "KIT_FLOOR", KIT_MIN)
    monkeypatch.setattr(FD, "KIT_DEADLINE_TS", deadline)
    # before the deadline: every kit is served, declared or not
    assert _post(FD, {})[0] == 200 and _post(FD, {"kit_version": 1})[0] == 200
    assert FD.kit_floor_now() is None and FD.kit_floor_status()[0] == "announced"
    # from the deadline: below the floor, none, or not a number is refused with the floor named; at or above served
    monkeypatch.setattr(FD, "KIT_DEADLINE_TS", time.time() - 1)
    assert FD.kit_floor_now() == KIT_MIN and FD.kit_floor_status()[0] == "armed"

    def no_quota(addr, ip):
        raise AssertionError("the quota was consulted for a refused kit")
    monkeypatch.setattr(FD, "_rate_ok", no_quota)
    monkeypatch.setattr(FD, "_pow_ok", no_quota)
    for body in ({}, {"kit_version": KIT_MIN - 1}, {"kit_version": 0}, {"kit_version": "3"}, {"kit_version": True},
                 {"kit_version": None}, {"kit_version": 3.0}):
        code, reply = _post(FD, body)
        assert code == 403 and reply["error"] == "stale kit" and reply["kit_min"] == KIT_MIN, body
        assert reply["info"] == FD.REFUSAL_STALE_KIT % KIT_MIN and "update" in reply["info"]
        assert reply["declared"] == (body.get("kit_version") if type(body.get("kit_version")) is int and
                                     not isinstance(body.get("kit_version"), bool) else None)
    assert FD._STALE_REFUSED[0] == 7 and capsys.readouterr().out.count("refused: kit_version") == 7
    monkeypatch.setattr(FD, "_rate_ok", lambda addr, ip: (True, ""))
    monkeypatch.setattr(FD, "_pow_ok", lambda addr, pow_str: True)
    assert _post(FD, {"kit_version": KIT_MIN})[0] == 200 and _post(FD, {"kit_version": KIT_MIN + 5})[0] == 200
    probe = _Req(FD, {})
    probe.do_GET()
    assert probe.out[0][1]["kit_floor"] == {"state": "armed", "kit_min": KIT_MIN, "deadline_ts": FD.KIT_DEADLINE_TS,
                                            "field": "kit_version"}


def test_the_faucets_floor_is_dormant_when_unset_and_a_half_configuration_is_an_error(faucet, monkeypatch):
    FD = faucet
    monkeypatch.setattr(FD, "KIT_FLOOR", None)
    monkeypatch.setattr(FD, "KIT_DEADLINE_TS", None)
    assert FD.kit_floor_now() is None and FD.kit_floor_status()[0] == "dormant"
    assert _post(FD, {})[0] == 200 and _post(FD, {"kit_version": 0})[0] == 200
    assert FD.kit_policy_from_env("", "") == (None, None, None) and FD.kit_policy_from_env(None, None)[2] is None
    floor, ts, err = FD.kit_policy_from_env("3", "2026-10-23T12:00:00Z")
    assert (floor, err) == (3, None) and ts == 1_792_756_800.0
    for a, b in (("3", ""), ("", "2026-10-23T12:00:00Z"), ("0", "2026-10-23T12:00:00Z"), ("x", "2026-10-23T12:00:00Z"),
                 ("3", "2026-10-23 12:00:00"), ("3", "2026-10-23T12:00:00+00:00")):
        assert FD.kit_policy_from_env(a, b)[2], (a, b)
    assert FD.declared_kit_version({"kit_version": 4}) == 4
    for bad in ({}, {"kit_version": "4"}, {"kit_version": True}, {"kit_version": -1}, [], None):
        assert FD.declared_kit_version(bad) is None
