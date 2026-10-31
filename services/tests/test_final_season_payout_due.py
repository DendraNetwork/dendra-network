"""Bench of --due, the walk the chain host's cron runs: every week payable and not paid yet, from week 0 on.

The chain is a fake `dendrad` held in memory (after `PayerIndex` in test_final_season_payout.py): what the payout
account sends is kept and served back to the next search, so a second run sees the first run's payments, and its
height is a number the case sets. The rankings are files written by `publish`, with the two inputs a real ranking
carries for lateness (`start_height`, `finality_blocks`). Nothing reaches a network. The cases: nothing ranked; one
week payable and unpaid; a week already paid and the next not ranked; two weeks in one run; a refused week stops the
walk; a day that cannot be read, that is gone from a week already paid, or that is late, is refused, never awaited;
the end of the season; plan only; the refusals before any week; and --week, whose message and exit status do not
change."""
import json
import os
import subprocess
import sys
import types
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_payout as P  # noqa: E402
from test_final_season_payout import A, B, _inflated, _tamper, addr  # noqa: E402
from test_final_season_payout import publish as publish_days  # noqa: E402

PAYER = addr(7)
CHAIN_ID = "dendra-bench-1"
WEEK = {A: 7 * 100_000, B: 7 * 50_000}      # what a full week of `publish` pays each address
DAY = P.R.RULES["day_blocks"]
START = 1                                   # `publish` writes day d's last block as (d + 1) x DAY: start at block 1
FINALITY = DAY + 440                        # a finality longer than a day, as the public chain's


def publish(tmp_path, days, **kw):
    """`test_final_season_payout.publish`, plus the inputs `final_season_rank.rank_day` writes on every ranking and
    that lateness reads (`final_season_payout.late_from`)."""
    publish_days(tmp_path, days, **kw)
    for d in days:
        p = tmp_path / f"day-{d:03d}.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["inputs"].update(start_height=START, finality_blocks=FINALITY)
        p.write_text(json.dumps(doc), encoding="utf-8")


def late_at(day):
    """Where day `day` is late: the height at which the day after it is final (its last block + FINALITY)."""
    return START + (day + 2) * DAY - 1 + FINALITY


class FakeChain:
    """The payout account's side of `dendrad`: `query txs` serves what the account sent, 100 a page with the count;
    `tx sign` copies the unsigned file; `tx broadcast` executes it and keeps it (or refuses the part named by
    `refuse_part` with a code); `query tx` finds it. `keys show` and the history check are counted, since both must
    happen once per run, before any payment. The node's height is `height_now`, or `height_error` is raised."""

    def __init__(self):
        self.txs, self.signed, self.keys_shown, self.index_checks = [], [], 0, 0
        self.key_found, self.history_whole, self.refuse_part = True, True, None
        self.height_now, self.height_error = 1, None

    def height(self, rpc):
        if self.height_error is not None:
            raise self.height_error
        return self.height_now

    def seed(self, week, digest, part):
        """A part already on chain, sent by an earlier run: one transfer of 1 udndr to A under that memo."""
        send = {"@type": "/cosmos.bank.v1beta1.MsgSend", "from_address": PAYER, "to_address": A,
                "amount": [{"denom": "udndr", "amount": "1"}]}
        self.txs.append({"txhash": f"SEED{len(self.txs)}", "code": 0,
                         "tx": {"body": {"memo": P.memo(week, digest, part), "messages": [send]}}})

    def cli(self, args, node, timeout=120):
        if args[:2] == ["query", "txs"]:
            page = int(args[args.index("--page") + 1])
            return json.dumps({"txs": self.txs[(page - 1) * 100: page * 100], "total_count": str(len(self.txs))})
        if args[:2] == ["tx", "sign"]:
            self.signed.append((args[args.index("--from") + 1], args[args.index("--chain-id") + 1]))
            with open(args[2], encoding="utf-8") as f, open(args[args.index("--output-document") + 1], "w",
                                                            encoding="utf-8") as g:
                g.write(f.read())
            return ""
        if args[:2] == ["tx", "broadcast"]:
            with open(args[2], encoding="utf-8") as f:
                body = json.load(f)["body"]
            if self.refuse_part is not None and body["memo"].endswith(f"-part-{self.refuse_part}"):
                return json.dumps({"code": 5, "raw_log": "insufficient funds"})
            h = f"TX{len(self.txs)}"
            self.txs.append({"txhash": h, "code": 0, "tx": {"body": body}})
            return json.dumps({"code": 0, "txhash": h})
        if args[:2] == ["query", "tx"]:
            if any(t["txhash"] == args[2] for t in self.txs):
                return json.dumps({"code": 0})
            raise RuntimeError("tx not found")
        raise AssertionError(f"unexpected dendrad call {args}")

    def run(self, argv, **kw):
        assert argv[:3] == ["dendrad", "keys", "show"], argv
        self.keys_shown += 1
        if not self.key_found:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: payout.info: key not found")
        return subprocess.CompletedProcess(argv, 0, stdout=PAYER + chr(10), stderr="")

    def require_index_from(self, rpc, first_height):
        self.index_checks += 1
        if not self.history_whole:
            raise P.C.ChainUnreadable("this node's history starts at block 900, after block 1")

    def sent(self):
        """{week: {part: [[address, udndr], ...]}} of every season part the account sent, read as the list reads it."""
        out = {}
        for t in P.payout_transfers("tcp://chain:26657", PAYER):
            out.setdefault(t["week"], {})[t["part"]] = t["sends"]
        return out


@pytest.fixture
def chain(monkeypatch):
    ch = FakeChain()
    monkeypatch.setattr(P, "_cli", ch.cli)
    monkeypatch.setattr(P, "subprocess", types.SimpleNamespace(run=ch.run, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(P.C, "require_index_from", ch.require_index_from)
    monkeypatch.setattr(P.C, "height", ch.height)
    monkeypatch.setattr(P, "time", types.SimpleNamespace(sleep=lambda s: None))
    monkeypatch.delenv("DENDRA_CHAIN_ID", raising=False)
    return ch


def due(tmp_path, *extra, chain_id=CHAIN_ID):
    argv = ["--due", "--rankings", str(tmp_path), "--doubled-from-day", "none", *extra]
    return P.main(argv + (["--chain-id", chain_id] if chain_id else []))


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def as_sends(payable):
    return [[a, v] for a, v in sorted(payable.items())]


def waits(week, day, height=1):
    """The line of a week that waits for a day not ranked and not late."""
    return (f"week {week}: not payable yet: day {day} is not ranked (late from block {late_at(day)}, the chain is "
            f"at {height})")


def test_nothing_ranked_is_a_wait_not_a_failure(tmp_path, chain, capsys):
    # Day 0 has no earlier ranking to say where it ends: the walk says it does not know whether it is late.
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    assert ("week 0: not payable yet: day 0 is not ranked; no earlier ranking says where it ends, so whether it is "
            "late is not known") in out
    assert out[-1] == "summary: weeks paid now [], already paid []; week 0 waits for day 0 (exit 0)"
    assert chain.txs == [] and chain.signed == []
    assert (chain.keys_shown, chain.index_checks) == (1, 1)


def test_week_0_payable_and_unpaid_is_paid_then_the_walk_waits(tmp_path, chain, capsys):
    publish(tmp_path, range(7))
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    digest = P.plan_digest(WEEK)
    assert chain.sent() == {0: {0: as_sends(WEEK)}}                       # one part, exactly the plan
    assert chain.txs[0]["tx"]["body"]["memo"] == P.memo(0, digest, 0)
    assert chain.signed == [("payout", CHAIN_ID)]
    assert f"week 0 (days 0 to 6), plan {digest}: paid, part(s) [0] sent" in out
    assert waits(1, 7) in out
    assert out[-1] == "summary: weeks paid now [0], already paid []; week 1 waits for day 7 (exit 0)"


def test_a_week_already_paid_is_skipped_and_the_next_waits(tmp_path, chain, capsys):
    publish(tmp_path, range(10))                                          # week 1 lacks days 10 to 13
    assert due(tmp_path, "--yes") == 0
    capsys.readouterr()
    before = list(chain.txs)
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    assert chain.txs == before and len(chain.signed) == 1                 # the second run sends nothing
    assert f"week 0 (days 0 to 6), plan {P.plan_digest(WEEK)}: already paid in full, nothing sent" in out
    assert waits(1, 10) in out
    assert out[-1] == "summary: weeks paid now [], already paid [0]; week 1 waits for day 10 (exit 0)"


def test_two_weeks_payable_are_paid_in_one_run_with_one_key_and_history_check(tmp_path, chain, capsys):
    publish(tmp_path, range(14))
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    assert chain.sent() == {0: {0: as_sends(WEEK)}, 1: {0: as_sends(WEEK)}}
    assert (chain.keys_shown, chain.index_checks) == (1, 1)
    assert out[-1] == "summary: weeks paid now [0, 1], already paid []; week 2 waits for day 14 (exit 0)"


def test_parts_under_another_plan_refuse_the_week_and_stop_the_walk(tmp_path, chain, capsys):
    publish(tmp_path, range(21))                                          # weeks 0, 1 and 2 all ranked
    chain.seed(1, "000000000000", 0)
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert set(chain.sent()) == {0, 1} and chain.sent()[1] == {0: [[A, 1]]}   # week 0 paid, week 1 the seed only
    assert 2 not in chain.sent()                                         # nothing paid past the refused week
    assert any(x.startswith("week 1: refused: part(s) [0] are on chain under another plan") for x in out)
    assert out[-1] == "summary: weeks paid now [0], already paid []; week 1 refused, no later week paid (exit 3)"


def test_rankings_out_of_the_rules_refuse_the_week_and_stop_the_walk(tmp_path, chain, capsys):
    publish(tmp_path, range(21))
    _tamper(tmp_path, 9, _inflated)
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert set(chain.sent()) == {0}
    assert any(x.startswith("week 1: Refused: day 9:") for x in out)
    assert out[-1] == "summary: weeks paid now [0], already paid []; week 1 refused, no later week paid (exit 3)"


def test_a_published_day_that_cannot_be_read_is_refused_not_awaited(tmp_path, chain, capsys):
    # A row without `presence` raises KeyError on reading: the day exists and is unreadable, which no later run
    # can turn into a ranked day. Read as "not ranked yet" the cron would wait on it in silence for ever.
    publish(tmp_path, range(14))

    def other_format(d):
        row = d["ranking"][0]
        row["windows"] = row.pop("presence")
    _tamper(tmp_path, 9, other_format)
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert set(chain.sent()) == {0}
    assert any(x.startswith("week 1: refused: day 9 cannot be read") and "KeyError" in x for x in out)


def test_a_day_gone_from_a_week_already_paid_is_refused_not_awaited(tmp_path, chain, capsys):
    # A part is sent only once every day of its week is published. Rankings gone after a payment (a purged service)
    # would read "week 0 waits for day 0" and exit 0 at every run from then on, with nothing paid and no alert.
    publish(tmp_path, range(14))
    assert due(tmp_path, "--yes") == 0
    capsys.readouterr()
    before = list(chain.txs)
    (tmp_path / "day-003.json").unlink()
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert chain.txs == before
    assert any(x.startswith("week 0: refused: day 3 is not published, yet week(s) [0, 1] are paid on chain") for x in out)
    assert out[-1] == "summary: weeks paid now [], already paid []; week 0 refused, no later week paid (exit 3)"


def test_late_from_is_the_height_where_the_day_after_is_final():
    # Day 3 is late once day 4 is final: day 4 ends at start + 5 x DAY - 1 (final_season_rules.day_bounds).
    assert P.late_from({"inputs": {"start_height": 100, "finality_blocks": 50}}, 3) == 100 + 5 * DAY - 1 + 50
    for bad in ({"start_height": 0, "finality_blocks": 50}, {"start_height": 100, "finality_blocks": 0},
                {"start_height": 100, "finality_blocks": True}, {"start_height": "100", "finality_blocks": 50},
                {"start_height": 100}, {"finality_blocks": 50}, {}, None):
        with pytest.raises(ValueError):
            P.late_from({"inputs": bad}, 3)
    with pytest.raises(ValueError):
        P.late_from({}, 3)


def test_a_day_the_service_is_late_to_rank_is_refused_not_awaited(tmp_path, chain, capsys):
    # The service still answers, with a 404 for day 10, but no longer ranks: the chain is past the height at which
    # even day 11 is final. Read as a wait, every run would end "ok", week 1 would never be paid, and nobody told.
    publish(tmp_path, range(10))
    chain.height_now = late_at(10) - 1                                    # one block before: still a wait
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    assert waits(1, 10, late_at(10) - 1) in out
    assert set(chain.sent()) == {0}
    before = list(chain.txs)
    chain.height_now = late_at(10)
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert chain.txs == before                                            # nothing sent
    assert any(x.startswith(f"week 1: refused: day 10 is late: it is not ranked, and the chain is at block "
                            f"{late_at(10)}, at or past block {late_at(10)}") for x in out)
    assert out[-1] == "summary: weeks paid now [], already paid [0]; week 1 waits for day 10, which is late (exit 3)"
    assert due(tmp_path) == 3                                             # plan only says the same


@pytest.mark.parametrize("unreadable", ["chain-height", "ranking-without-finality"])
def test_a_lateness_that_cannot_be_read_is_a_failure_not_a_wait(tmp_path, chain, capsys, unreadable):
    publish(tmp_path, range(10))
    if unreadable == "chain-height":
        chain.height_error = P.C.ChainUnreadable("http://chain:26657/status: URLError: connection refused")
    else:
        _tamper(tmp_path, 9, lambda d: d["inputs"].pop("finality_blocks"))
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert set(chain.sent()) == {0}                                       # week 0 is paid before the wait is read
    assert any(x.startswith("week 1: failed: whether day 10 is late cannot be read") for x in out)
    assert out[-1] == "summary: weeks paid now [0], already paid []; week 1 failed, no later week paid (exit 3)"


@pytest.mark.parametrize("error,rc", [
    (urllib.error.HTTPError("http://final-season/ranking/day-007.json", 404, "not published", {}, None), 0),
    (urllib.error.HTTPError("http://final-season/ranking/day-007.json", 503, "unavailable", {}, None), 3),
    (urllib.error.URLError("connection refused"), 3),
], ids=["404-is-a-wait", "503-is-refused", "unreachable-is-refused"])
def test_only_a_ranking_the_service_says_does_not_exist_is_a_wait(tmp_path, chain, capsys, monkeypatch, error, rc):
    # The rankings come over HTTP on the chain host: a 404 is a day not published yet; a service that does not
    # answer, or answers 503, is a failure the cron must report, never a wait.
    publish(tmp_path, range(7))
    real = P._read_bytes

    def served(src, name):
        if name == "day-007.json":
            raise error
        return real(src, name)
    monkeypatch.setattr(P, "_read_bytes", served)
    assert due(tmp_path, "--yes") == rc
    out = lines(capsys)
    assert set(chain.sent()) == {0}                                       # week 0 is paid either way
    if rc == 0:
        assert waits(1, 7) in out
    else:
        assert any(x.startswith("week 1: refused: day 7 cannot be read") for x in out)


def test_the_end_of_the_season_ends_the_walk(tmp_path, chain, capsys):
    publish(tmp_path, range(10), end_day=9)                               # day 9 holds the season's last block
    assert due(tmp_path, "--yes") == 0
    out = lines(capsys)
    assert chain.sent() == {0: {0: as_sends(WEEK)}, 1: {0: as_sends({A: 3 * 100_000, B: 3 * 50_000})}}
    assert "week 2: starts after the season's last day (9): no week left to pay" in out
    assert out[-1] == "summary: weeks paid now [0, 1], already paid []; the season has no week after week 1 (exit 0)"
    before = list(chain.txs)
    assert due(tmp_path, "--yes") == 0                                    # run again after the end: nothing sent
    assert chain.txs == before
    assert lines(capsys)[-1] == ("summary: weeks paid now [], already paid [0, 1]; the season has no week after "
                                 "week 1 (exit 0)")


def test_a_part_that_fails_stops_the_walk(tmp_path, chain, capsys):
    publish(tmp_path, range(14))
    chain.refuse_part = 0
    assert due(tmp_path, "--yes") == 3
    out = lines(capsys)
    assert chain.sent() == {}
    assert any(x.startswith("week 0: failed: RuntimeError: part 0 refused: code=5") for x in out)
    assert out[-1] == "summary: weeks paid now [], already paid []; week 0 failed, no later week paid (exit 3)"


def test_without_yes_it_plans_and_signs_nothing(tmp_path, chain, capsys):
    publish(tmp_path, range(14))
    chain.seed(0, P.plan_digest(WEEK), 0)                                 # week 0 is on chain, week 1 is due
    before = list(chain.txs)
    assert due(tmp_path, chain_id="") == 0                                # no chain id needed to plan
    out = lines(capsys)
    assert chain.txs == before and chain.signed == []
    assert f"week 1 (days {list(range(7, 14))}), plan {P.plan_digest(WEEK)}: 2 addresses, 1050000 udndr in 1 part(s)" in out
    assert f"week 1 (days 7 to 13), plan {P.plan_digest(WEEK)}: due, plan only: part(s) [0] would be sent" in out
    assert out[-1] == ("summary, plan only, nothing signed: weeks due [1], already paid [0]; week 2 waits for day 14 "
                       "(exit 0)")


@pytest.mark.parametrize("setup,argv_extra,chain_id,why", [
    (lambda ch: None, ("--yes",), "", "Refused: --chain-id (or DENDRA_CHAIN_ID) is required to sign"),
    (lambda ch: setattr(ch, "key_found", False), ("--yes",), CHAIN_ID, "Refused: key 'payout' not found"),
    (lambda ch: setattr(ch, "history_whole", False), ("--yes",), CHAIN_ID, "Refused: this node's history starts"),
    (lambda ch: setattr(ch, "history_whole", False), (), CHAIN_ID, "Refused: this node's history starts"),
], ids=["no-chain-id", "key-not-found", "history-not-whole", "history-not-whole-plan-only"])
def test_refusals_before_any_week_exit_2_and_read_no_week(tmp_path, chain, capsys, setup, argv_extra, chain_id, why):
    publish(tmp_path, range(14))
    setup(chain)
    assert due(tmp_path, *argv_extra, chain_id=chain_id) == 2
    out = lines(capsys)
    assert any(x.startswith(why) for x in out)
    assert not any(x.startswith("week ") for x in out)                    # no week is planned or paid
    assert chain.txs == [] and chain.signed == []
    assert out[-1].startswith("summary: not run") and out[-1].endswith("(exit 2)")


# -- --week keeps its words and its exit status ------------------------------------------------------------------
def _cli_week(tmp_path, week):
    return subprocess.run([sys.executable, P.__file__, "--week", str(week), "--rankings", str(tmp_path),
                           "--doubled-from-day", "none"], capture_output=True, text=True, timeout=120)


def test_week_mode_unchanged_on_an_unranked_day(tmp_path):
    publish(tmp_path, range(9))
    message = "day 9 is not ranked yet or unreadable (FileNotFoundError): week 1 is not paid in part"
    with pytest.raises(SystemExit) as e:
        P.main(["--week", "1", "--rankings", str(tmp_path), "--doubled-from-day", "none"])
    assert e.value.code == message and isinstance(e.value, P.NotRankedYet) and e.value.day == 9
    r = _cli_week(tmp_path, 1)
    assert (r.returncode, r.stdout, r.stderr.strip()) == (1, "", message)


def test_week_mode_unchanged_after_the_end_and_on_a_refusal(tmp_path):
    publish(tmp_path, range(10), end_day=9)
    r = _cli_week(tmp_path, 2)
    assert (r.returncode, r.stdout, r.stderr.strip()) == (
        1, "", "week 2 starts after the season's last day (9): there is nothing to pay")
    _tamper(tmp_path, 3, _inflated)
    r = _cli_week(tmp_path, 0)
    assert r.returncode == 1 and r.stdout == ""
    assert r.stderr.startswith("Refused: day 3: ") and r.stderr.strip().endswith("Week 0 is not paid; nothing is signed.")
    with pytest.raises(P.PlanRefused):
        P.plan(0, str(tmp_path), None)
