"""Bench of `final_season_chain.presence_windows`, with the node replaced by fabricated `query txs` pages.

What it pins: only an availability proof the chain ACCEPTED, at a height inside the day, makes a miner
present; another message of the same transaction naming a miner_id does not; a window counts once
however many proofs it holds (window = height // avail_epoch_blocks, the arithmetic of the chain's
own ProveAvailability handler); every page is read; and a read that fails RAISES, it never returns {}:
"nobody proved a window" pays no presence for a reason, "the node did not answer" would pay none by
accident.
"""
import json
import os
import subprocess
import sys

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, MODEA or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_chain as C  # noqa: E402

AVAIL = "/dendra.jobs.v1.MsgProveAvailability"
NODE = "tcp://chain:26657"
MAX_PER_PAGE = 100          # the node's own ceiling on a page, whatever limit is asked


def proof(mid):
    return {"@type": AVAIL, "creator": "dendra1op" + mid, "miner_id": mid, "challenge": "c", "vrf_proof": "00"}


def tx(height, *messages, code=None):
    t = {"height": str(height), "txhash": f"H{height}", "tx": {"body": {"messages": list(messages)}}}
    if code is not None:
        t["code"] = code
    return t


class Pages:
    """A fake `dendrad query txs`: serves the given transactions page by page, at the limit asked and
    never more than the node's ceiling, announces the total the way the CLI prints it (a string), and
    records every call. It does not filter on the query: the reader must not rely on the node for that."""

    def __init__(self, txs, fail_on_page=None):
        self.txs, self.fail_on_page, self.calls = list(txs), fail_on_page, []

    def __call__(self, args, node, timeout=60):
        self.calls.append(list(args))
        assert args[:2] == ["query", "txs"] and node == NODE
        page = int(args[args.index("--page") + 1])
        if page == self.fail_on_page:
            raise C.ChainUnreadable("query txs: rc=1 post failed")
        per = min(int(args[args.index("--limit") + 1]), MAX_PER_PAGE)
        chunk = self.txs[(page - 1) * per: page * per]
        return {"total_count": str(len(self.txs)), "count": str(len(chunk)), "page_number": str(page),
                "limit": str(per), "txs": chunk}

    def pages(self):
        return [c[c.index("--page") + 1] for c in self.calls]


def read(monkeypatch, txs, first, last, epoch, **kw):
    fake = Pages(txs, kw.pop("fail_on_page", None))
    monkeypatch.setattr(C, "_cli", fake)
    return C.presence_windows(NODE, first, last, epoch, **kw), fake


def test_only_the_availability_message_makes_a_miner_present(monkeypatch):
    other = {"@type": "/dendra.jobs.v1.MsgUpdateMiner", "creator": "dendra1x", "miner_id": "m2"}
    near = dict(proof("m3"), **{"@type": "/dendra.jobs.v2.MsgProveAvailability"})
    untyped = {"miner_id": "m4", "challenge": "c"}
    out, fake = read(monkeypatch, [tx(1005, other, proof("m1"), near, untyped)], 1000, 1999, 100)
    assert out == {"m1": 1}
    q = fake.calls[0][fake.calls[0].index("--query") + 1]
    assert f"message.action='{AVAIL}'" in q and "tx.height>=1000" in q and "tx.height<=1999" in q


def test_the_camel_case_field_is_read_too(monkeypatch):
    out, _ = read(monkeypatch, [tx(1005, {"@type": AVAIL, "minerId": "m5"})], 1000, 1999, 100)
    assert out == {"m5": 1}


def test_a_refused_transaction_proves_nothing(monkeypatch):
    txs = [tx(1005, proof("absent")),                   # proto3 omits a zero code: accepted
           tx(1006, proof("zero"), code=0),
           tx(1007, proof("zero_text"), code="0"),
           tx(1008, proof("refused"), code=18),
           tx(1009, proof("refused_text"), code="5"),
           tx(1010, proof("refused_mixed"), {"@type": "/dendra.jobs.v1.MsgUpdateMiner", "miner_id": "x"}, code=4)]
    out, _ = read(monkeypatch, txs, 1000, 1999, 100)
    assert out == {"absent": 1, "zero": 1, "zero_text": 1}


def test_heights_outside_the_day_are_ignored(monkeypatch):
    txs = [tx(999, proof("before")), tx(1000, proof("first")), tx(1999, proof("last")),
           tx(2000, proof("after")),
           {"tx": {"body": {"messages": [proof("no_height")]}}}]    # no height: 0, outside the day
    out, _ = read(monkeypatch, txs, 1000, 1999, 100)
    assert out == {"first": 1, "last": 1}


def test_a_window_counts_once_and_is_the_height_divided_by_the_epoch(monkeypatch):
    # The day starts at 150, off the window grid: a window counted from the day's start would merge
    # b's two windows and split a's one.
    txs = [tx(200, proof("a")), tx(299, proof("a")),       # window 2, twice: once
           tx(299, proof("b")), tx(300, proof("b")),       # windows 2 and 3: twice
           tx(450, proof("c"), proof("c"))]                # two proofs in one transaction: once
    out, _ = read(monkeypatch, txs, 150, 1000, 100)
    assert out == {"a": 1, "b": 2, "c": 1}


def test_every_page_is_read(monkeypatch):
    txs = [tx(1000 + 10 * k, proof("m")) for k in range(250)]      # 250 distinct windows of 10 blocks
    out, fake = read(monkeypatch, txs, 1000, 3499, 10)
    assert out == {"m": 250}
    assert fake.pages() == ["1", "2", "3"]
    assert all(c[c.index("--limit") + 1] == "100" for c in fake.calls)


def test_a_window_proven_on_two_pages_counts_once(monkeypatch):
    txs = [tx(1000 + 10 * k, proof("f")) for k in range(99)]
    txs += [tx(2000, proof("x")), tx(2005, proof("x"))]   # 100th and 101st: two pages, one window
    out, fake = read(monkeypatch, txs, 1000, 2999, 10)
    assert out == {"f": 99, "x": 1}
    assert fake.pages() == ["1", "2"]


def test_a_search_that_does_not_end_raises(monkeypatch):
    txs = [tx(1000 + k, proof(f"m{k}")) for k in range(300)]
    with pytest.raises(C.ChainUnreadable):
        read(monkeypatch, txs, 1000, 1999, 100, max_pages=2)


@pytest.mark.parametrize("epoch", [0, -1])
def test_no_window_length_raises_before_any_query(monkeypatch, epoch):
    fake = Pages([tx(1005, proof("m"))])
    monkeypatch.setattr(C, "_cli", fake)
    with pytest.raises(C.ChainUnreadable):
        C.presence_windows(NODE, 1000, 1999, epoch)
    assert fake.calls == []


@pytest.mark.parametrize("answer", [{"total_count": "0", "txs": []}, {"total_count": "0", "txs": None}])
def test_a_search_that_found_nothing_is_zero(monkeypatch, answer):
    monkeypatch.setattr(C, "_cli", lambda args, node, timeout=60: answer)
    assert C.presence_windows(NODE, 1000, 1999, 100) == {}


@pytest.mark.parametrize("answer", [{}, {"txs": None}, {"txs": [], "total_count": "many"}])
def test_an_answer_without_its_count_is_unreadable_not_empty(monkeypatch, answer):
    # Without the search's own count, a first page cannot be known to be the last: read as a count of 0,
    # it stopped after page 1 with every sign of success.
    monkeypatch.setattr(C, "_cli", lambda args, node, timeout=60: answer)
    with pytest.raises(C.ChainUnreadable):
        C.presence_windows(NODE, 1000, 1999, 100)


def test_a_page_that_fails_raises_and_returns_no_part(monkeypatch):
    txs = [tx(1000 + k, proof(f"m{k}")) for k in range(150)]
    with pytest.raises(C.ChainUnreadable):
        read(monkeypatch, txs, 1000, 1999, 100, fail_on_page=2)


def _run(returncode=0, stdout="", stderr="", raises=None, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append(list(cmd))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


@pytest.mark.parametrize("fake", [
    _run(returncode=1, stderr="Error: post failed: connection refused"),
    _run(returncode=0, stdout="not json"),
    _run(raises=FileNotFoundError("dendrad")),
    _run(raises=subprocess.TimeoutExpired("dendrad", 60)),
])
def test_a_failing_cli_raises_through_the_shipped_invocation(monkeypatch, fake):
    monkeypatch.setattr(C.subprocess, "run", fake)
    with pytest.raises(C.ChainUnreadable):
        C.presence_windows(NODE, 1000, 1999, 100)


def test_the_proofs_name_the_operator_that_signed_the_latest_one(monkeypatch):
    # The default payout address of a day: the creator of the identity's latest accepted proof that day,
    # which the chain requires to be the miner's operator at that height.
    def signed(mid, who):
        return dict(proof(mid), creator=who)
    txs = [tx(1005, signed("m", "dendra1old")), tx(1500, signed("m", "dendra1new")),
           tx(1600, signed("m", "dendra1refused"), code=4), tx(2100, signed("m", "dendra1nextday"))]
    monkeypatch.setattr(C, "_cli", Pages(txs))
    assert C.presence_proofs(NODE, 1000, 1999, 100) == ({"m": 2}, {"m": "dendra1new"})


def test_only_settlement_messages_date_a_job(monkeypatch):
    # A settlement transaction can carry other messages naming a job: they settled nothing.
    settle = {"@type": C.SETTLE_ACTION, "creator": "dendra1anyone", "job_id": "a"}
    verdict = {"@type": "/dendra.jobs.v1.MsgCommitVerdict", "creator": "dendra1j", "job_id": "b"}
    txs = [tx(1005, verdict, settle), tx(1006, dict(settle, job_id="c"), code=5),
           tx(1007, {"@type": C.SETTLE_ACTION, "jobId": "d"}), tx(1500, dict(settle, job_id="b"))]
    fake = Pages(txs)
    monkeypatch.setattr(C, "_cli", fake)
    assert C.settle_heights(NODE) == {"a": 1005, "d": 1007, "b": 1500}
    q = fake.calls[0][fake.calls[0].index("--query") + 1]
    assert q == f"message.action='{C.SETTLE_ACTION}'"


def test_the_shipped_invocation_asks_the_node_for_json(monkeypatch):
    seen = []
    page = {"total_count": "1", "txs": [tx(1005, proof("m"))]}
    monkeypatch.setattr(C.subprocess, "run", _run(stdout=json.dumps(page), seen=seen))
    assert C.presence_windows(NODE, 1000, 1999, 100) == {"m": 1}
    cmd = seen[0]
    assert cmd[:3] == ["dendrad", "query", "txs"]
    assert cmd[cmd.index("--node") + 1] == NODE and cmd[cmd.index("--output") + 1] == "json"
    assert cmd[cmd.index("--page") + 1] == "1" and cmd[cmd.index("--limit") + 1] == "100"
