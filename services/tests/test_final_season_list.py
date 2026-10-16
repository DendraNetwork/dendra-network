"""Bench of the season's list of payments (ADR-049 decision 2) and of the archive of the rankings: the list is
built from the rankings and checked against the payout account's transfers on chain, it is the same bytes on
every run with its SHA-256 next to it, and every payment out of line refuses it."""
import hashlib
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from modea import cosmos_addr  # noqa: E402
from final_season_address import module_address  # noqa: E402
from final_season_calc import Identity, compute_day, to_json  # noqa: E402
import final_season_payout as P  # noqa: E402


def addr(n):
    return cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5))


A, B, PAYER = addr(1), addr(2), addr(9)
BLOCKED = module_address("bonded_tokens_pool")
END_HEIGHT = 1_234_567


def publish(root, days, end_day=None, blocked=False):
    # m1 earns 0.1 DNDR a day, m2 0.05 (the bench of the weekly payment); m3, with `blocked`, 0.05 to a module
    # account the chain refuses to credit.
    paid = 0
    for d in days:
        ids = [Identity("m1", presence=25, verified_requests=1, payout_address=A),
               Identity("m2", verified_requests=1, payout_address=B)]
        if blocked:
            ids.append(Identity("m3", verified_requests=1, payout_address=BLOCKED))
        r = compute_day(d, ids, paid)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": END_HEIGHT if d == end_day else (d + 1) * 17_280,
                         "season_end_height": END_HEIGHT if d == end_day else None}
        (root / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def paid_as_planned(root, weeks):
    """The payout account's transfers, as `final_season_payout.send_part` sends each week's plan."""
    out = []
    for k in weeks:
        payable = P.plan_days(k, str(root), None)[0]
        dg = P.plan_digest(payable)
        for i, chunk in enumerate(P.parts(payable)):
            out.append({"week": k, "plan": dg, "part": i, "txhash": f"TX{k}{i}", "sends": [[a, v] for a, v in chunk]})
    return out


def build(root, transfers):
    days, digests, end = P.season_days(str(root))
    return P.season_list(days, digests, end, transfers, None, PAYER)


def test_the_list_is_what_the_weeks_paid_and_sums_to_what_was_sent(tmp_path):
    publish(tmp_path, range(10), end_day=9)
    transfers = paid_as_planned(tmp_path, (0, 1))
    doc = build(tmp_path, transfers)
    assert doc["paid_udndr"] == {A: 10 * 100_000, B: 10 * 50_000}
    sent = sum(v for t in transfers for _a, v in t["sends"])
    assert doc["total_paid_udndr"] == sent == 10 * 150_000 == doc["rankings_total_paid_udndr"]
    assert (doc["last_day"], doc["season_end_height"], doc["payer"]) == (9, END_HEIGHT, PAYER)
    assert [p["week"] for p in doc["payments"]] == [0, 1] and doc["payments"][1]["txs"] == ["TX10"]
    raw = (tmp_path / "day-004.json").read_bytes()
    assert doc["rankings_sha256"]["day-004.json"] == hashlib.sha256(raw).hexdigest()
    assert sorted(doc["rankings_sha256"]) == [f"day-{d:03d}.json" for d in range(10)]


def test_an_amount_ranked_to_an_unpayable_address_is_listed_apart_never_in_the_list(tmp_path):
    publish(tmp_path, range(3), end_day=2, blocked=True)
    doc = build(tmp_path, paid_as_planned(tmp_path, (0,)))
    assert BLOCKED not in doc["paid_udndr"] and doc["set_aside_udndr"] == {BLOCKED: 3 * 50_000}
    assert doc["total_paid_udndr"] + doc["total_set_aside_udndr"] == doc["rankings_total_paid_udndr"]


def test_the_list_is_the_same_bytes_whatever_the_order_the_chain_served(tmp_path):
    publish(tmp_path, range(10), end_day=9)
    transfers = paid_as_planned(tmp_path, (0, 1))
    one = P.stable_bytes(build(tmp_path, transfers))
    two = P.stable_bytes(build(tmp_path, list(reversed(transfers))))
    assert one == two and one.endswith(b"}" + bytes([10])) and bytes([13]) not in one
    assert one.isascii()


@pytest.mark.parametrize("change, why", [
    (lambda t: t[:-1], "not paid as planned"),                                        # week 1 never paid
    (lambda t: t + [dict(t[0], txhash="TWICE")], "paid twice"),
    (lambda t: [dict(t[0], plan="000000000000")] + t[1:], "another plan"),
    (lambda t: [dict(t[0], sends=[[t[0]["sends"][0][0], t[0]["sends"][0][1] + 1]] + t[0]["sends"][1:])] + t[1:],
     "did not send the transfers its plan lists"),
    (lambda t: [dict(t[0], sends=t[0]["sends"][:1])] + t[1:], "did not send the transfers its plan lists"),
    (lambda t: t + [dict(t[0], week=2, txhash="LATE")], "not a week of the season"),
    (lambda t: t + [dict(t[0], part=1, txhash="EXTRA")], "beyond the plan"),
], ids=["week-unpaid", "part-twice", "other-plan", "amount-off", "transfer-missing", "week-after-season",
        "part-beyond-plan"])
def test_a_payment_out_of_line_refuses_the_list(tmp_path, change, why):
    publish(tmp_path, range(10), end_day=9)
    with pytest.raises(ValueError, match=why):
        build(tmp_path, change(paid_as_planned(tmp_path, (0, 1))))


def test_no_list_before_the_last_day_is_ranked(tmp_path):
    publish(tmp_path, range(10))                         # no ranking carries the season's last block yet
    with pytest.raises(SystemExit, match="day 10 is not ranked"):
        P.season_days(str(tmp_path))


def test_rankings_that_break_the_rules_refuse_the_list(tmp_path):
    publish(tmp_path, range(3), end_day=2)
    d = json.loads((tmp_path / "day-001.json").read_text(encoding="utf-8"))
    d["ranking"][0]["paid_udndr"] += 1
    (tmp_path / "day-001.json").write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="day 1"):
        build(tmp_path, [])


def test_a_ranking_read_from_an_unreachable_source_is_not_an_absent_day(monkeypatch):
    def unreachable(src, name):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(P, "_read_bytes", unreachable)
    with pytest.raises(urllib.error.URLError):
        P.ranking_bytes("https://x/ranking", 0)

    def not_found(src, name):
        raise urllib.error.HTTPError(f"{src}/{name}", 404, "Not Found", None, None)
    monkeypatch.setattr(P, "_read_bytes", not_found)
    assert P.ranking_bytes("https://x/ranking", 0) is None

    def server_error(src, name):
        raise urllib.error.HTTPError(f"{src}/{name}", 503, "Unavailable", None, None)
    monkeypatch.setattr(P, "_read_bytes", server_error)
    with pytest.raises(urllib.error.HTTPError):
        P.ranking_bytes("https://x/ranking", 0)


class PayoutAccount:
    """A fake `dendrad query txs` over the payout account: the season's transfers as bank transactions, among
    unrelated ones, 100 per page."""

    def __init__(self, transfers, bad_message=False, change=None):
        self.txs = [{"txhash": f"O{k}", "tx": {"body": {"memo": f"other-{k}", "messages": []}}} for k in range(120)]
        for t in transfers:
            msgs = [{"@type": P._SEND, "from_address": PAYER, "to_address": a,
                     "amount": [{"denom": "udndr", "amount": str(v)}]} for a, v in t["sends"]]
            if bad_message:
                msgs[0]["amount"][0]["denom"] = "uother"
            if change is not None and t["txhash"] == "TX10":
                msgs = change(msgs)
            self.txs.append({"txhash": t["txhash"], "tx": {"body": {"memo": P.memo(t["week"], t["plan"], t["part"]),
                                                                    "messages": msgs}}})
        self.txs.append({"txhash": "FAILED", "code": 5,
                         "tx": {"body": {"memo": P.memo(0, "ffffffffffff", 7), "messages": []}}})

    def __call__(self, args, node, timeout=120):
        page = int(args[args.index("--page") + 1])
        return json.dumps({"txs": self.txs[(page - 1) * 100: page * 100], "total_count": str(len(self.txs))})


def test_the_transfers_are_read_from_the_chain_and_a_failed_one_is_not_a_payment(tmp_path, monkeypatch):
    publish(tmp_path, range(10), end_day=9)
    transfers = paid_as_planned(tmp_path, (0, 1))
    monkeypatch.setattr(P, "_cli", PayoutAccount(transfers))
    assert P.payout_transfers("tcp://n:26657", PAYER) == transfers


def test_a_season_transaction_that_is_not_plain_udndr_transfers_refuses_the_list(tmp_path, monkeypatch):
    publish(tmp_path, range(10), end_day=9)
    monkeypatch.setattr(P, "_cli", PayoutAccount(paid_as_planned(tmp_path, (0, 1)), bad_message=True))
    with pytest.raises(RuntimeError, match="cannot be counted"):
        P.payout_transfers("tcp://n:26657", PAYER)


# The search finds every transaction the payer signed (`message.sender`), so a transaction may carry the season's
# memo and still move another account's coins, a zero, or nothing at all: none of them is a payment of the season.
@pytest.mark.parametrize("change", [
    lambda msgs: [dict(msgs[0], from_address=A)] + msgs[1:],
    lambda msgs: [dict(msgs[0], amount=[{"denom": "udndr", "amount": "0"}])] + msgs[1:],
    lambda msgs: [],
], ids=["sent-by-another-account", "zero-udndr", "no-message"])
def test_a_season_transaction_that_is_not_a_transfer_from_the_payer_refuses_the_list(tmp_path, monkeypatch, change):
    publish(tmp_path, range(10), end_day=9)
    transfers = paid_as_planned(tmp_path, (0, 1))
    monkeypatch.setattr(P, "_cli", PayoutAccount(transfers))
    assert P.payout_transfers("tcp://n:26657", PAYER) == transfers      # the same account, unchanged, is read
    monkeypatch.setattr(P, "_cli", PayoutAccount(transfers, change=change))
    with pytest.raises(RuntimeError, match="TX10 carries the memo of week 1 part 0"):
        P.payout_transfers("tcp://n:26657", PAYER)


def test_the_list_refuses_days_ranked_otherwise_than_the_payers_first_doubled_day(tmp_path):
    publish(tmp_path, range(10), end_day=9)             # every day under the rates before decision 22
    days, digests, end = P.season_days(str(tmp_path))
    transfers = paid_as_planned(tmp_path, (0, 1))
    assert P.season_list(days, digests, end, transfers, None, PAYER)["last_day"] == 9
    with pytest.raises(ValueError, match="day 4: not ranked under the doubled rates"):
        P.season_list(days, digests, end, transfers, 4, PAYER)


def test_the_list_refuses_a_ranking_filed_under_another_day(tmp_path):
    publish(tmp_path, range(3), end_day=2)
    (tmp_path / "day-000.json").write_bytes((tmp_path / "day-001.json").read_bytes())
    with pytest.raises(ValueError, match="day-000.json says it is day 1"):
        P.season_days(str(tmp_path))


# The two sum checks at the end of `season_list` restate the per-part check: no input reaches them while `parts`
# and `week_plan` lose nothing. These cases break each helper and require the check to say so.
def test_the_list_refuses_when_the_parts_of_a_week_lose_an_address(tmp_path, monkeypatch):
    publish(tmp_path, range(10), end_day=9)
    monkeypatch.setattr(P, "parts", lambda payable: [sorted(payable.items())[:-1]])
    transfers = paid_as_planned(tmp_path, (0, 1))       # each part sent exactly as the lossy split lists it
    with pytest.raises(ValueError, match="the payout account sent"):
        build(tmp_path, transfers)


def test_the_list_refuses_when_the_set_aside_amounts_are_lost(tmp_path, monkeypatch):
    publish(tmp_path, range(3), end_day=2, blocked=True)
    transfers = paid_as_planned(tmp_path, (0,))
    week_plan = P.week_plan
    monkeypatch.setattr(P, "week_plan", lambda days: (week_plan(days)[0], {}))
    with pytest.raises(ValueError, match="do not add up to what the rankings paid"):
        build(tmp_path, transfers)


def _cli_season(tmp_path, monkeypatch, out, transfers):
    monkeypatch.setattr(P, "_cli", PayoutAccount(transfers))
    monkeypatch.setattr(P.C, "require_index_from", lambda rpc, h: None)
    return P.main(["--season-list", str(out), "--payer", PAYER, "--rankings", str(tmp_path / "r"),
                   "--doubled-from-day", "none"])


def test_the_command_writes_the_list_and_its_sha256_and_a_second_run_changes_nothing(tmp_path, monkeypatch, capsys):
    (tmp_path / "r").mkdir()
    publish(tmp_path / "r", range(10), end_day=9)
    transfers = paid_as_planned(tmp_path / "r", (0, 1))
    out = tmp_path / "season-list.json"
    assert _cli_season(tmp_path, monkeypatch, out, transfers) == 0
    data = out.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert (tmp_path / "season-list.json.sha256").read_bytes() == f"{digest}  season-list.json".encode() + bytes([10])
    printed = capsys.readouterr().out
    assert f"sha256 {digest}" in printed and "equal to what the payout account sent" in printed
    assert json.loads(data)["paid_udndr"] == {A: 1_000_000, B: 500_000}
    assert _cli_season(tmp_path, monkeypatch, out, transfers) == 0
    assert out.read_bytes() == data and "(unchanged)" in capsys.readouterr().out


def test_a_list_already_written_with_other_bytes_is_kept_and_the_command_refuses(tmp_path, monkeypatch, capsys):
    (tmp_path / "r").mkdir()
    publish(tmp_path / "r", range(10), end_day=9)
    out = tmp_path / "season-list.json"
    out.write_bytes(b"{}" + bytes([10]))
    assert _cli_season(tmp_path, monkeypatch, out, paid_as_planned(tmp_path / "r", (0, 1))) == 3
    assert out.read_bytes() == b"{}" + bytes([10]) and "not replaced" in capsys.readouterr().out


def test_the_command_refuses_an_unpaid_week_and_writes_nothing(tmp_path, monkeypatch, capsys):
    (tmp_path / "r").mkdir()
    publish(tmp_path / "r", range(10), end_day=9)
    out = tmp_path / "season-list.json"
    assert _cli_season(tmp_path, monkeypatch, out, paid_as_planned(tmp_path / "r", (0,))) == 3
    assert not out.exists() and "not paid as planned" in capsys.readouterr().out


@pytest.mark.parametrize("payer", ["", "cosmos1abc", BLOCKED])
def test_the_payer_is_named_never_defaulted(tmp_path, monkeypatch, payer):
    (tmp_path / "r").mkdir()
    publish(tmp_path / "r", range(3), end_day=2)
    rc = P.main(["--season-list", str(tmp_path / "l.json"), "--payer", payer, "--rankings", str(tmp_path / "r"),
                 "--doubled-from-day", "none"])
    assert rc == 2 and not (tmp_path / "l.json").exists()


def test_the_list_needs_the_first_day_of_the_doubled_rates(tmp_path):
    with pytest.raises(SystemExit):
        P.main(["--season-list", str(tmp_path / "l.json"), "--payer", PAYER, "--rankings", str(tmp_path)])


# -- the archive of the rankings -------------------------------------------------------------------------------
def test_the_archive_copies_every_ranking_byte_for_byte_with_its_manifest(tmp_path, capsys):
    src, dest = tmp_path / "r", tmp_path / "archive"
    src.mkdir()
    publish(src, range(10), end_day=9)
    assert P.main(["--archive-rankings", str(dest), "--rankings", str(src)]) == 0
    names = [f"day-{d:03d}.json" for d in range(10)]
    assert sorted(os.listdir(dest)) == sorted(names + ["SHA256SUMS"])
    for n in names:
        assert (dest / n).read_bytes() == (src / n).read_bytes()
    manifest = (dest / "SHA256SUMS").read_bytes()
    assert manifest == b"".join(f"{hashlib.sha256((src / n).read_bytes()).hexdigest()}  {n}".encode() + bytes([10])
                                for n in names)
    printed = capsys.readouterr().out
    assert f"sha256 {hashlib.sha256(manifest).hexdigest()}  SHA256SUMS" in printed
    assert f"season's last block {END_HEIGHT}" in printed


def test_an_archive_of_a_running_season_says_its_last_day_is_not_there(tmp_path, capsys):
    src = tmp_path / "r"
    src.mkdir()
    publish(src, range(4))
    assert P.archive_rankings(str(src), str(tmp_path / "a"))[:2] == (4, None)
    assert P.main(["--archive-rankings", str(tmp_path / "b"), "--rankings", str(src)]) == 0
    assert "none carries the season's last block" in capsys.readouterr().out


def test_an_archive_is_written_once_into_an_empty_directory(tmp_path):
    src, dest = tmp_path / "r", tmp_path / "a"
    src.mkdir()
    dest.mkdir()
    publish(src, range(2))
    (dest / "keep.txt").write_bytes(b"x")
    assert P.main(["--archive-rankings", str(dest), "--rankings", str(src)]) == 3
    assert os.listdir(dest) == ["keep.txt"]
    with pytest.raises(ValueError, match="nothing to archive"):
        P.archive_rankings(str(tmp_path / "empty-src"), str(tmp_path / "c"))
    assert not (tmp_path / "c").exists()


def test_an_archive_refuses_a_ranking_filed_under_another_day(tmp_path):
    src = tmp_path / "r"
    src.mkdir()
    publish(src, range(2))
    os.replace(src / "day-001.json", src / "day-000.json")
    with pytest.raises(ValueError, match="says it is day 1"):
        P.archive_rankings(str(src), str(tmp_path / "a"))
