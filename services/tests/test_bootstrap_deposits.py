# -*- coding: utf-8 -*-
"""The bootstrap deposits (`pub`, `attest`) are retried until the relay accepts them (ADR-058, decision 7).

What is pinned, against the shipped `BootstrapDeposits`:
  * a kind is done on `ok` and on `exists` (the relay already holds it), never on `refused`;
  * `skipped` (the key differs from the anchored one) and `unavailable` (a build that cannot attest) end a
    kind without an acceptance, as the daemon did before this record;
  * a refused round is due again after a delay that doubles from BOOTSTRAP_RETRY_S and stops growing at
    BOOTSTRAP_RETRY_MAX_S, and only the refused kind is sent again;
  * the heartbeat form names each outcome, says whether the set is complete and when the next try is due.
The loop that calls it is pinned in test_registration_retry.py (the daemon's main(), on an injected clock).
The subject can be pointed at a mutated copy (DENDRA_DAEMON_FILE), so a harness shows each case turn red.
"""
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402


def test_nothing_is_done_before_a_round_and_both_kinds_are_pending():
    b = md.BootstrapDeposits()
    assert b.pending() == ["pub", "attest"] and not b.complete()
    assert b.due(0.0), "the first round is due at once"
    assert b.to_status() == {"pub": None, "attest": None, "complete": False, "rounds": 0, "next_at": None}


def test_a_refusal_never_marks_a_kind_done():
    b = md.BootstrapDeposits(retry_s=60, retry_max_s=3600)
    b.record("pub", "refused")
    b.record("attest", "ok")
    b.close_round(1000.0)
    assert b.pending() == ["pub"], "the refused kind alone is sent again"
    assert not b.complete() and b.next_at == 1060.0
    assert not b.due(1059.0) and b.due(1060.0)
    st = b.to_status()
    assert st["pub"] == "refused" and st["attest"] == "ok" and st["complete"] is False and st["next_at"] == 1060


def test_ok_exists_skipped_and_unavailable_end_a_kind():
    for outcome in ("ok", "exists", "skipped", "unavailable"):
        b = md.BootstrapDeposits()
        b.record("pub", outcome)
        b.record("attest", outcome)
        b.close_round(5.0)
        assert b.complete() and not b.due(10_000.0) and b.to_status()["next_at"] is None, outcome


def test_the_delay_doubles_and_is_bounded():
    b = md.BootstrapDeposits(retry_s=60, retry_max_s=300)
    delays = []
    t = 0.0
    for _ in range(6):
        b.record("pub", "refused")
        b.record("attest", "refused")
        b.close_round(t)
        delays.append(b.next_at - t)
        t = b.next_at
    assert delays == [60, 120, 240, 300, 300, 300]
    assert b.rounds == 6


def test_an_acceptance_after_refusals_completes_the_set():
    b = md.BootstrapDeposits(retry_s=30)
    b.record("pub", "refused")
    b.record("attest", "refused")
    b.close_round(0.0)
    b.record("pub", "ok")
    b.record("attest", "refused")
    b.close_round(30.0)
    assert b.pending() == ["attest"]
    b.record("attest", "exists")
    b.close_round(90.0)
    assert b.complete() and b.to_status() == {"pub": "ok", "attest": "exists", "complete": True,
                                               "rounds": 3, "next_at": None}


def test_an_unknown_kind_is_refused_by_name():
    b = md.BootstrapDeposits()
    try:
        b.record("res", "ok")
    except ValueError as e:
        assert "res" in str(e)
    else:
        raise AssertionError("a kind outside the bootstrap set was recorded")


def test_the_defaults_are_the_module_constants():
    assert md.BOOTSTRAP_KINDS == ("pub", "attest")
    b = md.BootstrapDeposits()
    assert (b.retry_s, b.retry_max_s) == (md.BOOTSTRAP_RETRY_S, md.BOOTSTRAP_RETRY_MAX_S)
    assert 0 < md.BOOTSTRAP_RETRY_S <= md.BOOTSTRAP_RETRY_MAX_S
