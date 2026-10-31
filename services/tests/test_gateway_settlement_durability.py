"""A free-tier settlement that never reaches the disk must not be read back as the smaller estimate.

`run_dendra` reserves an estimate before the job and replaces it with the real use afterwards
(`gateway.py::quota_reserve`, `quota_settle`). Under the flat rate the real use counts the
returned answer and can exceed the estimate. If the settlement's save then fails, the file still holds
the estimate, and a restart used to resume from it: the day's counters came back short by the overrun.
These tests drive the served path (`run_dendra` with a stubbed network call) and restart the module.
"""
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway

MESSAGES = [{"role": "user", "content": "hi"}]
CLIENT_IP = "203.0.113.9"
LONG_ANSWER = "word " * 200          # 1000 characters, 250 tokens by the four-character rule


class _Disk:
    """os.replace that fails on demand, as a full or broken volume would."""

    def __init__(self):
        self.fail = False
        self._real = os.replace

    def replace(self, src, dst):
        if self.fail:
            raise OSError("write failed")
        return self._real(src, dst)


def _gateway(monkeypatch, tmp_path, metered):
    path = str(tmp_path / "quota.json")
    monkeypatch.setenv("DENDRA_QUOTA_FILE", path)
    gw = importlib.reload(gateway)
    monkeypatch.setattr(gw, "FREE_TIER", True)
    monkeypatch.setattr(gw, "DYN_PRICING", metered)
    monkeypatch.setattr(gw, "OUT_ALLOW", 10)
    gw._quota_load()
    disk = _Disk()
    monkeypatch.setattr(gw.os, "replace", disk.replace)
    return gw, path, disk


def _restart(disk):
    disk.fail = False
    gw = importlib.reload(gateway)
    gw._quota_load()
    gw._quota_roll()                  # the first request rolls first; the property must hold after it
    return gw


def _est(gw):
    return max(1, len(gw.messages_to_prompt(MESSAGES)) // 4) + gw.OUT_ALLOW


def test_a_FLAT_rate_overrun_whose_settlement_save_FAILS_is_not_undercounted_after_a_restart(
        monkeypatch, tmp_path):
    gw, path, disk = _gateway(monkeypatch, tmp_path, metered=False)

    def quick(*a, **k):
        disk.fail = True              # the volume breaks while the job runs: the settlement cannot land
        return {"answer": LONG_ANSWER}
    monkeypatch.setattr(gw.dc, "quick", quick)

    est = _est(gw)
    answer, _ = gw.run_dendra(MESSAGES, ip=CLIENT_IP)
    assert answer == LONG_ANSWER
    real = gw._QUOTA["glob"]
    assert real > est, (real, est)    # the overrun is real, and counted in memory
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["glob"] == est   # the disk kept only the reservation

    gw = _restart(disk)
    assert gw._QUOTA["glob"] >= real, gw._QUOTA
    assert gw._QUOTA["ip"].get(CLIENT_IP, 0) >= real, gw._QUOTA


def test_a_FLAT_rate_job_IN_FLIGHT_at_a_restart_is_not_read_back_as_its_estimate(monkeypatch, tmp_path):
    gw, _, disk = _gateway(monkeypatch, tmp_path, metered=False)
    ok, _ = gw.quota_reserve(CLIENT_IP, _est(gw), False)
    assert ok
    gw = _restart(disk)               # the process died before the job came back: its use is unknown
    assert gw._QUOTA["glob"] >= gw.FREE_TOK_DAY, gw._QUOTA
    assert gw._QUOTA["ip"][CLIENT_IP] >= gw.FREE_TOK_IP, gw._QUOTA


def test_a_FLAT_rate_settlement_that_LANDS_resumes_the_real_count_without_closing_the_tap(
        monkeypatch, tmp_path):
    gw, _, disk = _gateway(monkeypatch, tmp_path, metered=False)
    monkeypatch.setattr(gw.dc, "quick", lambda *a, **k: {"answer": LONG_ANSWER})
    gw.run_dendra(MESSAGES, ip=CLIENT_IP)
    real = gw._QUOTA["glob"]

    gw = _restart(disk)
    assert gw._QUOTA["glob"] == real, gw._QUOTA
    assert gw._QUOTA["ip"][CLIENT_IP] == real, gw._QUOTA
    assert gw._QUOTA["open"] == {}, gw._QUOTA


def test_a_FAILED_flat_rate_job_releases_its_reservation_and_leaves_nothing_open(monkeypatch, tmp_path):
    gw, _, disk = _gateway(monkeypatch, tmp_path, metered=False)
    monkeypatch.setattr(gw.dc, "quick", lambda *a, **k: {"error": "relay down"})
    gw.run_dendra(MESSAGES, ip=CLIENT_IP)

    gw = _restart(disk)
    assert gw._QUOTA["glob"] == 0, gw._QUOTA
    assert gw._QUOTA["open"] == {}, gw._QUOTA


def test_a_METERED_settlement_whose_save_FAILS_keeps_the_estimate_which_bounds_it(monkeypatch, tmp_path):
    """Metered pricing bills at most the escrow, which covers the estimate: the stale file overcounts,
    so a restart neither undercounts nor needs to close the tap."""
    gw, _, disk = _gateway(monkeypatch, tmp_path, metered=True)
    est = _est(gw)

    def quick_metered(*a, **k):
        disk.fail = True
        return {"answer": "short.", "in_tok": 4, "out_tok": 2}
    monkeypatch.setattr(gw.dc, "quick_metered", quick_metered)
    gw.run_dendra(MESSAGES, ip=CLIENT_IP)
    assert gw._QUOTA["glob"] == 6, gw._QUOTA

    gw = _restart(disk)
    assert gw._QUOTA["glob"] == est, gw._QUOTA
    assert gw._QUOTA["glob"] < gw.FREE_TOK_DAY, gw._QUOTA


def test_a_METERED_count_above_the_escrow_is_charged_at_the_escrow(monkeypatch, tmp_path):
    gw, _, _ = _gateway(monkeypatch, tmp_path, metered=True)
    est = _est(gw)
    monkeypatch.setattr(gw.dc, "quick_metered",
                        lambda *a, **k: {"answer": "short.", "in_tok": est, "out_tok": est})
    gw.run_dendra(MESSAGES, ip=CLIENT_IP)
    assert gw._QUOTA["glob"] == est, gw._QUOTA


def test_a_reservation_REFUSED_for_a_failed_save_leaves_nothing_open(monkeypatch, tmp_path):
    gw, _, disk = _gateway(monkeypatch, tmp_path, metered=False)
    disk.fail = True
    ok, _ = gw.quota_reserve(CLIENT_IP, 10, False)
    assert not ok
    assert gw._QUOTA["open"] == {}, gw._QUOTA
    disk.fail = False
    ok, _ = gw.quota_reserve("198.51.100.1", 10, True)   # a later save writes the in-memory state
    assert ok

    gw = _restart(disk)
    assert gw._QUOTA["glob"] < gw.FREE_TOK_DAY, gw._QUOTA


def test_a_MALFORMED_open_record_keeps_the_tap_closed(monkeypatch, tmp_path):
    gw, path, disk = _gateway(monkeypatch, tmp_path, metered=False)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": gw.datetime.date.today().isoformat(), "glob": 5, "ip": {}, "open": {CLIENT_IP: "lots"}}, f)
    gw = _restart(disk)
    assert gw._QUOTA["glob"] >= gw.FREE_TOK_DAY, gw._QUOTA


def test_a_file_written_BEFORE_the_open_record_existed_loads_as_settled(monkeypatch, tmp_path):
    gw, path, disk = _gateway(monkeypatch, tmp_path, metered=False)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": gw.datetime.date.today().isoformat(), "glob": 77, "ip": {CLIENT_IP: 77}}, f)
    gw = _restart(disk)
    assert gw._QUOTA["glob"] == 77, gw._QUOTA
    assert gw._QUOTA["open"] == {}, gw._QUOTA
