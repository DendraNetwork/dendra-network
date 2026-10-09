"""Bench of `client.submit_job` when `open-job` is refused: the error carries what was read from the
refusal (its `raw_log`, else its code, else the output), never a cause guessed in its place.

The responses are the JSON the client asks `dendrad` for (`--output json`), in the form the chain gives
them: a successful broadcast carries NO `code` field, since proto3 omits a field at its zero value."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402

TXHASH = "A" * 64


def _refused(monkeypatch, output):
    calls = []
    monkeypatch.setattr(dc, "tx_from", lambda *a, **k: calls.append(a) or output)
    monkeypatch.setattr(dc, "assigned_committee", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    r = dc.submit_job("Explain tides.", 500, "http://relay", client="gen", k=1, jid="job1")
    assert [c[1] for c in calls] == ["open-job"]
    return r


def test_a_refusal_at_broadcast_reports_its_raw_log(monkeypatch):
    r = _refused(monkeypatch, json.dumps({"height": "0", "txhash": TXHASH, "codespace": "sdk", "code": 4,
                                          "raw_log": "signature verification failed"}) + "\n")
    assert r == {"error": "open-job (escrow) refused: signature verification failed"}
    assert "balance" not in r["error"]


def test_a_refusal_in_the_block_reports_the_raw_log_of_the_included_tx(monkeypatch):
    seen = []
    monkeypatch.setattr(dc, "_node", lambda: [])
    monkeypatch.setattr(dc, "run", lambda cmd, t=120: seen.append(cmd) or json.dumps(
        {"height": "12", "txhash": TXHASH, "code": 1105, "raw_log": "escrow exceeds the fee"}))
    # The broadcast succeeded: its response carries no `code` at all.
    r = _refused(monkeypatch, json.dumps({"height": "0", "txhash": TXHASH, "raw_log": ""}) + "\n")
    assert r == {"error": "open-job (escrow) refused: escrow exceeds the fee"}
    assert seen and seen[0][:4] == ["dendrad", "query", "tx", TXHASH] and "json" in seen[0], seen


def test_an_output_with_no_code_is_reported_as_read(monkeypatch):
    r = _refused(monkeypatch, "Error: post failed: dial tcp 127.0.0.1:26657: connection refused\n")
    assert r["error"] == "open-job (escrow) refused: Error: post failed: dial tcp 127.0.0.1:26657: connection refused"


def test_a_refusal_without_raw_log_reports_its_code(monkeypatch):
    r = _refused(monkeypatch, json.dumps({"txhash": TXHASH, "codespace": "jobs", "code": 1105}))
    assert r == {"error": "open-job (escrow) refused: code 1105 (codespace jobs)"}
