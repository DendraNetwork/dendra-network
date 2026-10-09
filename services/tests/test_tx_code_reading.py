"""Bench of how `client` judges a transaction: the `code` FIELD is parsed, never searched for.

proto3 omits a field at its zero value, and the zero of `code` IS success: the normal form of an
accepted transaction is a response WITHOUT a `code`. A predicate that searches the text for `code: 0`
reads that form as a failure, and one that searched for a non-zero code would read an unreadable output
as a success. So there are three readings, never two:

    code absent           -> 0     (a reading of zero: success)
    code present, a number -> that number
    no response / not a number -> None  (nothing was read: never a success, never a refusal)

The responses are the JSON the client requests (`--output json`), followed by what `run()` appends
from stderr, because that is the exact text `_tx_code` is handed in production.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402

H = "B" * 64
OTHER = "C" * 64


def _resp(**fields):
    d = {"height": "0", "txhash": H}
    d.update(fields)
    return json.dumps(d)


# ── the three readings ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, want", [
    (_resp(), 0),                                          # code ABSENT: proto3's form of success
    (_resp(code=0), 0),                                    # code written out as zero
    (_resp(code=5, raw_log="out of gas"), 5),              # a refusal
    (_resp(code="7"), 7),                                  # a number written as a string
    (_resp() + "\nWARNING: a note printed on stderr\n", 0),  # stderr after the response is not read
    ("  \n" + _resp(code=3), 3),                           # leading blank lines
])
def test_a_response_yields_its_code(text, want):
    assert dc._tx_code(text) == want


@pytest.mark.parametrize("text", [
    "",                                                    # nothing printed
    "Error: rpc error: connection refused\n",              # no response at all
    "code: 0\ntxhash: " + H + "\n",                        # the text format: not what was requested
    "{not json",                                           # broken JSON
    "[]",                                                  # JSON, but not an object
    json.dumps({"code": 0}),                               # an object that names no transaction
    json.dumps({"txhash": "abc", "code": 0}),              # a hash that is not a transaction hash
    _resp(code=-1),                                        # negative: not a result code
    _resp(code=True),                                      # a bool is not a number
    _resp(code=None),                                      # null is not a number
    _resp(code="zero"),                                    # neither is a word
    "Error: key not found\n" + _resp(),                    # a response AFTER an error is not the response
])
def test_no_response_yields_none_never_a_code(text):
    assert dc._tx_code(text) is None


def test_ok_is_true_only_for_a_code_read_as_zero():
    assert dc._ok(_resp()) is True
    assert dc._ok(_resp(code=0)) is True
    assert dc._ok(_resp(code=11)) is False
    assert dc._ok("Error: post failed\n") is False
    assert dc._ok("code: 0\n") is False


# ── the full wait, through the shipped function ────────────────────────────────────────────────
class _Chain:
    """Answers `dendrad query tx` with the scripted texts, in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cmd, t=120):
        self.calls.append(list(cmd))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture()
def chain(monkeypatch):
    monkeypatch.setattr(dc, "_node", lambda: [])
    monkeypatch.setattr(dc.time, "sleep", lambda s: None)

    def install(*answers):
        c = _Chain(*answers)
        monkeypatch.setattr(dc, "run", c)
        return c
    return install


def test_included_with_no_code_is_accepted(chain):
    c = chain("Error: tx (" + H + ") not found\n", _resp(height="41"))
    assert dc.wait_tx_reason(_resp(), timeout=5) == (True, "")
    assert dc.wait_tx(_resp(), timeout=5) is True
    q = c.calls[0]
    assert q[:4] == ["dendrad", "query", "tx", H] and q[q.index("--output") + 1] == "json", q


def test_included_and_refused_reports_the_raw_log(chain):
    chain(_resp(height="41", code=1105, raw_log="escrow exceeds the fee"))
    assert dc.wait_tx_reason(_resp(code=0), timeout=5) == (False, "escrow exceeds the fee")


def test_included_with_an_unreadable_code_is_a_failure(chain):
    chain(_resp(height="41", code="x"))
    ok, why = dc.wait_tx_reason(_resp(), timeout=5)
    assert ok is False and "unreadable" in why


def test_refused_at_broadcast_never_polls(chain):
    c = chain(_resp(height="41"))
    assert dc.wait_tx_reason(_resp(code=13, raw_log="insufficient fee"), timeout=5) == (False, "insufficient fee")
    assert c.calls == []


def test_an_unreadable_broadcast_is_a_failure_reported_as_read(chain):
    c = chain(_resp(height="41"))
    assert dc.wait_tx_reason("Error: account not found\n", timeout=5) == (False, "Error: account not found")
    assert dc.wait_tx("Error: account not found\n", timeout=5) is False
    assert c.calls == []


def test_a_response_for_another_transaction_is_not_this_one(chain):
    chain(json.dumps({"height": "41", "txhash": OTHER}))
    ok, why = dc.wait_tx_reason(_resp(), timeout=3)
    assert ok is False and "never included" in why


def test_never_included_is_a_failure(chain):
    c = chain("Error: tx not found\n")
    ok, why = dc.wait_tx_reason(_resp(), timeout=3)
    assert ok is False and "never included" in why and len(c.calls) == 3


# ── the request: the format that is parsed is the format that is ASKED for ───────────────────
def test_tx_from_requests_json(monkeypatch):
    seen = []
    monkeypatch.setattr(dc, "_node", lambda: [])
    monkeypatch.setattr(dc._cid, "chain_id", lambda node_flags=(): "dendra-bench")
    monkeypatch.setattr(dc, "run", lambda cmd, t=120: seen.append(list(cmd)) or _resp())
    dc.tx_from("gen", "open-job", "job1", "500")
    cmd = seen[0]
    end = cmd.index("--")
    assert "--output" in cmd[:end] and cmd[cmd.index("--output") + 1] == "json", cmd
    assert cmd[end + 1:] == ["job1", "500"], cmd
