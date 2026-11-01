"""Bench of the gateway's `dendra` block: `settled` says whether the chain accepted the settlement, read
from the settlement's own answer, in three states. `audit_state` stays the documented constant."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture()
def gw(monkeypatch):
    monkeypatch.setenv("DENDRA_EXPOSE_META", "1")
    import importlib
    import gateway
    importlib.reload(gateway)
    return gateway


def job(**kw):
    r = {"jid": "job1", "committee": ["dm1m"], "beacon": "ab", "fee_actual": 120, "fee_escrow": 900}
    r.update(kw)
    return r


@pytest.mark.parametrize("settle,want", [
    ({"settled": True, "txhash": "AA"}, True),
    ({"settled": False, "error": "primary commit missing"}, False),
    ({}, None),                     # an answer that does not say is not known, never a false or a true
    (None, None),
    ({"settled": "yes"}, None),     # not a boolean: not known
])
def test_settled_is_read_from_the_settlement_in_three_states(gw, settle, want):
    r = job() if settle is None else job(settle=settle)
    m = gw._dendra_meta(r)
    assert m["settled"] is want
    assert m["audit_state"] == "pending"       # the documented constant, unchanged
    assert m["job_id"] == "job1" and m["miner_id"] == "dm1m"


def test_no_job_id_no_block(gw):
    assert gw._dendra_meta({"settle": {"settled": True}}) is None
