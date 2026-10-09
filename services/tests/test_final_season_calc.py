"""Bench of the Final Testnet Season formula (ADR-047). Each case names the rule it pins; the order of the
rules is pinned too (an address before the pro rata), because changing it changes who is paid."""
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from final_season_calc import Identity, compute_day, gross_of, to_json, week_totals  # noqa: E402
from final_season_rules import RULES, UDNDR, fingerprint  # noqa: E402

PRESENCE_NOT_PAID = "presence not paid: no verified request counted that day"
FULL_DAY = 50 * 2_000 + 50_000       # the default identity below: 50 windows proven, 1 verified request


def ident(mid, **kw):
    # The ordinary case: online all day, one verified request. Each case states what it changes.
    kw.setdefault("presence", 50)
    kw.setdefault("verified_requests", 1)
    kw.setdefault("payout_address", "addr-" + mid)
    return Identity(mid, **kw)


def test_published_rates():
    # The numbers ADR-047 and the texts announce (owner's decision of 2026-10-06, every rate and cap divided
    # by ten): 0.05 DNDR per verified request, 0.008 per verdict, 0.002 per window up to 50, 50 per day and
    # 1 500 over the season for the whole programme, and no other cap.
    assert RULES["work_per_request"] == 50_000
    assert RULES["juror_per_verdict"] == 8_000
    assert RULES["presence_per_window"] == 2_000
    assert RULES["presence_max_windows"] == 50
    assert RULES["cap_programme_day"] == 50 * UDNDR
    assert RULES["cap_season"] == 1_500 * UDNDR
    assert sorted(k for k in RULES if k.startswith("cap_")) == ["cap_programme_day", "cap_season"]


def test_formula_on_a_full_day():
    # 50 windows, 6 verified requests, 2 verdicts: 0.1 + 0.3 + 0.016 = 0.416 DNDR.
    assert gross_of(ident("m", verified_requests=6, verdicts=2)) == 50 * 2_000 + 6 * 50_000 + 2 * 8_000
    assert gross_of(ident("m", verified_requests=6, verdicts=2)) == 416_000


def test_juror_rate():
    assert gross_of(ident("m", verdicts=3)) == FULL_DAY + 3 * 8_000


def test_work_rate_is_flat_whatever_the_presence():
    # 0.05 DNDR per verified request, the same for every miner: the presence does not scale it, and there is
    # no model class to multiply it.
    for presence in (0, 1, 25, 50, 288):
        expected = min(presence, 50) * 2_000 + 6 * 50_000
        assert gross_of(ident("m", presence=presence, verified_requests=6)) == expected, presence
    # The rate is read from the rules, not restated in the calculation.
    assert gross_of(ident("m", verified_requests=6), dict(RULES, work_per_request=7)) == 50 * 2_000 + 6 * 7
    assert not [k for k in RULES if "class" in k]
    with pytest.raises(TypeError):
        Identity("m", model_class=2)


def test_work_and_verdicts_are_paid_without_any_proven_window():
    # There is no rule "no proven window, no reward": verified work and verdicts are chain facts of their own.
    row = compute_day(0, [ident("m", presence=0, verified_requests=6, verdicts=2)], 0).rows[0]
    assert row.paid == 6 * 50_000 + 2 * 8_000
    assert row.reason == ""


def test_presence_is_paid_only_on_a_day_with_verified_work():
    # Proving a window needs an operator key and a VRF key, no model: the verified request of the same day
    # is what shows a model answered. A day voided by the work grading reaches this module with
    # verified_requests = 0 (`final_season_facts.py::day_identities`), so it loses its presence too.
    row = compute_day(0, [ident("m", presence=50, verified_requests=0, verdicts=3)], 0).rows[0]
    assert row.gross == row.payable == row.paid == 3 * 8_000     # the verdicts are still paid
    assert row.reason == PRESENCE_NOT_PAID
    assert row.presence == 50                                    # the row still shows what was proven
    assert gross_of(ident("m", presence=50, verified_requests=0)) == 0
    assert gross_of(ident("m", presence=50, verified_requests=1)) == FULL_DAY
    # The reason is given only when there was a presence to lose.
    idle = compute_day(0, [ident("m", presence=0, verified_requests=0, verdicts=3)], 0).rows[0]
    assert idle.paid == 3 * 8_000 and idle.reason == ""


def test_presence_is_capped_at_presence_max_windows():
    assert gross_of(ident("m", presence=288)) == FULL_DAY
    # The cap is read from the rules, not restated in the calculation.
    few = dict(RULES, presence_max_windows=3)
    assert gross_of(ident("m", presence=288), few) == 3 * 2_000 + 50_000
    assert gross_of(ident("m", presence=2), few) == 2 * 2_000 + 50_000
    # The published row carries the proven count; the cap belongs to the formula, which anyone reruns.
    assert compute_day(0, [ident("m", presence=288)], 0).rows[0].presence == 288


def test_counts_below_zero_pay_nothing():
    assert gross_of(ident("m", presence=-5, verified_requests=-2, verdicts=-1)) == 0
    assert gross_of(ident("m", presence=50, verified_requests=-2)) == 0


def test_no_ip_rule_anywhere():
    # No address is collected, so nothing is selected per address: 25 identities paid to one address are
    # 25 identities, each paid its own reward.
    ids = [ident(f"m{n:02d}", payout_address="same") for n in range(25)]
    d = compute_day(0, ids, 0)
    assert [x.paid for x in d.rows] == [FULL_DAY] * 25
    assert all(x.reason == "" for x in d.rows)
    assert not [k for k in RULES if re.search(r"(^|_)ip(_|$)", k)]
    with pytest.raises(TypeError):
        Identity("m", ip_group="x")


def test_ranking_carries_exactly_the_published_fields():
    # `final_season_payout.py::load_day` reads these fields back and nothing else.
    j = to_json(compute_day(0, [ident("m")], 0))
    assert set(j) == {"season", "day", "rules_fingerprint", "total_gross_udndr", "total_payable_udndr",
                      "day_budget_udndr", "total_paid_udndr", "pro_rata", "ranking"}
    assert set(j["ranking"][0]) == {"rank", "miner_id", "payout_address", "presence", "verified_requests",
                                    "verdicts", "gross_udndr", "payable_udndr", "paid_udndr", "reason"}


def test_public_node_is_not_a_reward():
    assert "node_per_day" not in RULES
    with pytest.raises(TypeError):
        Identity("m", public_node=True)
    assert "public_node" not in to_json(compute_day(0, [ident("m")], 0))["ranking"][0]


def test_no_cap_per_identity():
    # The owner's decision of 2026-10-05: an identity is paid its whole gross. The work it gets is its share
    # of the chain's work draw over a fixed daily volume (each stake weighing up to the ceiling of
    # committee.go::capAssignmentWeights); a cap per identity would only pay an operator to split its stake
    # into identities that each stay under it.
    row = compute_day(0, [ident("m", verified_requests=40)], 0).rows[0]
    assert row.gross == 50 * 2_000 + 40 * 50_000 == 2_100_000     # 2.1 DNDR
    assert row.payable == row.paid == row.gross
    assert row.reason == ""
    # one identity or the same work split over three: the same total
    one = compute_day(0, [ident("a", presence=0, verified_requests=30)], 0).total_paid
    split = compute_day(0, [ident(m, presence=0, verified_requests=10) for m in "bcd"], 0).total_paid
    assert one == split == 1_500_000                                # 1.5 DNDR


def test_the_pro_rata_shares_the_budget_in_proportion_to_the_gross():
    # `a` grosses 3.1 DNDR, `b` 1, the budget is 1: each is paid its share of what can be paid.
    rules = dict(RULES, cap_programme_day=1 * UDNDR)
    a, b = ident("a", verified_requests=60), ident("b", verified_requests=18)
    assert gross_of(a, rules) == 3_100_000 and gross_of(b, rules) == 1 * UDNDR
    d = compute_day(0, [a, b], 0, rules)
    assert [(x.miner_id, x.paid) for x in d.rows] == [("a", 3_100_000 * UDNDR // 4_100_000),
                                                      ("b", 1_000_000 * UDNDR // 4_100_000)]
    assert all(x.reason == "daily budget shared pro rata" for x in d.rows)


def test_a_row_without_an_address_takes_no_share_of_the_budget():
    # The address comes before the pro rata: a row that cannot be paid does not dilute those that can.
    rules = dict(RULES, cap_programme_day=1 * UDNDR)
    b = ident("b", verified_requests=18)
    alone = compute_day(0, [b], 0, rules)
    beside = compute_day(0, [b, ident("x", verified_requests=60, payout_address="")], 0, rules)
    assert alone.rows[0].paid == {r.miner_id: r for r in beside.rows}["b"].paid == 1 * UDNDR
    assert beside.total_payable == 1 * UDNDR and beside.total_gross == 4_100_000


def test_daily_budget_is_shared_pro_rata():
    ids = [ident(f"m{n:03d}", verified_requests=40) for n in range(100)]
    d = compute_day(0, ids, 0)
    assert d.total_payable == 210 * UDNDR
    assert d.day_budget == 50 * UDNDR
    assert d.total_paid <= 50 * UDNDR
    assert all(x.paid == UDNDR // 2 for x in d.rows)
    assert all("pro rata" in x.reason for x in d.rows)


def test_pro_rata_never_pays_more_than_payable_and_floors():
    rnd = random.Random(7)
    ids = [ident(f"m{n:03d}", presence=rnd.randint(0, 60), verified_requests=rnd.randint(0, 20),
                 verdicts=rnd.randint(0, 9)) for n in range(300)]
    d = compute_day(0, ids, 0)
    assert d.total_payable > d.day_budget              # the pro rata is exercised, not skipped
    assert d.total_paid <= d.day_budget
    assert all(0 <= x.paid <= x.payable for x in d.rows)
    assert d.day_budget - d.total_paid < len(ids)   # flooring loses less than 1 udndr per identity


def test_season_budget_limits_the_last_days():
    ids = [ident(f"m{n:03d}", verified_requests=40) for n in range(30)]
    d = compute_day(29, ids, paid_before=1_490 * UDNDR)
    assert d.day_budget == 10 * UDNDR and d.total_paid <= 10 * UDNDR
    spent = compute_day(29, ids, paid_before=1_500 * UDNDR)
    assert spent.day_budget == 0 and spent.total_paid == 0


def test_below_budget_nothing_is_scaled():
    d = compute_day(0, [ident("a"), ident("b", verified_requests=6)], 0)
    assert (d.scale_num, d.scale_den) == (1, 1)
    assert [x.paid for x in d.rows] == [x.payable for x in d.rows] == [x.gross for x in d.rows]


def test_every_amount_is_an_integer_of_udndr():
    rnd = random.Random(11)
    ids = [ident(f"m{n:03d}", presence=rnd.randint(0, 60), verified_requests=rnd.randint(0, 30),
                 verdicts=rnd.randint(0, 9)) for n in range(200)]
    j = to_json(compute_day(2, ids, 1_234_567))
    for k in ("total_gross_udndr", "total_payable_udndr", "day_budget_udndr", "total_paid_udndr"):
        assert type(j[k]) is int, k
    assert all(type(v) is int for v in j["pro_rata"])
    for row in j["ranking"]:
        for k in ("rank", "presence", "verified_requests", "verdicts", "gross_udndr", "payable_udndr",
                  "paid_udndr"):
            assert type(row[k]) is int, k


def test_ranking_order_is_paid_then_miner_id():
    j = to_json(compute_day(0, [ident("c"), ident("a"), ident("b", verified_requests=3)], 0))
    assert [(x["rank"], x["miner_id"]) for x in j["ranking"]] == [(1, "b"), (2, "a"), (3, "c")]


def test_the_ranking_carries_the_fingerprint_of_its_rules():
    assert to_json(compute_day(0, [ident("m")], 0))["rules_fingerprint"] == fingerprint(RULES)
    other = dict(RULES, presence_max_windows=49)
    assert fingerprint(other) != fingerprint(RULES)
    assert compute_day(0, [ident("m")], 0, other).rules_fingerprint == fingerprint(other)


def test_same_facts_same_bytes_whatever_the_order():
    rnd = random.Random(3)
    ids = [ident(f"m{n:03d}", payout_address=f"p{n % 9}", presence=rnd.randint(0, 60),
                 verified_requests=rnd.randint(0, 30), verdicts=rnd.randint(0, 9)) for n in range(400)]
    one = json.dumps(to_json(compute_day(3, ids, 0)), sort_keys=True)
    rnd.shuffle(ids)
    two = json.dumps(to_json(compute_day(3, ids, 0)), sort_keys=True)
    assert one == two


def test_an_identity_twice_is_refused():
    with pytest.raises(ValueError):
        compute_day(0, [ident("m"), ident("m")], 0)


def test_week_totals_by_payout_address():
    d1 = compute_day(0, [ident("a", payout_address="p"), ident("b", payout_address="p"), ident("c")], 0)
    d2 = compute_day(1, [ident("a", payout_address="p")], d1.total_paid)
    w = week_totals([d1, d2])
    assert w == {"addr-c": FULL_DAY, "p": 3 * FULL_DAY}


def test_an_identity_with_no_known_address_is_not_paid_and_the_week_still_pays():
    # An identity that left the registry before its day was ranked, with no declared address, has nowhere to
    # be paid. Ranked with its reward, it stopped the WHOLE week's payment (week_totals refuses a paid row
    # without an address); it is ranked at zero instead, and says why.
    d = compute_day(0, [ident("a", payout_address=""), ident("b")], 0)
    row = {r.miner_id: r for r in d.rows}
    assert row["a"].gross > 0 and row["a"].payable == 0 and row["a"].paid == 0
    assert "no payout address known" in row["a"].reason
    assert week_totals([d]) == {row["b"].payout_address: row["b"].paid}


def test_a_paid_row_without_address_is_refused_by_the_week():
    # The last guard stays: a ranking that pays a row with no address is refused, never paid to nobody.
    d = compute_day(0, [ident("a")], 0)
    d.rows[0].payout_address = ""
    with pytest.raises(ValueError):
        week_totals([d])
