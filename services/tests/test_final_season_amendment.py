"""Bench of the amendments, the payment decided after the rankings for rows they did not pay (the header of
final_season_payout.py). The plan reads the rankings, the evidence and the relay (directories here) and never signs.
The payment runs against the fake chain of test_final_season_payout_due.py: it is finished by a later run after a
partial one, stops on a part under another digest, and refuses a ranking that changed. Weekly and amendment memos are
never confused, and the season's list counts every frozen amendment, or refuses."""
import hashlib
import json
import os
import re
import subprocess
import sys
import types
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_payout as P  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_address import module_address  # noqa: E402
from final_season_calc import NO_KIT_REPORT, Identity, compute_day, to_json  # noqa: E402
from final_season_evidence import Evidence  # noqa: E402
from test_final_season_payout import _inflated, _tamper, addr  # noqa: E402
from test_final_season_payout_due import CHAIN_ID, PAYER, FakeChain  # noqa: E402

KIT_MIN = 3                     # the floor of test_final_season_kit_rules.py: one floor per process
KIT_DAY = 10
DAY = R.RULES["day_blocks"]
END = 1_234_567
A, B, C, D = addr(1), addr(2), addr(3), addr(4)
DECISION = "R67, owner, 26/10/2026"
REASON = "kit 3 identities ranked without a signed capacity report"
# miner -> (verified requests, payout address, kit on a day of the kit set)
IDS = {"m1": (10, A, KIT_MIN),           # paid by the ranking: never a row
       "m2": (20, B, None),              # no report, answers with kit 3 deposits: a row on days 10 and 11
       "m3": (5, C, None),               # no report: no proof on day 10 (set apart), a proof on day 11
       "m4": (10, D, KIT_MIN - 1)}       # a stale kit, another reason: never a row
NOT_PROVEN = "no work_answer job of the day whose relay deposit declares kit_version 3 or above"
ROWS = [{"day": 10, "miner_id": "m2", "address": B, "udndr": 2_000_000,
         "kit_proof": {"job_id": "job102", "kit_version": 3}},
        {"day": 11, "miner_id": "m2", "address": B, "udndr": 2_000_000,
         "kit_proof": {"job_id": "job200", "kit_version": 4}},
        {"day": 11, "miner_id": "m3", "address": C, "udndr": 500_000,
         "kit_proof": {"job_id": "job201", "kit_version": 3}}]
APART = [{"day": 10, "miner_id": "m3", "address": C, "udndr": 500_000, "why": NOT_PROVEN}]


def floor():
    if R.KIT_MIN is None:
        R.set_kit_min(KIT_MIN)
    assert R.KIT_MIN == KIT_MIN


def ranked_digest(ev, day):
    """`inputs.evidence_sha256` of a day, as the service computes it: the records of days 0 to `day` its own reader
    gives (`final_season_server.ranking_inputs`), hashed as `final_season_rank.rank_day` hashes them."""
    recs = [r for d in range(day + 1) for r in Evidence(str(ev)).read(d)]
    return hashlib.sha256("".join(json.dumps(r, sort_keys=True) + chr(10) for r in recs).encode()).hexdigest()


def publish(root, n=12, end_day=None, change=None):
    """Days 0 to n - 1 ranked by `final_season_calc.compute_day` under the owner's sets (decision 18 from day 1, the
    doubled rates from 4, decision 23 from 6, the kit set from KIT_DAY), with the inputs a ranking carries, the digest
    of the evidence beside `root` included. `change(day, ids)` gives the identities a day is ranked on, to publish a
    ranking that changed."""
    floor()
    root.mkdir(exist_ok=True)
    paid = 0
    for d in range(n):
        rules = R.rules_for_day(d, 1, 4, 6, KIT_DAY)
        kit = R.pays_judge_presence(rules)
        ids = [Identity(m, verified_requests=q, payout_address=a, kit_version=k if kit else None)
               for m, (q, a, k) in IDS.items()]
        if change is not None:
            ids = change(d, ids)
        r = compute_day(d, ids, paid, rules)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": END if d == end_day else (d + 1) * DAY,
                         "season_end_height": END if d == end_day else None,
                         "start_height": 1, "finality_blocks": DAY + 440,
                         "evidence_sha256": ranked_digest(root.parent / "evidence", d)}
        if kit:
            out["inputs"]["kit_floor"] = {"kit_min": rules["kit_min"], "reports": {}}
        (root / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def answers(root):
    """The evidence of days 10 and 11, with a torn line, a record filed under another day and a grade that is not an
    answer, and the relay's answer deposits (`res/<job>__<miner>`). m2's job100 has no deposit, job101 declares kit 2,
    job102 kit 3; job099 (filed under day 9) and m3's graded job300 declare kit 3 and prove nothing."""
    ev, res = root / "evidence", root / "relay" / "res"
    ev.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    days = {10: [("m1", "job010", 10), ("m2", "job099", 9), ("m2", "job100", 10), ("m2", "job101", 10),
                 ("m2", "job102", 10), ("m4", "job400", 10)],
            11: [("m1", "job210", 11), ("m2", "job200", 11), ("m3", "job201", 11)]}
    for d, recs in days.items():
        lines = [json.dumps({"type": "work_answer", "day": day, "miner_id": m, "job_id": j, "answer": "a"},
                            sort_keys=True) for m, j, day in recs]
        lines.insert(1, "{torn")
        lines.append(json.dumps({"type": "work_grade", "day": d, "miner_id": "m3", "job_id": "job300",
                                 "coherent": True}, sort_keys=True))
        (ev / f"day-{d:03d}.jsonl").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    deposits = {"job099__m2": 3, "job101__m2": 2, "job102__m2": 3, "job200__m2": 4, "job201__m3": 3,
                "job400__m4": 2, "job010__m1": 3, "job210__m1": 3, "job300__m3": 3}
    for key, kit in deposits.items():
        (res / key).write_text(json.dumps({"nonce": "00", "ct": "ff", "kit_version": kit}), encoding="utf-8")
    return str(ev), str(root / "relay")


def make_season(tmp_path, monkeypatch, n=12, end_day=None):
    ev, relay = answers(tmp_path)
    publish(tmp_path / "r", n, end_day)
    monkeypatch.setattr(P, "AMENDMENTS_DIR", str(tmp_path / "amendments"))
    return types.SimpleNamespace(root=tmp_path, rankings=str(tmp_path / "r"), evidence=ev, relay=relay,
                                 amend=tmp_path / "amendments")


@pytest.fixture
def season(tmp_path, monkeypatch):
    """Rankings of days 0 to 11, the evidence, the relay, and an empty directory of amendments."""
    return make_season(tmp_path, monkeypatch)


@pytest.fixture
def ended(tmp_path, monkeypatch):
    """The same season, ended on day 11."""
    return make_season(tmp_path, monkeypatch, 12, end_day=11)


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


def plan(s, n=1, days=(10, 11), frozen=()):
    return P.plan_amendment(n, list(days), s.rankings, s.evidence, s.relay, DECISION, REASON, 4, list(frozen))


def freeze(s, doc):
    """Commits `doc` into the directory of amendments, as the owner would, in the bytes the plan writes."""
    s.amend.mkdir(exist_ok=True)
    (s.amend / f"amendment-{doc['id']:03d}.json").write_bytes(P.stable_bytes(doc))
    return P.amendment_digest(doc)


def amend(s, *extra, n=1, chain_id=CHAIN_ID):
    argv = ["--amendment", str(n), "--rankings", s.rankings, "--doubled-from-day", "4", *extra]
    return P.main(argv + (["--chain-id", chain_id] if chain_id else []))


def plan_argv(s, out, days="10,11"):
    return ["--plan-amendment", "1", "--days", days, "--rankings", s.rankings, "--evidence", s.evidence,
            "--relay", s.relay, "--decision", DECISION, "--reason", REASON, "--out", str(out),
            "--doubled-from-day", "4"]


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def memos(chain):
    return [t["tx"]["body"]["memo"] for t in chain.txs]


# -- the plan ----------------------------------------------------------------------------------------------------
def test_the_plan_takes_the_no_report_rows_with_a_kit_proof_and_sets_apart_the_others(season):
    doc = plan(season)
    assert doc == {"schema": 1, "id": 1, "decision": DECISION, "reason": REASON, "rows": ROWS, "set_apart": APART}
    # the amount is the row's gross, the reason the ranker's own words, and only rows paid nothing are taken
    day10 = {x.miner_id: x for x in P.load_day(season.rankings, 10).rows}
    assert day10["m2"].reason == NO_KIT_REPORT and (day10["m2"].gross, day10["m2"].paid) == (2_000_000, 0)
    assert day10["m4"].reason != NO_KIT_REPORT and day10["m1"].paid == 1_000_000
    assert P.amendment_payable(doc) == {B: 4_000_000, C: 500_000}


def test_the_plan_command_writes_the_file_once_reads_no_key_and_signs_nothing(season, chain, capsys):
    out = season.root / "plan" / "amendment-001.json"
    out.parent.mkdir()
    argv = plan_argv(season, out)
    assert P.main(argv) == 0
    data = out.read_bytes()
    assert data == P.stable_bytes(plan(season))
    assert (chain.signed, chain.keys_shown, chain.txs) == ([], 0, [])
    printed = capsys.readouterr().out
    assert f"amendment 1, digest {P.amendment_digest(json.loads(data))}: 3 row(s), 4500000 udndr" in printed
    assert f"set apart: day 10  m3  {C}  500000  ({NOT_PROVEN})" in printed
    assert P.main(argv) == 0 and "(unchanged)" in capsys.readouterr().out
    # nothing is defaulted: without the relay the plan is refused before any read
    assert P.main([x for x in argv if x not in ("--relay", season.relay)]) == 2


@pytest.mark.parametrize("case, why", [
    ("day-not-ranked", "day 11 is not ranked yet"),
    ("evidence-missing", "(day-010.jsonl is missing)"),
    ("relay-unreachable", "URLError"),
    ("day-without-floor", "day 9 is ranked under a set without a kit floor"),
])
def test_the_plan_refuses_what_it_cannot_read_and_writes_nothing(season, monkeypatch, capsys, case, why):
    days = "10,11"
    if case == "day-not-ranked":
        os.remove(os.path.join(season.rankings, "day-011.json"))
    elif case == "evidence-missing":
        os.remove(os.path.join(season.evidence, "day-010.jsonl"))
    elif case == "relay-unreachable":
        real = P._read_bytes

        def served(src, name):
            if src.endswith("/res"):
                raise urllib.error.URLError("connection refused")
            return real(src, name)
        monkeypatch.setattr(P, "_read_bytes", served)
    else:
        days = "9,10"
    out = season.root / "a.json"
    assert P.main(plan_argv(season, out, days)) == 3
    assert not out.exists() and why in capsys.readouterr().out


def test_a_frozen_amendment_is_never_planned_again_nor_its_rows_paid_twice(season):
    doc = plan(season)
    freeze(season, doc)
    frozen = P.load_amendments(str(season.amend))
    assert frozen == [(1, doc, P.amendment_digest(doc))]
    with pytest.raises(P.PlanRefused, match="amendment 1 is already frozen"):
        plan(season, frozen=frozen)
    with pytest.raises(P.PlanRefused, match="is in amendment 1 and in amendment 2: it would be paid twice"):
        plan(season, n=2, days=(11,), frozen=frozen)


def test_the_plan_reads_only_the_evidence_the_day_was_ranked_from(season):
    # an answer added after the ranking, for m3 on day 10 with a kit 3 deposit, would turn a row set apart into a
    # paid row: the files no longer give the ranking's digest, and the plan is refused
    p = Path(season.evidence) / "day-010.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + json.dumps({"type": "work_answer", "day": 10, "miner_id": "m3",
                                                             "job_id": "job300", "answer": "a"}) + chr(10),
                 encoding="utf-8")
    with pytest.raises(P.PlanRefused, match="the ranking of day 10 was computed from "):
        plan(season)
    publish(season.root / "r")                                            # ranked from those files, it is proven
    assert [(r["day"], r["miner_id"]) for r in plan(season)["rows"]] == [(10, "m2"), (10, "m3"), (11, "m2"),
                                                                         (11, "m3")]
    # a ranking that publishes no digest ties no evidence to it
    q = Path(season.rankings) / "day-011.json"
    d = json.loads(q.read_text(encoding="utf-8"))
    del d["inputs"]["evidence_sha256"]
    q.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(P.PlanRefused, match="day 11 publishes no evidence digest"):
        plan(season)


def _with_m5(tmp_path, monkeypatch, address=None, **flags):
    """A season where m5 answered on day 11 with a kit-3 deposit, has no signed report, and carries `flags`."""
    address = addr(5) if address is None else address
    ev, relay = answers(tmp_path)
    p = Path(ev) / "day-011.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + json.dumps({"type": "work_answer", "day": 11, "miner_id": "m5",
                                                             "job_id": "job500", "answer": "a"}) + chr(10),
                 encoding="utf-8")
    (tmp_path / "relay" / "res" / "job500__m5").write_text(json.dumps({"kit_version": 3}), encoding="utf-8")
    publish(tmp_path / "r", change=lambda d, ids: ids + [Identity("m5", verified_requests=8, payout_address=address,
                                                                  **{k: d >= KIT_DAY for k in flags})])
    monkeypatch.setattr(P, "AMENDMENTS_DIR", str(tmp_path / "amendments"))
    s = types.SimpleNamespace(rankings=str(tmp_path / "r"), evidence=ev, relay=relay)
    return s, next(x for x in P.load_day(s.rankings, 11).rows if x.miner_id == "m5")


def test_a_probation_row_refused_for_the_report_is_paid_its_already_reduced_gross(tmp_path, monkeypatch):
    # m5 is on probation on the kit days and has no report: "probation: ...; no signed capacity report ...". The
    # ranking's gross is already the probation's share, so the missing report alone kept it unpaid.
    s, m5 = _with_m5(tmp_path, monkeypatch, probation=True)
    assert m5.probation and m5.reason.startswith("probation: ") and m5.reason.endswith("; " + NO_KIT_REPORT)
    assert m5.gross > 0 and m5.paid == 0
    doc = plan(s, days=(11,))
    assert [r["miner_id"] for r in doc["rows"]] == ["m2", "m3", "m5"]
    assert next(r for r in doc["rows"] if r["miner_id"] == "m5")["udndr"] == m5.gross
    assert doc["set_apart"] == []


def test_a_row_refused_for_the_report_and_another_reason_is_set_apart_never_dropped(tmp_path, monkeypatch):
    # m5's registration cannot be dated and it has no report: the other reason is a refusal of its own, so the
    # missing report is not what alone kept it unpaid.
    s, m5 = _with_m5(tmp_path, monkeypatch, registration_not_dated=True)
    assert NO_KIT_REPORT in m5.reason.split("; ") and m5.reason != NO_KIT_REPORT and m5.gross > 0
    assert not m5.reason.startswith("probation: ")
    doc = plan(s, days=(11,))
    assert [r["miner_id"] for r in doc["rows"]] == ["m2", "m3"]
    assert doc["set_apart"] == [{"day": 11, "miner_id": "m5", "address": addr(5), "udndr": m5.gross,
                                 "why": f"the ranking gives another reason beside the missing report: {m5.reason}"}]


def test_a_second_reason_other_than_the_probation_note_keeps_the_row_apart(tmp_path, monkeypatch):
    # Two parts ending with the missing report, the first one not the probation's note: "no payout address known;
    # no signed capacity report ...". Only the probation's note is already accounted for in the gross.
    s, m5 = _with_m5(tmp_path, monkeypatch, address="")
    assert m5.reason.split("; ")[-1] == NO_KIT_REPORT and len(m5.reason.split("; ")) == 2 and not m5.probation
    doc = plan(s, days=(11,))
    assert doc["set_apart"] == [{"day": 11, "miner_id": "m5", "address": "", "udndr": m5.gross,
                                 "why": f"the ranking gives another reason beside the missing report: {m5.reason}"}]


def test_a_kit_day_row_without_its_kit_field_is_not_read_as_a_row_without_a_report(season):
    q = Path(season.rankings) / "day-011.json"
    d = json.loads(q.read_text(encoding="utf-8"))
    for x in d["ranking"]:
        if x["miner_id"] == "m2":
            del x["kit_version"]
    q.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(P.DayUnreadable, match="day 11 is not ranked yet or unreadable [(]KeyError[)]"):
        plan(season)


# -- the payment -------------------------------------------------------------------------------------------------
def test_the_payment_without_yes_says_what_it_would_send_and_signs_nothing(season, chain, capsys):
    digest = freeze(season, plan(season))
    assert amend(season, chain_id="") == 0
    out = lines(capsys)
    assert (f"amendment 1, digest {digest}: 3 row(s), 4500000 udndr to 2 address(es) in 1 part(s); 1 row(s) set "
            f"apart") in out
    assert "plan only, nothing signed: part(s) [0] would be sent. Add --yes to pay." in out
    assert chain.signed == [] and chain.txs == []


def test_the_payment_sends_each_part_once_under_its_memo(season, chain, capsys, monkeypatch):
    monkeypatch.setattr(P, "BATCH", 1)
    doc = plan(season)
    digest = freeze(season, doc)
    assert amend(season, "--yes") == 0
    chunks = P.parts(P.amendment_payable(doc))
    assert len(chunks) == 2 and memos(chain) == [P.amend_memo(1, digest, i) for i in range(2)]
    assert P.amendment_transfers("tcp://chain:26657", PAYER) == [
        {"amendment": 1, "plan": digest, "part": i, "txhash": f"TX{i}", "sends": [[a, v] for a, v in chunk]}
        for i, chunk in enumerate(chunks)]
    assert P.payout_transfers("tcp://chain:26657", PAYER) == []          # an amendment's memo names no week
    assert lines(capsys)[-1] == "amendment 1 paid, part(s) [0, 1] sent."
    assert amend(season, "--yes") == 0
    assert len(chain.txs) == 2 and len(chain.signed) == 2
    assert lines(capsys)[-1] == f"amendment 1 is already paid in full under digest {digest}: nothing sent."


def test_an_interrupted_payment_is_finished_by_the_next_run_and_never_sent_twice(season, chain, capsys, monkeypatch):
    monkeypatch.setattr(P, "BATCH", 1)
    digest = freeze(season, plan(season))
    chain.refuse_part = 1
    assert amend(season, "--yes") == 3
    assert memos(chain) == [P.amend_memo(1, digest, 0)]
    assert any(x.startswith("amendment 1: failed: RuntimeError: part 1 refused") for x in lines(capsys))
    chain.refuse_part = None
    assert amend(season, "--yes") == 0
    assert memos(chain) == [P.amend_memo(1, digest, i) for i in range(2)]
    assert lines(capsys)[-1] == "amendment 1 paid, part(s) [1] sent."
    assert amend(season, "--yes") == 0 and len(chain.txs) == 2


def test_a_part_on_chain_under_another_digest_stops_everything(season, chain, capsys):
    freeze(season, plan(season))
    send = {"@type": P._SEND, "from_address": PAYER, "to_address": B, "amount": [{"denom": "udndr", "amount": "1"}]}
    chain.txs.append({"txhash": "OLD", "code": 0,
                      "tx": {"body": {"memo": P.amend_memo(1, "000000000000", 0), "messages": [send]}}})
    assert amend(season, "--yes") == 3
    assert len(chain.txs) == 1 and chain.signed == []
    assert any(x.startswith("Refused: part(s) [0] of amendment 1 are on chain under another digest")
               for x in lines(capsys))


def test_an_amendment_paid_under_a_file_gone_from_the_directory_refuses_another(ended, chain, capsys):
    # the image rebuilt from a tree without amendment-001.json: its rows are planned again as amendment 2
    one = plan(ended)
    freeze(ended, one)
    assert amend(ended, "--yes") == 0
    os.remove(ended.amend / "amendment-001.json")
    two = plan(ended, n=2)
    assert two["rows"] == one["rows"]
    freeze(ended, two)
    assert amend(ended, "--yes", n=2) == 3
    assert memos(chain) == [P.amend_memo(1, P.amendment_digest(one), 0)]
    assert any("amendment(s) [1] are paid on chain, and no frozen amendment file names them" in x for x in lines(capsys))


def test_an_amendment_file_edited_after_its_payment_refuses_another(ended, chain, capsys):
    one = plan(ended)
    freeze(ended, one)
    assert amend(ended, "--yes") == 0
    # day 11 dropped from amendment 1 after it was paid, then planned again as amendment 2
    edited = dict(one, rows=[r for r in one["rows"] if r["day"] == 10])
    freeze(ended, edited)
    two = plan(ended, n=2, days=(11,), frozen=P.load_amendments(str(ended.amend)))
    freeze(ended, two)
    assert amend(ended, "--yes", n=2) == 3
    assert len(chain.txs) == 1
    assert any(f"amendment 1: part(s) [0] are on chain under another digest than its file gives "
               f"({P.amendment_digest(edited)})" in x for x in lines(capsys))


def on_day_10(fn):
    """A ranking that changed: `fn(identity)` gives m2 of day 10 as it is ranked now, or None to drop it."""
    def change(d, ids):
        if d != 10:
            return ids
        return [x for x in (fn(i) if i.miner_id == "m2" else i for i in ids) if x is not None]
    return change


def _with(**kw):
    def fn(i):
        for k, v in kw.items():
            setattr(i, k, v)
        return i
    return fn


@pytest.mark.parametrize("change, why", [
    (on_day_10(_with(verified_requests=21)), "2000000 udndr, but the ranking's gross is 2100000"),
    (on_day_10(_with(payout_address=addr(5))), "but the ranking names"),
    (on_day_10(_with(kit_version=KIT_MIN)), "the ranking now gives the reason '' and the kit 3"),
    (on_day_10(lambda i: None), "the identity is not in the day's ranking"),
    (None, "day 11 is not ranked yet"),
], ids=["amount", "address", "now-paid", "gone", "day-not-ranked"])
def test_a_ranking_that_changed_refuses_the_payment(season, chain, capsys, change, why):
    freeze(season, plan(season))
    if change is None:
        os.remove(os.path.join(season.rankings, "day-011.json"))
    else:
        publish(season.root / "r", change=change)
    assert amend(season, "--yes") == 3
    assert why in capsys.readouterr().out
    assert chain.signed == [] and chain.txs == []


def test_the_day_cap_and_the_season_cap_count_the_amendments(season):
    # day 10: m1 ranked 99 DNDR of the day's 100; m2's 2 DNDR would pass the day's cap
    def heavy(d, ids):
        return [Identity("m1", verified_requests=990, payout_address=A, kit_version=KIT_MIN)
                if (d == 10 and i.miner_id == "m1") else i for i in ids]
    publish(season.root / "r", change=heavy)
    with pytest.raises(P.PlanRefused, match="day 10: the ranking paid 99000000 and the amendments add 2000000, over "
                                            "the day's cap of 100000000"):
        plan(season)
    publish(season.root / "r")
    doc = plan(season)
    days, frozen = P._amendment_days(season.rankings, 11, 4), [(1, doc, P.amendment_digest(doc))]
    assert P.amendment_refusal(1, frozen, days) == ""
    cap = R.RULES_KIT["cap_season"]
    days[0].total_paid += cap - sum(d.total_paid for d in days) - 4_500_000 + 1
    assert P.amendment_refusal(1, frozen, days).endswith(f"over the season's cap of {cap}")
    days[0].total_paid -= 1
    assert P.amendment_refusal(1, frozen, days) == ""                      # at the cap exactly, it is paid


def test_the_caps_add_every_amendment_not_only_the_one_paid(season):
    # day 11: m1 ranked 97.6 DNDR; amendment 1 (m2, 2 DNDR) fits the day's cap alone, with amendment 2 (m3, 0.5 DNDR)
    # the day passes it, whichever of the two is paid
    def heavy(d, ids):
        return [Identity("m1", verified_requests=976, payout_address=A, kit_version=KIT_MIN)
                if (d == 11 and i.miner_id == "m1") else i for i in ids]
    publish(season.root / "r", change=heavy)
    days = P._amendment_days(season.rankings, 11, 4)
    assert days[11].total_paid == 97_600_000
    one, two = {"id": 1, "rows": [ROWS[1]]}, {"id": 2, "rows": [ROWS[2]]}
    first, both = [(1, one, "a")], [(1, one, "a"), (2, two, "b")]
    assert P.amendment_refusal(1, first, days) == ""
    for n in (1, 2):
        assert P.amendment_refusal(n, both, days) == ("day 11: the ranking paid 97600000 and the amendments add "
                                                      "2500000, over the day's cap of 100000000")
    # the season's cap: room for amendment 1 alone, not for both
    publish(season.root / "r")
    days = P._amendment_days(season.rankings, 11, 4)
    cap = R.RULES_KIT["cap_season"]
    days[0].total_paid += cap - sum(d.total_paid for d in days) - 2_000_000
    assert P.amendment_refusal(1, first, days) == ""
    assert P.amendment_refusal(1, both, days).endswith(f"the amendments add 2500000, over the season's cap of {cap}")


def test_a_row_ranked_with_a_kit_is_refused_even_with_the_reports_words(season):
    # a stale kit (2) is not paid either, but it is not a missing report: only `check_days` stands before this
    doc = plan(season)
    days = P._amendment_days(season.rankings, 11, 4)
    next(x for x in days[10].rows if x.miner_id == "m2").kit_version = KIT_MIN - 1
    assert P.amendment_refusal(1, [(1, doc, P.amendment_digest(doc))], days) == (
        f"amendment 1, day 10, m2: the ranking now gives the reason {NO_KIT_REPORT!r} and the kit 2")


def test_rankings_out_of_the_rules_refuse_the_plan_and_the_payment(season, chain, capsys):
    freeze(season, plan(season))
    _tamper(Path(season.rankings), 3, _inflated)
    with pytest.raises(P.PlanRefused, match="Refused: day 3: "):
        plan(season)
    assert amend(season, "--yes") == 3
    assert any(x.startswith("Refused: day 3: ") for x in lines(capsys)) and chain.signed == []


def test_two_amendments_are_paid_each_under_its_own_memo_and_both_listed(ended, chain, capsys):
    one = plan(ended, days=(10,))
    freeze(ended, one)
    two = plan(ended, n=2, days=(11,), frozen=P.load_amendments(str(ended.amend)))
    freeze(ended, two)
    assert amend(ended, "--yes") == 0 and amend(ended, "--yes", n=2) == 0
    assert memos(chain) == [P.amend_memo(1, P.amendment_digest(one), 0), P.amend_memo(2, P.amendment_digest(two), 0)]
    assert amend(ended, "--yes", n=2) == 0 and len(chain.txs) == 2
    week = ["--rankings", ended.rankings, "--doubled-from-day", "4"]
    assert P.main(["--due", *week, "--yes", "--chain-id", CHAIN_ID]) == 0
    out = ended.root / "season-list.json"
    assert P.main(["--season-list", str(out), "--payer", PAYER, *week]) == 0
    doc = json.loads(out.read_bytes())
    assert [(x["id"], x["total_udndr"]) for x in doc["amendments"]] == [(1, 2_000_000), (2, 2_500_000)]
    assert doc["total_amended_udndr"] == 4_500_000


def test_a_row_the_ranking_now_pays_is_refused_whatever_its_reason(season):
    # `check_days` refuses such a published day before `amendment_refusal` reads it; the refusal holds on its own.
    doc = plan(season)
    days = P._amendment_days(season.rankings, 11, 4)
    row = next(x for x in days[10].rows if x.miner_id == "m2")
    row.paid = 1
    assert P.amendment_refusal(1, [(1, doc, P.amendment_digest(doc))], days).endswith("payable 0, paid 1")


def test_a_kit_proof_below_the_days_floor_is_refused(season):
    doc = plan(season)
    doc["rows"][0]["kit_proof"]["kit_version"] = KIT_MIN - 1
    days = P._amendment_days(season.rankings, 11, 4)
    assert "kit proof 2, the day's floor is 3" in P.amendment_refusal(1, [(1, doc, P.amendment_digest(doc))], days)


def test_weekly_memos_and_amendment_memos_are_never_confused(tmp_path, monkeypatch, chain, capsys):
    s = make_season(tmp_path, monkeypatch, 14)
    dg = "abcdef012345"
    assert P._MEMO.match(P.amend_memo(1, dg, 0)) is None and P._AMEND_MEMO.match(P.memo(1, dg, 0)) is None
    digest = freeze(s, plan(s))
    assert amend(s, "--yes") == 0                                         # amendment 1 part 0, first on chain
    week = ["--rankings", s.rankings, "--doubled-from-day", "4", "--yes", "--chain-id", CHAIN_ID]
    assert P.main(["--week", "1", *week]) == 0                            # not "under another plan"
    assert P.main(["--due", *week]) == 0
    assert lines(capsys)[-1] == "summary: weeks paid now [0], already paid [1]; week 2 waits for day 14 (exit 0)"
    assert amend(s, "--yes") == 0                                         # week 1 part 0 is not amendment 1 part 0
    assert lines(capsys)[-1] == f"amendment 1 is already paid in full under digest {digest}: nothing sent."
    assert [t["week"] for t in P.payout_transfers("tcp://chain:26657", PAYER)] == [1, 0]
    assert [t["amendment"] for t in P.amendment_transfers("tcp://chain:26657", PAYER)] == [1]


def now_reported(d, ids):
    """Days 10 and 11 ranked again with a report for m2 and m3: the rows amendment 1 pays are now paid by the ranking."""
    if d in (10, 11):
        for i in ids:
            if i.miner_id in ("m2", "m3"):
                i.kit_version = KIT_MIN
    return ids


def test_a_week_is_not_paid_when_its_ranking_now_pays_a_row_an_amendment_pays(tmp_path, monkeypatch, chain, capsys):
    s = make_season(tmp_path, monkeypatch, 14)
    digest = freeze(s, plan(s))
    assert amend(s, "--yes") == 0
    publish(s.root / "r", 14, change=now_reported)
    week = ["--rankings", s.rankings, "--doubled-from-day", "4", "--yes", "--chain-id", CHAIN_ID]
    with pytest.raises(SystemExit, match=re.escape("Refused: amendment 1, day 10, m2: the ranking now gives the reason "
                                                   "'' and the kit 3; a week paying that day could pay the row twice. "
                                                   "Week 1 is not paid; nothing is signed.")):
        P.main(["--week", "1", *week])
    capsys.readouterr()
    assert P.main(["--due", *week]) == 3                                  # the cron: week 0 paid, week 1 refused
    assert lines(capsys)[-1] == ("summary: weeks paid now [0], already paid []; week 1 refused, no later week paid "
                                 "(exit 3)")
    assert [t["week"] for t in P.payout_transfers("n", PAYER)] == [0]
    assert memos(chain)[0] == P.amend_memo(1, digest, 0)
    # without the frozen file the same rankings plan week 1: the refusal comes from the amendment alone
    os.remove(s.amend / "amendment-001.json")
    assert P.plan_days(1, s.rankings, 4)[2] == list(range(7, 14))


# -- the season's list -------------------------------------------------------------------------------------------
def paid_weeks(rankings, weeks):
    out = []
    for k in weeks:
        payable = P.plan_days(k, rankings, 4)[0]
        dg = P.plan_digest(payable)
        for i, chunk in enumerate(P.parts(payable)):
            out.append({"week": k, "plan": dg, "part": i, "txhash": f"W{k}{i}", "sends": [[a, v] for a, v in chunk]})
    return out


def paid_amendment(doc, n=1):
    dg = P.amendment_digest(doc)
    return [{"amendment": n, "plan": dg, "part": i, "txhash": f"A{n}{i}", "sends": [[a, v] for a, v in chunk]}
            for i, chunk in enumerate(P.parts(P.amendment_payable(doc)))]


def test_the_season_list_adds_the_amendment_per_address(ended):
    doc = plan(ended)
    days, digests, end = P.season_days(ended.rankings)
    weeks = paid_weeks(ended.rankings, (0, 1))
    without = P.season_list(days, digests, end, weeks, 4, PAYER, [], [])
    frozen = [(1, doc, P.amendment_digest(doc))]
    listed = P.season_list(days, digests, end, weeks, 4, PAYER, frozen, paid_amendment(doc))
    added = {B: 4_000_000, C: 500_000}
    assert listed["paid_udndr"] == {a: without["paid_udndr"].get(a, 0) + added.get(a, 0)
                                    for a in set(without["paid_udndr"]) | set(added)}
    assert listed["total_paid_udndr"] == without["total_paid_udndr"] + 4_500_000
    assert (listed["total_amended_udndr"], without["total_amended_udndr"], without["amendments"]) == (4_500_000, 0, [])
    assert listed["amendments"] == [{"id": 1, "plan": P.amendment_digest(doc), "decision": DECISION,
                                     "total_udndr": 4_500_000, "txs": ["A10"]}]
    assert listed["rankings_total_paid_udndr"] == without["rankings_total_paid_udndr"] and listed["schema"] == 2


@pytest.mark.parametrize("change, why", [
    (lambda am: [], "amendment 1 is not paid as frozen: part(s) [0] of 1 missing"),
    (lambda am: am + [dict(am[0], txhash="TWICE")], "amendment 1 part 0 is on chain twice"),
    (lambda am: [dict(am[0], plan="000000000000")], "are on chain under another digest than its file gives"),
    (lambda am: [dict(am[0], sends=am[0]["sends"][:1])], "did not send the transfers its file lists"),
    (lambda am: am + [dict(am[0], amendment=2, txhash="STRAY")],
     "amendment(s) [2] are paid on chain, and no frozen amendment file names them"),
    (lambda am: am + [dict(am[0], part=1, txhash="EXTRA")], "part(s) [1] beyond it"),
], ids=["not-paid", "part-twice", "other-digest", "transfer-missing", "no-file", "part-beyond"])
def test_an_amendment_out_of_line_refuses_the_season_list(ended, change, why):
    doc = plan(ended)
    days, digests, end = P.season_days(ended.rankings)
    with pytest.raises(ValueError, match=re.escape(why)):
        P.season_list(days, digests, end, paid_weeks(ended.rankings, (0, 1)), 4, PAYER,
                      [(1, doc, P.amendment_digest(doc))], change(paid_amendment(doc)))


def test_an_amendment_of_a_day_after_the_season_refuses_the_list(ended):
    doc = plan(ended)
    doc["rows"].append({"day": 12, "miner_id": "m2", "address": B, "udndr": 2_000_000,
                        "kit_proof": {"job_id": "job300", "kit_version": 3}})
    days, digests, end = P.season_days(ended.rankings)
    with pytest.raises(ValueError, match="amendment 1, day 12, m2: the day is not ranked"):
        P.season_list(days, digests, end, paid_weeks(ended.rankings, (0, 1)), 4, PAYER,
                      [(1, doc, P.amendment_digest(doc))], paid_amendment(doc))


def test_the_season_list_command_counts_the_amendment_paid_on_chain(ended, chain, capsys):
    freeze(ended, plan(ended))
    week = ["--rankings", ended.rankings, "--doubled-from-day", "4"]
    assert amend(ended, "--yes") == 0
    assert P.main(["--due", *week, "--yes", "--chain-id", CHAIN_ID]) == 0
    out = ended.root / "season-list.json"
    assert P.main(["--season-list", str(out), "--payer", PAYER, *week]) == 0
    doc = json.loads(out.read_bytes())
    assert doc["total_amended_udndr"] == 4_500_000 and doc["amendments"][0]["txs"] == ["TX0"]
    sent = sum(v for t in P.payout_transfers("n", PAYER) + P.amendment_transfers("n", PAYER) for _a, v in t["sends"])
    assert doc["total_paid_udndr"] == sent
    assert "4500000 of them by 1 amendment(s)" in capsys.readouterr().out
    # the frozen file removed, its payment on chain refuses the list
    os.remove(ended.amend / "amendment-001.json")
    other = ended.root / "other-list.json"
    assert P.main(["--season-list", str(other), "--payer", PAYER, *week]) == 3 and not other.exists()
    assert "no frozen amendment file names them" in capsys.readouterr().out


# -- the frozen file ---------------------------------------------------------------------------------------------
def test_no_directory_holds_no_amendment_and_a_stray_file_is_refused(tmp_path):
    assert P.load_amendments(str(tmp_path / "none")) == []
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "README.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="is not an amendment file"):
        P.load_amendments(str(tmp_path / "a"))


@pytest.mark.parametrize("change, why", [
    (lambda d: d.update(extra=1), "carries exactly the keys"),
    (lambda d: d.pop("reason"), "carries exactly the keys"),
    (lambda d: d.update(id=2), "id 2, the file is amendment 1"),
    (lambda d: d.update(schema=2), "schema 2"),
    (lambda d: d.update(decision=" "), "the decision and the reason are required"),
    (lambda d: d.update(rows=[]), "at least one row"),
    (lambda d: d["rows"].reverse(), "listed twice or out of order"),
    (lambda d: d["rows"].append(dict(d["rows"][-1])), "listed twice or out of order"),
    (lambda d: d["set_apart"].append(dict(d["set_apart"][0], day=11, miner_id="m2")), "listed twice or out of order"),
    (lambda d: d["rows"][0].update(udndr=0), "is not a whole number above 0"),
    (lambda d: d["rows"][0].update(udndr=2_000_000.0), "is not a whole number above 0"),
    (lambda d: d["rows"][0].update(address=module_address("bonded_tokens_pool")), "a module account"),
    (lambda d: d["rows"][0]["kit_proof"].update(kit_version=True), "no well formed kit_proof"),
    (lambda d: d["rows"][0]["kit_proof"].update(job_id="../x"), "no well formed kit_proof"),
    (lambda d: d["rows"][0]["kit_proof"].pop("job_id"), "no well formed kit_proof"),
    (lambda d: d["rows"][0].update(miner_id="m/2"), "cannot name a relay deposit"),
    (lambda d: d["set_apart"][0].update(why=""), "says why"),
], ids=["extra-key", "missing-key", "other-id", "other-schema", "blank-decision", "no-row", "unsorted", "row-twice",
        "paid-and-set-apart", "zero", "float", "module-account", "bool-kit", "job-path", "no-job", "miner-path",
        "no-why"])
def test_an_amendment_file_out_of_form_is_refused(season, change, why):
    doc = plan(season)
    change(doc)
    season.amend.mkdir()
    (season.amend / "amendment-001.json").write_bytes(P.stable_bytes(doc))
    with pytest.raises(ValueError, match=re.escape(why)):
        P.load_amendments(str(season.amend))


def test_the_digest_reads_the_content_and_a_key_twice_is_refused(season):
    doc = plan(season)
    season.amend.mkdir()
    lf = P.stable_bytes(doc)
    p = season.amend / "amendment-001.json"
    p.write_bytes(lf.replace(bytes([10]), bytes([13, 10])))               # a checkout that rewrote the line endings
    assert P.load_amendments(str(season.amend)) == [(1, doc, P.amendment_digest(doc))]
    assert P.amendment_digest(doc) == hashlib.sha256(lf).hexdigest()[:12]
    p.write_bytes(lf.replace(b'"decision"', b'"reason": "x", "decision"', 1))
    with pytest.raises(ValueError, match="appears twice"):
        P.load_amendments(str(season.amend))
