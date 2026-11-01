"""DENDRA_FINAL_SEASON_WATCH_S: the seconds between two chain-status reads of `final_season_server.watch_chain`.

What these cases hold, against the shipped `watch_interval_s`, `watch_chain` and `main`:
  * absent: 5 seconds (WATCH_DEFAULT_S);
  * a number of seconds from WATCH_MIN_S (0.5) to WATCH_MAX_S (30): that number;
  * anything else (text, empty, zero, negative, nan, inf, below WATCH_MIN_S, 1e-9, above WATCH_MAX_S) raises in
    `watch_interval_s` and makes `main` refuse to start (exit 2, an error line naming the variable) before any worker thread starts, never a read as the default;
  * `watch_chain` sleeps exactly the interval `main` set, after each read (a good one and a failed one alike);
  * `State.day_now` follows the stored height alone, so the interval changes when a boundary is seen, not how.
"""
import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

VAR = "DENDRA_FINAL_SEASON_WATCH_S"


@pytest.fixture()
def S(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path))
    monkeypatch.setenv("DENDRA_FINAL_SEASON_START_HEIGHT", "1")
    monkeypatch.delenv(VAR, raising=False)
    import final_season_server as mod
    importlib.reload(mod)
    mod.ST = mod.State(str(tmp_path), 1)
    return mod


def test_absent_is_five_seconds(S):
    assert S.WATCH_DEFAULT_S == 5.0
    assert S.watch_interval_s() == 5.0
    assert S.WATCH_S == 5.0


@pytest.mark.parametrize("raw,expected", [("2", 2.0), ("0.5", 0.5), ("30", 30.0)])
def test_a_set_value_is_used(S, monkeypatch, raw, expected):
    monkeypatch.setenv(VAR, raw)
    assert S.watch_interval_s() == expected


@pytest.mark.parametrize("raw", ["five", "", "0", "-1", "nan", "inf", "5s", "1e-9", "0.0001", "0.49", "30.5", "31", "1000"])
def test_a_value_outside_the_bounds_is_refused(S, monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    with pytest.raises(ValueError, match=VAR):
        S.watch_interval_s()


def _no_workers(S, monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)

    class Server:
        def __init__(self, addr, handler):
            self.daemon_threads = False

        def serve_forever(self):
            return None

    monkeypatch.setattr(S, "threading", types.SimpleNamespace(Thread=Thread, Lock=__import__("threading").Lock))
    monkeypatch.setattr(S, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(S, "GENERATOR", "")
    return started


@pytest.mark.parametrize("raw", ["five", "", "0", "-1", "nan", "inf", "1e-9", "0.0001", "30.5", "300"])
def test_main_refuses_to_start_on_a_bad_value(S, monkeypatch, capsys, raw):
    started = _no_workers(S, monkeypatch)
    monkeypatch.setenv(VAR, raw)
    assert S.main() == 2
    assert started == []
    assert VAR in capsys.readouterr().out
    assert S.WATCH_S == 5.0                  # the refused value was never taken up


def test_main_takes_up_a_set_value_and_the_default(S, monkeypatch):
    started = _no_workers(S, monkeypatch)
    monkeypatch.setenv(VAR, "2.5")
    assert S.main() == 0
    assert S.WATCH_S == 2.5 and S.watch_chain in started
    monkeypatch.delenv(VAR)
    assert S.main() == 0
    assert S.WATCH_S == 5.0


class _Stop(Exception):
    pass


@pytest.mark.parametrize("interval", [5.0, 2.5])
def test_watch_chain_sleeps_the_interval_after_a_read_and_after_a_failed_read(S, monkeypatch, interval):
    S.WATCH_S = interval
    reads = [(1000, S.END - 10 * 86400), S.C.ChainUnreadable("node down"), (1001, S.END - 10 * 86400)]
    slept = []

    def latest(rpc):
        r = reads.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def sleep(s):
        slept.append(s)
        if not reads:
            raise _Stop()

    monkeypatch.setattr(S.C, "latest", latest)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {"avail_epoch_blocks": "288"})
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    with pytest.raises(_Stop):
        S.watch_chain()
    assert slept == [interval] * 3
    assert S.ST.height == 1001


def test_the_day_of_an_answer_follows_the_height_last_read_and_nothing_else(S):
    # `State.day_now` is a pure function of the stored height: a reading some seconds old puts a boundary answer
    # in the previous day, which `seal_days` has not sealed yet (it seals a day only once the height has passed it).
    first = S.day_bounds(3, 1)[0]
    S.ST.height = first - 1
    assert S.ST.day_now() == 2
    S.ST.height = first
    assert S.ST.day_now() == 3
