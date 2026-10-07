"""THE PRIMARY THAT REVEALS (ADR-026, `reveal_worker.py` plus `reveal_helpers.query_all`).

WHY THIS BENCH EXISTS. This module sits on the side of the ACCUSED: each of its failures is paid for
by a slash of the honest party that suffers it.
  - if it does not see its own disputed jobs, it does not reveal -> a "0" verdict;
  - if it anchors a root without a list, its marker is unverifiable FOREVER, and the jurors stay on
    `None` for that epoch;
  - if it reads a wrong height, it files its marker under the wrong epoch -> unfindable.
None of these three failures produces a visible error. They produce SILENCE, and silence is exactly
what the protocol punishes.

NOT COVERED, stated plainly: the `main()` loop, the real `dendrad` calls, decryption from the relay
(`_decrypt_from_relay`) and the network dialogue. What is exercised here are the DECISIONS and the
READS.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reveal_worker as rw
import reveal_helpers as rv
import reveal_marker as rmk


# === (1) `query_all` — SILENT TRUNCATION, whose failure only ever produces GOOD news ==============
# `list-job` without pagination stops at 100 (the SDK DefaultLimit). From the 101st job on, a primary
# stops seeing ITS OWN audits — and truncation can only produce "fewer disputes", a failure mode
# INDISTINGUISHABLE from a healthy network. That is the most expensive shape this defect can take.

def fake_runner(pages):
    """Returns a `runner(cmd, t=...)` that serves `pages` in order and records the commands."""
    state = {"i": 0, "cmds": []}

    def runner(cmd, t=90):
        state["cmds"].append(list(cmd))
        p = pages[min(state["i"], len(pages) - 1)]
        state["i"] += 1
        return json.dumps(p) if not isinstance(p, str) else p
    runner.state = state
    return runner


def test_pagination_concatenates_all_pages():
    pages = [
        {"job": [{"job_id": "j1"}, {"job_id": "j2"}], "pagination": {"next_key": "K1"}},
        {"job": [{"job_id": "j3"}], "pagination": {"next_key": None}},
    ]
    r = fake_runner(pages)
    rows = rv.query_all("list-job", "job", [], r)
    assert [x["job_id"] for x in rows] == ["j1", "j2", "j3"]
    # and the second command MUST carry the page key: without it the first page is read forever.
    assert "--page-key" in r.state["cmds"][1] and "K1" in r.state["cmds"][1]


def test_next_key_in_camelCase_is_followed_too():
    # dendrad may emit camelCase (the same trap as `_norm_keys`). Missing `nextKey` means stopping at
    # the first page while believing everything was read.
    pages = [
        {"job": [{"job_id": "j1"}], "pagination": {"nextKey": "K1"}},
        {"job": [{"job_id": "j2"}], "pagination": {}},
    ]
    rows = rv.query_all("list-job", "job", [], fake_runner(pages))
    assert [x["job_id"] for x in rows] == ["j1", "j2"]


def test_a_capitalised_field_is_read_too():
    pages = [{"Job": [{"job_id": "j1"}], "pagination": {}}]
    assert rv.query_all("list-job", "job", [], fake_runner(pages)) == [{"job_id": "j1"}]


# `None`, NEVER `[]`. In a `for job in list_jobs()` loop, "I could not read" would otherwise become
# "nothing to judge": the worker sleeps believing it has worked. Not knowing is not knowing that there
# is nothing.
@pytest.mark.parametrize("output", ["pas du json", "", "null", "[]", "{}", '"text"', "123"])
def test_unreadable_output_returns_None_not_an_empty_list(output):
    assert rv.query_all("list-job", "job", [], fake_runner([output])) is None


def test_refuses_to_return_a_PREFIX_when_there_are_too_many_pages():
    # Pagination that never ends: what has been read is not returned. A prefix returned as a total is
    # silent truncation reintroduced by the guard itself.
    page = {"job": [{"job_id": "x"}], "pagination": {"next_key": "TOUJOURS"}}
    assert rv.query_all("list-job", "job", [], fake_runner([page]), max_pages=5) is None


def test_page_limit_is_explicit_in_the_command():
    # THIS flag is what bypasses the SDK DefaultLimit=100. If it disappears, truncation returns
    # without any test changing colour — except this one.
    r = fake_runner([{"job": [], "pagination": {}}])
    rv.query_all("list-job", "job", [], r)
    assert "--page-limit" in r.state["cmds"][0]


def test_a_runner_without_the_t_parameter_is_supported():
    # `_accepts_t` inspects the signature instead of assuming it: the workers do not all share one.
    def runner_without_t(cmd):
        return json.dumps({"job": [{"job_id": "j1"}], "pagination": {}})
    assert rv.query_all("list-job", "job", [], runner_without_t) == [{"job_id": "j1"}]


# === (2) `_norm_keys` — the casing mistake that got an honest miner slashed ========================
# dendrad may emit `jobId`; reading `job_id` ALONE leaves the primary unable to find ITS OWN
# +disputed jobs -> the reveal is never posted -> a slash caused by a casing mistake.

def test_camelCase_becomes_readable_as_snake_case():
    d = rw._norm_keys({"jobId": "j1", "encPubkey": "AA", "minerId": "m1"})
    assert d["job_id"] == "j1" and d["enc_pubkey"] == "AA" and d["miner_id"] == "m1"


def test_the_original_keys_are_KEPT():
    # Aliases are added, nothing is renamed: a caller still reading camelCase must keep working.
    d = rw._norm_keys({"jobId": "j1"})
    assert d["jobId"] == "j1" and d["job_id"] == "j1"


def test_an_existing_snake_key_is_not_OVERWRITTEN():
    # When both forms coexist, the explicit snake_case wins: otherwise a derived alias would replace
    # a value that was set deliberately.
    # KEY ORDER MATTERS IN THIS TEST. With `jobId` first, the derived alias is written and then
    # OVERWRITTEN by the explicit key, so the "overwrite instead of complete" sabotage would pass
    # green. The explicit key must come FIRST so the alias has a chance to overwrite it. An assertion
    # never seen to fail against the faulty version proves nothing.
    d = rw._norm_keys({"job_id": "SNAKE", "jobId": "CAMEL"})
    assert d["job_id"] == "SNAKE"


@pytest.mark.parametrize("entry", [None, [], "text", 42])
def test_a_non_dict_is_returned_as_is(entry):
    assert rw._norm_keys(entry) == entry


def test_already_snake_case_is_unchanged():
    d = {"job_id": "j1", "state": "open"}
    assert rw._norm_keys(d) == d


# === (3) `_tx_ok` — the `code:0` trap ==============================================================
@pytest.mark.parametrize("text,expected", [
    ("code: 0\ntxhash: AB", True),
    ("code: 5\nraw_log: error", False),
    ("code: 18", False),
    ("", False),
    (None, False),
    ("no code here", False),
    ("txhash: AB\ncode: 0", True),
])
def test_tx_ok(text, expected):
    assert rw._tx_ok(text) is expected


def test_code_0_in_ANOTHER_field_does_not_count():
    # `gas_code: 0` and `codespace: 0...` are not the execution code. The `(^|\n)code:` anchor keeps a
    # substring from being taken for a verdict.
    assert rw._tx_ok("gas_code: 0\ncode: 7") is False


# === (4) `is_disputed` — the predicate that decides whether to reveal ==============================
@pytest.mark.parametrize("state,expected", [
    ("open+paid+optimistic+disputed", True),
    ("open+paid+optimistic+disputed+resolved", False),
    ("open+paid+optimistic", False),
    ("", False),
])
def test_is_disputed(state, expected):
    assert rw.is_disputed(state) is expected


# === (5) `anchor_marker` — THE ORDER IS LOAD-BEARING: the LIST first, the ROOT second ==============
# The other way round, a failure leaves an ANCHORED and IMMUTABLE root with no list: the marker
# becomes permanently unverifiable and the jurors stay on `None` for that epoch, forever.

class Journal:
    def __init__(self):
        self.order = []
        self.puts = {}
        self.txs = []


@pytest.fixture
def jrn(monkeypatch):
    j = Journal()

    def fake_put(url, kind, key, payload):
        j.order.append("list")
        j.puts[key] = payload
        return getattr(fake_put, "ok", True)

    def fake_tx(frm, *a):
        j.order.append("root")
        j.txs.append((frm, a))
        return getattr(fake_tx, "output", "code: 0\ntxhash: AB")

    monkeypatch.setattr(rw.relay, "put", fake_put)
    monkeypatch.setattr(rw, "tx_from", fake_tx)
    j.put, j.tx = fake_put, fake_tx
    return j


def test_the_list_is_published_BEFORE_the_root(jrn):
    ok, _ = rw.anchor_marker("m1", "http://relay", 3, ["jB", "jA"])
    assert ok is True
    assert jrn.order == ["list", "root"], (
        "anchoring the root first would leave, on failure, a marker unverifiable FOREVER")


def test_a_relay_refusing_the_list_PREVENTS_anchoring(jrn):
    jrn.put.ok = False
    ok, reason = rw.anchor_marker("m1", "http://relay", 3, ["jA"])
    assert ok is False
    assert "unverifiable root" in reason
    assert jrn.order == ["list"], "no root may be sent if the list was not deposited"


def test_an_epoch_without_a_reveal_anchors_NOTHING(jrn):
    ok, reason = rw.anchor_marker("m1", "http://relay", 3, [])
    assert ok is False
    assert "no empty marker" in reason
    assert jrn.order == [], "an empty marker would suggest activity that did not happen"


def test_the_list_and_the_root_are_CONSISTENT(jrn):
    # This is exactly what the juror re-checks (`marker_state`): if they diverge it returns `None` and
    # the primary's work does not count. Consistency is locked here, at the source.
    jobs = ["jC", "jA", "jB", "jA"]
    rw.anchor_marker("m1", "http://relay", 7, jobs)
    payload = jrn.puts[rmk.list_key(7, "m1")]
    assert payload["jobs"] == ["jA", "jB", "jC"], "sorted and deduplicated, otherwise the root varies"
    assert payload["root"] == rmk.merkle_root(jobs)
    assert rmk.merkle_root(payload["jobs"]) == payload["root"]
    # ...and it is THAT root that goes on-chain, under the right key.
    frm, args = jrn.txs[0]
    assert frm == "m1" and args[0] == "create-commit"
    assert args[1] == rmk.marker_key(7, "m1") and args[2] == payload["root"]
    assert args[4] == rmk.MARKER_KIND


def test_an_already_anchored_commit_is_NOT_an_error(jrn):
    # An anchored commit is IMMUTABLE. The chain refusing a re-anchor is the expected behaviour, not
    # a failure — otherwise the worker would replay forever.
    jrn.tx.output = "failed to execute message: commit already exists"
    ok, reason = rw.anchor_marker("m1", "http://relay", 3, ["jA"])
    assert ok is True and "already anchored" in reason


def test_a_real_chain_error_stays_a_failure(jrn):
    jrn.tx.output = "code: 18\nraw_log: insufficient funds"
    ok, reason = rw.anchor_marker("m1", "http://relay", 3, ["jA"])
    assert ok is False and "insufficient funds" in reason


def test_same_set_same_root_whatever_the_order(jrn):
    rw.anchor_marker("m1", "http://r", 3, ["a", "b", "c"])
    r1 = jrn.txs[0][1][2]
    jrn.txs.clear()
    rw.anchor_marker("m1", "http://r", 3, ["c", "b", "a", "c"])
    assert jrn.txs[0][1][2] == r1, "two runs over the same work must anchor the SAME root"
