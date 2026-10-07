"""Bench of `client.quick_metered`'s answer hook: called with the decrypted answer BEFORE the
settlement, and unable to stop the settlement whatever it raises. The Final Testnet Season generator
records each answer there, so that a programme request counts as work only when its answer reached the
programme (`final_season_facts.answered_jobs`)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402


def _stub(monkeypatch, events, answer="A compass needle points north."):
    monkeypatch.setattr(dc, "_submit_committee_k", lambda: 1)
    monkeypatch.setattr(dc, "submit_job", lambda *a, **k: {"jid": "job1", "committee": ["dm1m"], "keys": {},
                                                          "beacon": ""})
    monkeypatch.setattr(dc, "job_results", lambda *a, **k: {"dm1m": "sealed"})
    monkeypatch.setattr(dc, "canonical_answer", lambda results: answer)
    monkeypatch.setattr(dc, "job_tokens", lambda *a, **k: (0, 0))
    monkeypatch.setattr(dc, "settle_when_ready", lambda *a, **k: events.append("settle") or "settled")


def test_the_hook_runs_before_the_settlement_with_the_answer(monkeypatch):
    events, seen = [], []
    _stub(monkeypatch, events)
    r = dc.quick_metered("Explain tides.", 500, 10, 768, "http://relay", client="gen",
                         on_answer=lambda jid, comm, ans: events.append("hook") or seen.append((jid, comm, ans)))
    assert events == ["hook", "settle"]
    assert seen == [("job1", ["dm1m"], "A compass needle points north.")] and r["reported"] is True


def test_a_hook_that_raises_is_reported_and_the_job_is_still_settled(monkeypatch):
    events = []
    _stub(monkeypatch, events)

    def boom(jid, comm, ans):
        raise ConnectionError("service down")

    r = dc.quick_metered("Explain tides.", 500, 10, 768, "http://relay", client="gen", on_answer=boom)
    assert events == ["settle"] and "ConnectionError" in r["reported"]


def test_without_a_hook_nothing_changes(monkeypatch):
    events = []
    _stub(monkeypatch, events)
    r = dc.quick_metered("Explain tides.", 500, 10, 768, "http://relay", client="gen")
    assert events == ["settle"] and r["reported"] is None
