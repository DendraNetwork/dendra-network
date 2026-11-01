"""The job registry keeps jobs resolved by id across list refreshes, and the relay reads newest first.

Before: the relay read `/dendra/jobs/v1/job?pagination.limit=1000` with no `reverse`, so the cached page
held the first 1000 job ids in key order, the oldest ones for the `job<milliseconds>` ids clients mint,
and a recent job always missed. Each miss was read by id and written into the list snapshot, which the
next `refresh()` replaced: the same job was read by id again after every refresh.

Held here:
  * a job resolved by id is answered without a read while its resolution is younger than `expiry`, and
    a list refresh does not drop it;
  * past `expiry` it is read again; a failed re-read keeps the binding, a read that finds no job drops it;
  * the map is bounded, least recently used out first;
  * the list snapshot answers before the by-id map;
  * the shipped relay asks the gateway for the list with `pagination.reverse=true` and resolves a miss
    by id (a local fake gateway records the paths; no live host is touched).
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MODEA = Path(os.environ.get("DENDRA_MODEA_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(MODEA))

from modea import job_registry as jr  # noqa: E402

LIST = json.dumps({"job": [{"job_id": "job1", "client": "dendra1listed"}]})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock, answers, by_id_max=jr.BY_ID_MAX):
    """A registry whose by-id reader answers from `answers[jid]` (a body, or an exception to raise)."""
    calls = []

    def by_id(jid):
        calls.append(jid)
        a = answers.get(jid, "{}")
        if isinstance(a, Exception):
            raise a
        return a

    reg = jr.JobRegistry(lambda: LIST, clock, expiry=300, job_reader=by_id, by_id_max=by_id_max)
    assert reg.refresh()
    return reg, calls


def job(jid, client):
    return json.dumps({"job": {"job_id": jid, "client": client}})


def test_resolved_job_survives_refresh_without_a_second_read():
    clock = Clock()
    reg, calls = make(clock, {"late": job("late", "dendra1late")})
    assert reg.client("late") == ("dendra1late", "FEES")
    assert reg.refresh()
    clock.t += 100
    assert reg.client("late") == ("dendra1late", "FEES")
    assert calls == ["late"]
    assert "held=1" in reg.summary()


def test_expired_resolution_is_read_again():
    clock = Clock()
    answers = {"late": job("late", "dendra1late")}
    reg, calls = make(clock, answers)
    reg.client("late")
    clock.t += 301
    assert reg.client("late")[0] == "dendra1late"
    assert calls == ["late", "late"]


def test_failed_re_read_keeps_the_binding_and_counts():
    clock = Clock()
    answers = {"late": job("late", "dendra1late")}
    reg, calls = make(clock, answers)
    reg.client("late")
    clock.t += 301
    answers["late"] = OSError("link down")
    assert reg.client("late")[0] == "dendra1late"
    assert reg.lookups_failed == 1 and calls == ["late", "late"]


def test_failed_first_read_is_unknown_not_a_guess():
    clock = Clock()
    reg, _calls = make(clock, {"late": OSError("link down")})
    assert reg.client("late") == (None, "FEES")
    assert reg.lookups_failed == 1


def test_re_read_that_finds_no_job_drops_the_binding():
    clock = Clock()
    answers = {"late": job("late", "dendra1late")}
    reg, _calls = make(clock, answers)
    reg.client("late")
    clock.t += 301
    answers["late"] = "{}"
    assert reg.client("late")[0] is None
    assert "held=0" in reg.summary()


def test_answer_naming_another_job_is_an_absence():
    clock = Clock()
    reg, _calls = make(clock, {"late": job("other", "dendra1other")})
    assert reg.client("late")[0] is None
    assert "held=0" in reg.summary()


def test_by_id_map_is_bounded_least_recently_used_out():
    clock = Clock()
    answers = {f"j{i}": job(f"j{i}", f"dendra1c{i}") for i in range(5)}
    reg, calls = make(clock, answers, by_id_max=3)
    for i in range(3):
        reg.client(f"j{i}")
    reg.client("j0")                       # j0 used again: j1 is now the least recent
    reg.client("j3")
    reg.client("j4")
    assert "held=3" in reg.summary()
    n = len(calls)
    assert reg.client("j0")[0] == "dendra1c0" and len(calls) == n       # kept
    assert reg.client("j1")[0] == "dendra1c1" and len(calls) == n + 1   # evicted, read again


def test_list_snapshot_answers_first():
    clock = Clock()
    reg, calls = make(clock, {"job1": job("job1", "dendra1byid")})
    assert reg.client("job1")[0] == "dendra1listed"
    assert calls == []


@pytest.mark.parametrize("bad", [0, -1, 1.5, None, True, False])
def test_by_id_max_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError):
        jr.JobRegistry(lambda: LIST, Clock(), by_id_max=bad)


def test_an_eviction_between_lookup_and_reorder_does_not_raise():
    """Request threads share the by-id map. The clock call inside `client()` sits between reading a held
    job and moving it to the end; here it starts a second thread that resolves another job and so evicts
    the first. Without a lock the eviction lands in that gap and `move_to_end` raises KeyError out of
    `client()`; with it, the second thread waits until the first has finished."""
    clock = Clock()
    answers = {"a": job("a", "dendra1a"), "b": job("b", "dendra1b")}
    reg, _calls = make(clock, answers, by_id_max=1)
    assert reg.client("a")[0] == "dendra1a"
    other = {}

    def resolve_b():
        try:
            other["b"] = reg.client("b")[0]
        except Exception as e:  # noqa: BLE001
            other["b"] = e

    worker = threading.Thread(target=resolve_b)

    class Racing:
        armed = True

        def __call__(self):
            if Racing.armed:
                Racing.armed = False
                worker.start()
                worker.join(1.0)        # times out when the map is locked, finishes the eviction when not
            return clock.t

    reg._clock = Racing()
    got = reg.client("a")
    worker.join(5.0)
    assert not worker.is_alive()
    assert got == ("dendra1a", "FEES"), got
    assert other["b"] == "dendra1b", other
    assert reg.held() == 1


def test_an_eviction_from_resolve_by_id_waits_for_a_lookup_inside_the_locked_section():
    """The case above evicts from `client()`; this one evicts from `_resolve_by_id`. Thread A is held inside
    the locked section of `client()` (blocked on an event in the clock hook, between the read of the held job
    and its `move_to_end`) while thread B resolves another job, which evicts A's job at once if the map is not
    locked there. B must wait until A has left; if its locks are missing it finishes at once and A's
    `move_to_end` raises KeyError."""
    clock = Clock()
    answers = {"a": job("a", "dendra1a"), "b": job("b", "dendra1b")}
    reg, _calls = make(clock, answers, by_id_max=1)
    assert reg.client("a")[0] == "dendra1a"
    inside, release, b_done = threading.Event(), threading.Event(), threading.Event()
    log, out = [], {}
    a_thread_ref = []

    class Blocking:
        armed = True

        def __call__(self):
            if Blocking.armed and threading.current_thread() is a_thread_ref[0]:
                Blocking.armed = False
                inside.set()
                assert release.wait(10.0), "thread A was never released"
            return clock.t

    def lookup_a():
        try:
            out["a"] = reg.client("a")
        except Exception as e:  # noqa: BLE001
            out["a"] = e

    def resolve_b():
        try:
            out["b"] = reg._resolve_by_id("b")
        except Exception as e:  # noqa: BLE001
            out["b"] = e
        log.append("b done")
        b_done.set()

    reg._clock = Blocking()
    ta, tb = threading.Thread(target=lookup_a), threading.Thread(target=resolve_b)
    a_thread_ref.append(ta)
    ta.start()
    assert inside.wait(5.0), "thread A never reached the locked section"
    tb.start()
    waited_out = not b_done.wait(0.5)         # B is blocked on the map's lock while A sits inside it
    log.append("a released")
    release.set()
    ta.join(5.0)
    tb.join(5.0)
    assert not ta.is_alive() and not tb.is_alive()
    assert waited_out, "the eviction from _resolve_by_id did not wait for the lookup inside the locked section"
    assert log == ["a released", "b done"], log
    assert out["a"] == ("dendra1a", "FEES"), out
    assert out["b"] == "dendra1b", out
    assert reg.held() == 1


# -- The shipped relay, against a local fake gateway ------------------------------------------------
class FakeGateway(BaseHTTPRequestHandler):
    paths = []

    def do_GET(self):
        FakeGateway.paths.append(self.path)
        if self.path.startswith("/dendra/jobs/v1/job?"):
            body = LIST
        elif self.path == "/dendra/jobs/v1/job/late":
            body = job("late", "dendra1late")
        else:
            body = "{}"
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def test_shipped_relay_reads_the_job_list_newest_first(monkeypatch, tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeGateway.paths = []
    monkeypatch.setenv("DENDRA_RELAY_REGISTRY", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("DENDRA_RELAY_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("DENDRA_RELAY_SIGN", "off")
    for name in ("DENDRA_RELAY_TOKEN", "DENDRA_KIT_MIN", "DENDRA_KIT_DEADLINE", "DENDRA_PUBLIC"):
        monkeypatch.delenv(name, raising=False)
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("relay_under_test_jobs", str(MODEA / "relay.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.JOBS.refresh()
        assert mod.JOBS.client("late")[0] == "dendra1late"
        assert mod.JOBS.refresh()
        assert mod.JOBS.client("late")[0] == "dendra1late"
    finally:
        sys.path[:] = saved
        srv.shutdown()
        srv.server_close()
    lists = [p for p in FakeGateway.paths if p.startswith("/dendra/jobs/v1/job?")]
    assert len(lists) == 2
    assert all("pagination.reverse=true" in p and "pagination.limit=1000" in p for p in lists)
    assert FakeGateway.paths.count("/dendra/jobs/v1/job/late") == 1
