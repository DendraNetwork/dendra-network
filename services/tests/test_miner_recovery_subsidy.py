"""The miner daemon keeps the recovery phrase of a key it creates, and claims its subsidy by itself.

Pinned: the phrase is written only for a key CREATED here (never invented for an existing one), with
mode 0600; the claimable amount follows the chain's cap with the rule of zero (absent = 0 inside an
answer that was read, unreadable = no claim); nothing is sent below the threshold."""
import json
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miner as D  # noqa: E402

MN = " ".join(["word"] * 23 + ["last"])
CREATED = ("some warning on stderr\n"
           + json.dumps({"name": "m1", "type": "local", "address": "dendra1abc", "mnemonic": MN}))


def test_phrase_of_a_created_key_is_kept_private(tmp_path):
    assert D.keep_recovery_phrase(str(tmp_path), CREATED) is True
    p = tmp_path / D.RECOVERY_FILE
    d = json.loads(p.read_text())
    assert d["mnemonic"] == MN and d["address"] == "dendra1abc"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_no_phrase_no_file(tmp_path):
    assert D.keep_recovery_phrase(str(tmp_path), "Error: key already exists") is False
    assert D.keep_recovery_phrase(str(tmp_path), json.dumps({"mnemonic": "too short"})) is False
    assert D.keep_recovery_phrase("", CREATED) is False
    assert not (tmp_path / D.RECOVERY_FILE).exists()


def test_existing_key_writes_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(D, "run", lambda c, t=600: calls.append(c) or "dendra1existing")
    assert D.keys_addr("m1", str(tmp_path)) == "dendra1existing"
    assert not any("add" in c for c in calls)
    assert not (tmp_path / D.RECOVERY_FILE).exists()


def test_created_key_keeps_its_phrase(tmp_path, monkeypatch):
    state = {"made": False}

    def fake_run(c, t=600):
        if "add" in c:
            state["made"] = True
            assert "--output" in c and "json" in c
            return CREATED
        return "dendra1abc" if state["made"] else "Error: key not found"
    monkeypatch.setattr(D, "run", fake_run)
    assert D.keys_addr("m1", str(tmp_path)) == "dendra1abc"
    assert (tmp_path / D.RECOVERY_FILE).exists()


def _q(miner, params):
    def query(sub, *pos, flags=()):
        if sub == "get-miner":
            return miner if isinstance(miner, str) else json.dumps(miner)
        return params if isinstance(params, str) else json.dumps(params)
    return query


def test_claimable_follows_the_chain_cap(monkeypatch):
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "1000000", "subsidy_claimed": "100000"}},
                                       {"params": {"work_gate_bps": "5000"}}))
    assert D.claimable_subsidy("m1") == 400000


def test_absent_fields_are_zero_inside_a_read_answer(monkeypatch):
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "1000000"}}, {"params": {}}))
    assert D.claimable_subsidy("m1") == 0


def test_unreadable_is_none_and_claims_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q("Error: connection refused", {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "")
    assert D.claimable_subsidy("m1") is None
    assert "not read" in D.maybe_claim_subsidy("m1") and not sent


def test_below_threshold_nothing_is_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "10000"}}, {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "")
    assert "below" in D.maybe_claim_subsidy("m1") and not sent


def test_above_threshold_one_claim(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "2000000"}}, {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "{}")
    monkeypatch.setattr(D, "wait_tx", lambda o, timeout=24: True)
    out = D.maybe_claim_subsidy("m1")
    assert sent == [("m1", "claim-subsidy", "m1")] and "confirmed" in out
