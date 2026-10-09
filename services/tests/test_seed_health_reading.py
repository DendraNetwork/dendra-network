"""Bench of client.committee_seed_health: a failed query is None, never a reading of zeros, and the
seed's second bar (the contributors' share of the commit power) is carried."""
import json
import os
import subprocess
import sys

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, MODEA or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402


def _run(rc, out):
    return lambda cmd, **k: subprocess.CompletedProcess(cmd, rc, out, "")


def test_a_failed_query_is_unknown_not_zero(monkeypatch):
    # out_json used to turn any failure into {}, and every field then read 0: an unreachable node looked
    # like a chain with no seed source, no floor and no contributor.
    monkeypatch.setattr(dc.subprocess, "run", _run(1, ""))
    assert dc.committee_seed_health() is None
    monkeypatch.setattr(dc.subprocess, "run", _run(0, "Error: connection refused"))
    assert dc.committee_seed_health() is None

    def boom(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(dc.subprocess, "run", boom)
    assert dc.committee_seed_health() is None


def test_inside_a_read_answer_an_absent_field_is_zero(monkeypatch):
    # proto3 omits zeros: a chain with every field at zero answers {} and that IS a reading.
    monkeypatch.setattr(dc.subprocess, "run", _run(0, "{}"))
    d = dc.committee_seed_health()
    assert d is not None and d["contributors"] == 0 and d["power_bps"] == 0 and d["seed_height"] is None


def test_the_power_share_is_carried(monkeypatch):
    out = json.dumps({"committee_seed_source": "1", "committee_min_vrf_contributors": "1",
                      "latest_contributors": "2", "contributor_power_bps": "5000", "has_recent_seed": True,
                      "latest_seed_height": "120", "current_height": "121"})
    monkeypatch.setattr(dc.subprocess, "run", _run(0, out))
    d = dc.committee_seed_health()
    assert d["power_bps"] == 5000 and d["contributors"] == 2 and d["seed_height"] == 120
