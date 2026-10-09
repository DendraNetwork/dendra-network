"""Bench of the weekly payment: which days, which sums, unpayable addresses set aside, a partial week
refused, the week holding the season's end cut at its last day, a week after it refused, and a part
already on chain never sent twice."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from modea import cosmos_addr  # noqa: E402
from final_season_address import module_address, payable_address  # noqa: E402
from final_season_calc import Identity, compute_day, to_json  # noqa: E402
import final_season_payout as P  # noqa: E402


def addr(n):
    return cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5))


A, B, C = addr(1), addr(2), addr(250)
END_HEIGHT = 1_234_567          # the season's last block, carried by the ranking of the day that holds it


def publish(tmp_path, days, extra=(), end_day=None):
    # m1: one verified request and 25 windows proven on chain, 0.05 + 25 x 0.002 = 0.1 DNDR a day.
    # m2: one verified request and no window, 0.05 DNDR a day.
    # m0: 50 windows proven and no verified request: its presence is not paid, its row is paid 0, and its
    #     address is owed nothing at the end of the week.
    # `inputs.season_end_height` is written as `final_season_rank.rank_day` writes it: null on every day
    # of a running season, the season's last block on the day `end_day` that holds it.
    paid = 0
    for d in days:
        ids = [Identity("m1", presence=25, verified_requests=1, payout_address=A),
               Identity("m2", verified_requests=1, payout_address=B),
               Identity("m0", presence=50, payout_address=C)] + list(extra)
        r = compute_day(d, ids, paid)
        paid += r.total_paid
        out = to_json(r)
        last = END_HEIGHT if d == end_day else (d + 1) * 17_280
        out["inputs"] = {"last_height": last, "season_end_height": END_HEIGHT if d == end_day else None}
        (tmp_path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def test_week_days_and_the_short_last_week():
    # The season ends at a fixed instant, so how many days it has is READ from the ranking that carries
    # the end, never set. Replaces the 30-day pins (week_days(4) == [28, 29], week_days(5) == []): before
    # the last day is known a week is its seven nominal days; once known, the week holding it is cut at it,
    # and a week starting after it is refused — never returned empty, which would read "already paid".
    assert P.week_days(0) == list(range(7))
    assert P.week_days(4) == list(range(28, 35))
    assert P.week_days(4, last_day=29) == [28, 29]
    assert P.week_days(4, last_day=28) == [28]
    assert P.week_days(4, last_day=34) == list(range(28, 35))
    assert P.week_days(3, last_day=27) == list(range(21, 28))
    for week, last in ((5, 29), (4, 27)):
        with pytest.raises(SystemExit) as e:
            P.week_days(week, last_day=last)
        assert f"starts after the season's last day ({last})" in str(e.value)


@pytest.mark.parametrize("week", [-1, -7, True, 1.0, "0"])
def test_a_week_that_is_not_a_season_week_is_refused(tmp_path, week):
    # A negative week would read no day at all and plan nothing, which `decide` answers "already paid in
    # full"; a bool or a float is not a week either. Refused by `week_days` and by `plan` before any read.
    publish(tmp_path, range(14))
    with pytest.raises(SystemExit) as e:
        P.week_days(week)
    assert "not a season week" in str(e.value)
    with pytest.raises(SystemExit) as e:
        P.plan(week, str(tmp_path))
    assert "not a season week" in str(e.value)


def test_a_negative_week_on_the_command_line_pays_nothing_and_says_so(tmp_path, capsys):
    publish(tmp_path, range(7))
    with pytest.raises(SystemExit) as e:
        P.main(["--week", "-1", "--rankings", str(tmp_path)])
    assert e.value.code not in (0, None) and "not a season week" in str(e.value)
    assert "already paid" not in capsys.readouterr().out


def test_a_week_after_the_last_day_is_refused_never_already_paid(tmp_path, capsys):
    # Day 9 carries the season's end: weeks 2 and 3 start after it. An empty plan would reach `decide` as
    # ("done", []) and print "already paid in full", exit 0, having paid nothing; it is refused instead.
    publish(tmp_path, range(10), end_day=9)
    for week in (2, 3):
        with pytest.raises(SystemExit) as e:
            P.plan(week, str(tmp_path))
        assert f"week {week} starts after the season's last day (9)" in str(e.value)
    with pytest.raises(SystemExit) as e:
        P.main(["--week", "2", "--rankings", str(tmp_path)])
    assert e.value.code not in (0, None) and "after the season's last day" in str(e.value)
    assert "already paid" not in capsys.readouterr().out


def test_the_week_holding_the_end_stops_at_the_last_day(tmp_path, capsys):
    # Before day 9 is ranked, week 1 is refused on the missing day, never paid on days 7 and 8 alone.
    publish(tmp_path, range(9))
    with pytest.raises(SystemExit) as e:
        P.plan(1, str(tmp_path))
    assert "day 9 is not ranked yet" in str(e.value)
    # Day 9 carries the end: week 1 pays days 7, 8, 9, and needs no ranking of days 10..13, which never
    # exist since the season is over.
    publish(tmp_path, range(10), end_day=9)
    assert not any((tmp_path / f"day-{d:03d}.json").exists() for d in range(10, 14))
    payable, aside, days = P.plan_days(1, str(tmp_path))
    assert days == [7, 8, 9]
    assert payable == {A: 3 * 100_000, B: 3 * 50_000} and aside == {}
    assert P.plan(1, str(tmp_path)) == (payable, aside)
    assert P.plan_days(0, str(tmp_path))[2] == list(range(7))       # the weeks before it stay whole
    assert P.main(["--week", "1", "--rankings", str(tmp_path)]) == 0
    assert "week 1 (days [7, 8, 9])" in capsys.readouterr().out


def test_plan_sums_a_full_week(tmp_path):
    publish(tmp_path, range(7))
    payable, aside = P.plan(0, str(tmp_path))
    # C (presence without work) is in neither list: a row paid 0 sends nothing
    assert payable == {A: 7 * 100_000, B: 7 * 50_000} and aside == {}


def test_a_week_with_a_missing_day_is_not_paid_in_part(tmp_path):
    publish(tmp_path, [0, 1, 2, 4, 5, 6])
    with pytest.raises(SystemExit) as e:
        P.plan(0, str(tmp_path))
    assert "day 3 is not ranked yet" in str(e.value)        # refused on the missing day, not on another


def test_unpayable_addresses_are_set_aside_not_sent(tmp_path):
    blocked = module_address("bonded_tokens_pool")
    publish(tmp_path, range(7), extra=[Identity("m3", verified_requests=1, payout_address=blocked)])
    payable, aside = P.plan(0, str(tmp_path))
    assert blocked in aside and blocked not in payable and A in payable
    assert aside[blocked] == 7 * 50_000
    assert payable_address(cosmos_addr.bech32_encode("dendra", [])) != ""


def test_a_module_account_in_capitals_is_unpayable():
    # Bech32 is case-insensitive: DENDRA1... in capitals is the same module account, so the blocked set is
    # compared on the lowercase form; a comparison on the raw string would let the capitalised form through
    # both the service and the payout.
    blocked = module_address("fee_collector")
    assert payable_address(blocked.upper()) != ""
    assert payable_address(A.upper()) == ""                         # an ordinary account stays payable
    mixed = A[:10] + A[10:].upper()
    assert payable_address(mixed) != ""                             # mixed case is not bech32 at all


def _tamper(tmp_path, day, fn):
    p = tmp_path / f"day-{day:03d}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    fn(d)
    p.write_text(json.dumps(d), encoding="utf-8")


def _inflated(d):
    # A row paid more than its own counts give, every total kept consistent with it: only the formula
    # rerun on the row's facts can refuse it.
    r = d["ranking"][0]
    for k in ("gross_udndr", "payable_udndr", "paid_udndr"):
        r[k] += 1_000_000
    for k in ("total_gross_udndr", "total_payable_udndr", "total_paid_udndr"):
        d[k] += 1_000_000


def _no_address_paid(d):
    # A row with a gross and no payout address, kept payable: the payment would have nowhere to send it.
    d["ranking"][0]["payout_address"] = ""


def _set(path, value):
    def fn(d):
        *head, last = path
        for k in head:
            d = d[k]
        d[last] = value
    return fn


@pytest.mark.parametrize("mutate,why", [
    (_set(["rules_fingerprint"], "0" * 64), "ranked under rules"),
    (_set(["day"], 4), "says it is day 4"),
    (lambda d: d["ranking"][0].update(paid_udndr=d["ranking"][0]["paid_udndr"] + 1), "outside 0 <= paid"),
    (lambda d: d["ranking"][0].update(paid_udndr=-1), "outside 0 <= paid"),
    (_inflated, "the formula on its counts gives"),
    (_no_address_paid, "it is 0"),
    (lambda d: d["ranking"][0].update(verified_requests=1.0), "not a whole number"),
    (lambda d: d["ranking"][0].update(presence=-1), "not a whole number"),
    (lambda d: d.update(total_gross_udndr=d["total_gross_udndr"] + 1), "do not add up"),
    (lambda d: d.update(total_payable_udndr=d["total_payable_udndr"] + 1), "do not add up"),
    (lambda d: d.update(total_paid_udndr=d["total_paid_udndr"] + 1), "do not add up"),
    (_set(["day_budget_udndr"], 10 ** 12), "budget"),
    # a budget UNDER the programme's cap that is not the one the earlier days leave: the next check
    # (paid <= budget <= cap) passes it, so only the derived comparison can refuse it
    (_set(["day_budget_udndr"], 40 * 1_000_000), "but the earlier days paid"),
    # a total written as a float that equals the integer sum: every later comparison passes it
    (lambda d: d.update(total_paid_udndr=float(d["total_paid_udndr"])), "a total is not an integer"),
    (_set(["pro_rata"], [1, 2]), "pro-rata"),
    (lambda d: d["ranking"].append(dict(d["ranking"][0])), "ranked twice"),
    (lambda d: d["ranking"][0].update(paid_udndr=float(d["ranking"][0]["paid_udndr"])), "non-integer"),
])
def test_rankings_that_break_the_rules_refuse_the_whole_week(tmp_path, mutate, why):
    # The rankings come from the internet-facing service and this script signs what it reads: each figure
    # the rules fix is checked before anything is planned, and any one of these refuses the week.
    publish(tmp_path, range(7))
    _tamper(tmp_path, 3, mutate)
    with pytest.raises(SystemExit) as e:
        P.plan(0, str(tmp_path))
    assert why in str(e.value) and "day 3" in str(e.value)


def test_a_row_without_presence_is_unreadable_not_zero(tmp_path):
    # A row carrying no `presence` (a ranking in another format, with `windows` and `class`) is refused
    # whole: the field is read, never defaulted to 0, so a day nobody can read is never a day paid.
    publish(tmp_path, range(7))

    def other_format(d):
        row = d["ranking"][0]
        row["windows"], row["class"] = row.pop("presence"), 2
    _tamper(tmp_path, 3, other_format)
    with pytest.raises(SystemExit) as e:
        P.plan(0, str(tmp_path))
    assert "day 3" in str(e.value) and "unreadable" in str(e.value) and "KeyError" in str(e.value)


def _drop_end(d):
    del d["inputs"]["season_end_height"]


@pytest.mark.parametrize("mutate,err", [
    (_drop_end, "KeyError"),
    (lambda d: d.pop("inputs"), "KeyError"),
    (_set(["inputs", "season_end_height"], str(END_HEIGHT)), "ValueError"),
    (_set(["inputs", "season_end_height"], float(END_HEIGHT)), "ValueError"),
    (_set(["inputs", "season_end_height"], True), "ValueError"),
    # Bounded from below and tied to the day: the ranking that holds the end names it as its own last block.
    (_set(["inputs", "season_end_height"], 0), "ValueError"),
    (_set(["inputs", "season_end_height"], END_HEIGHT + 1), "ValueError"),
])
def test_a_ranking_that_does_not_say_whether_it_ends_the_season_is_refused(tmp_path, mutate, err):
    # `season_end_height` is read, never defaulted: a ranking that does not say whether it holds the end
    # can be read neither as "not the last day" (the reading would run past the end) nor as the last (the
    # week would be cut short at a day the chain never ended on). The whole week is refused.
    publish(tmp_path, range(7))
    _tamper(tmp_path, 3, mutate)
    with pytest.raises(SystemExit) as e:
        P.plan(0, str(tmp_path))
    assert "day 3" in str(e.value) and "unreadable" in str(e.value) and err in str(e.value)


def test_an_earlier_week_out_of_line_refuses_this_one(tmp_path):
    # Week 1's budget follows from what days 0..6 paid: a day of week 0 that breaks the rules refuses it.
    publish(tmp_path, range(14))
    assert P.plan(1, str(tmp_path))[0] == {A: 7 * 100_000, B: 7 * 50_000}
    _tamper(tmp_path, 2, lambda d: d["ranking"][0].update(payable_udndr=2_000_000))
    with pytest.raises(SystemExit) as e:
        P.plan(1, str(tmp_path))
    assert "day 2" in str(e.value)


def test_a_season_near_its_cap_is_checked_on_the_budget_it_had(tmp_path):
    # 60 identities grossing 2.1 DNDR each ask 126 DNDR a day, so every day is shared pro rata at its budget:
    # 29 days at the full 50 DNDR leave 50 for day 29, and a published day 29 claiming more is refused.
    # Here day 29 carries the season's end (a fixture choice: the real length is read, never set), so
    # week 4 is days 28 and 29.
    paid = 0
    for d in range(30):
        ids = [Identity(f"m{n:03d}", presence=50, verified_requests=40, payout_address=addr(n % 200 + 3))
               for n in range(60)]
        r = compute_day(d, ids, paid)
        assert (r.scale_num, r.scale_den) != (1, 1)                 # the bench exercises the pro-rata
        paid += r.total_paid
        out = to_json(r)
        last = END_HEIGHT if d == 29 else (d + 1) * 17_280
        out["inputs"] = {"last_height": last, "season_end_height": END_HEIGHT if d == 29 else None}
        (tmp_path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")
    assert paid <= 1_500 * 1_000_000
    P.plan(4, str(tmp_path))                                        # the honest season passes
    _tamper(tmp_path, 29, _set(["day_budget_udndr"], 60 * 1_000_000))
    with pytest.raises(SystemExit) as e:
        P.plan(4, str(tmp_path))
    assert "day 29" in str(e.value) and "budget" in str(e.value)


def test_decide_pays_missing_parts_only(monkeypatch):
    monkeypatch.setattr(P, "BATCH", 1)
    payable = {A: 5, B: 7}
    dg = P.plan_digest(payable)
    assert P.decide(payable, {}) == ("pay", [0, 1])
    assert P.decide(payable, {0: (dg, "h0")}) == ("pay", [1])                 # an interrupted run is finished
    assert P.decide(payable, {0: (dg, "h0"), 1: (dg, "h1")}) == ("done", [])   # never paid twice
    assert P.decide(payable, {0: ("000000000000", "hX")}) == ("stop", [0])     # the plan changed: a person decides


def test_memo_is_parsed_back():
    m = P.memo(3, "abcdef012345", 2)
    g = P._MEMO.match(m)
    assert g and g.groups() == ("3", "abcdef012345", "2")
    assert P._MEMO.match(P.memo(13, "abcdef012345", 0)).group(1) == "13"


class PayerIndex:
    """A fake `dendrad query txs` over the payout account's history: `sends` transactions, the week's part
    `part` paid at position `paid_at`, served 100 per page. `hollow_from` serves empty pages from that page
    on while the count stays whole; `no_count` drops the count from every answer."""

    def __init__(self, sends=150, paid_at=120, part=0, hollow_from=None, no_count=False):
        self.txs = [{"txhash": f"T{k}", "tx": {"body": {"memo": f"other-{k}"}}} for k in range(sends)]
        self.txs[paid_at] = {"txhash": "PAID", "tx": {"body": {"memo": P.memo(2, "abcdef012345", part)}}}
        self.hollow_from, self.no_count = hollow_from, no_count

    def __call__(self, args, node, timeout=120):
        page = int(args[args.index("--page") + 1])
        chunk = [] if self.hollow_from and page >= self.hollow_from else self.txs[(page - 1) * 100: page * 100]
        d = {"txs": chunk}
        if not self.no_count:
            d["total_count"] = str(len(self.txs))
        return json.dumps(d)


def test_a_part_paid_beyond_the_first_page_is_found(monkeypatch):
    monkeypatch.setattr(P, "_cli", PayerIndex())
    assert P.paid_parts("tcp://n:26657", "dendra1payer", 2) == {0: ("abcdef012345", "PAID")}


@pytest.mark.parametrize("index", [PayerIndex(hollow_from=2), PayerIndex(no_count=True)],
                         ids=["empty-page-before-the-count", "answer-without-its-count"])
def test_a_list_of_paid_parts_that_cannot_be_known_complete_stops_the_payment(monkeypatch, index):
    # The part paid at position 120 sits on page 2: a list cut after page 1 would say it is unpaid, and
    # `decide` would send it again.
    monkeypatch.setattr(P, "_cli", index)
    with pytest.raises(RuntimeError, match="cannot be known, nothing is sent"):
        P.paid_parts("tcp://n:26657", "dendra1payer", 2)
