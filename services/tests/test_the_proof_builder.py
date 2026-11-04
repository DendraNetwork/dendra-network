"""Bench of how The Proof is built: one background thread builds, requests only serve what it published.

Every case drives the shipped code (`build_loop`, `publish`, `_refresh`, `start_builder`, the real
`Handler` on a loopback port) with only the chain reads replaced. The scheduling cases use a fake clock
that the build and the wait advance, so a build "longer than the interval" costs no real time.
"""
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROWS = [{"id": "j1", "state": "open+paid+optimistic", "fee": 1, "slashes": []}]


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _refused(attempts, what):
    def refuse(*a, **k):
        attempts.append((what, a[:1]))
        raise OSError(f"bench: {what} refused")
    return refuse


def _no_rpc(path, params):
    raise OSError("bench: no node")


@pytest.fixture()
def outside(monkeypatch):
    """Refuses, and records, every way out of the process that is not a loopback stub: a connection or a
    name lookup for another host, and any child process (the chain CLI). The list is what was attempted."""
    attempts = []
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        if not (isinstance(address, tuple) and address[0] in LOOPBACK):
            attempts.append(("connect", address))
            raise OSError("bench: connection outside loopback refused")
        return real_connect(self, address)

    def lookup(host, *a, **k):
        if host not in LOOPBACK:
            attempts.append(("lookup", host))
            raise socket.gaierror("bench: lookup outside loopback refused")
        return real_lookup(host, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(subprocess, "run", _refused(attempts, "spawn"))
    monkeypatch.setattr(subprocess, "Popen", _refused(attempts, "spawn"))
    return attempts


@pytest.fixture()
def proof(monkeypatch, outside):
    tp = importlib.import_module("the_proof")
    # Every reader that could reach a node or another service is replaced, at both levels (the proof's own
    # readers and the client's REST/RPC/CLI ones), so the bench holds whatever DENDRA_NODE says in the shell.
    monkeypatch.setattr(tp.dc, "list_jobs_full", lambda: ROWS)
    monkeypatch.setattr(tp.dc, "head", lambda: (9, None))
    monkeypatch.setattr(tp.dc, "pools", lambda: {})
    monkeypatch.setattr(tp.dc, "committee_seed_health", lambda: None)
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: None)
    monkeypatch.setattr(tp.dc, "held_summary", lambda: None)
    monkeypatch.setattr(tp.dc, "prune_window_blocks", lambda: None)
    monkeypatch.setattr(tp.dc, "list_all", lambda *a, **k: None)
    monkeypatch.setattr(tp.dc, "read_json", lambda *a, **k: None)
    monkeypatch.setattr(tp.dc, "rest_json", lambda *a, **k: None)
    monkeypatch.setattr(tp.dc, "rpc_json", lambda *a, **k: None)
    monkeypatch.setattr(tp, "_read_miners", lambda: [])
    monkeypatch.setattr(tp, "_read_commits", lambda: [])
    monkeypatch.setattr(tp, "_read_season_generator", lambda: None)
    monkeypatch.setattr(tp, "_read_audit_timing", lambda jobs, height: None)
    monkeypatch.setattr(tp, "_rpc_get", _no_rpc)
    monkeypatch.setattr(tp, "_CACHE", tp.new_cache())
    monkeypatch.setattr(tp, "REFRESH_S", 15.0)
    monkeypatch.setattr(tp, "_BUILDER", None)
    monkeypatch.setattr(tp, "_CHAIN_READER", None)
    monkeypatch.setattr(tp, "_CHAIN_FAILING", [False])
    yield tp
    tp._STOP.set()
    for th in (tp._BUILDER, tp._CHAIN_READER):
        if th is not None:
            th.join(10)


class FakeClock:
    """A clock that only moves when the build or the wait says so."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def drive(tp, clock, build_seconds, builds, refresh_s=15.0):
    """Run the shipped loop for `builds` builds; each build lasts `build_seconds[i]` fake seconds.
    Returns the (start, end) of every build and the waits the loop asked for."""
    spans, waits, running = [], [], []
    cache = tp._CACHE
    count = {"n": 0}

    def read():
        running.append(1)
        assert len(running) == 1, "two builds at once"
        start = clock()
        clock.advance(build_seconds[count["n"]])
        spans.append((start, clock()))
        count["n"] += 1
        running.pop()
        return {"jobs": ROWS, "pools": {}, "seed_health": {}, "height": 9}

    def build():
        tp.publish(cache, clock(), read, clock=clock, refresh_s=refresh_s)

    def sleep(s):
        waits.append(s)
        clock.advance(s)

    tp.build_loop(cache, build, refresh_s, sleep, lambda: count["n"] >= builds)
    return spans, waits


def served(tp):
    return json.loads(tp._CACHE["payload"])["refresh"]


def test_a_build_longer_than_the_interval_waits_four_times_its_duration_from_its_end(proof):
    clock = FakeClock()
    spans, waits = drive(proof, clock, [40.0, 40.0, 40.0], 3)
    assert waits == [160.0, 160.0, 160.0]
    for (_, end), (nxt, _) in zip(spans, spans[1:]):
        assert nxt - end == 160.0, "the gap is counted from the end of the previous build"
    assert [round(e - s, 1) for s, e in spans] == [40.0, 40.0, 40.0]


def test_at_most_one_build_starts_per_gap(proof):
    clock = FakeClock()
    durations = [40.0, 5.0, 1.0, 30.0, 2.0]
    spans, waits = drive(proof, clock, durations, 5)
    gaps = [max(15.0, 4 * d) for d in durations]
    assert waits == gaps
    for i, (start, _) in enumerate(spans[1:], 1):
        window = [s for s, _ in spans if start - gaps[i - 1] < s <= start]
        assert window == [start], f"a second build started inside the gap before build {i}"


def test_a_fast_build_keeps_the_interval_and_raises_no_alert(proof):
    clock = FakeClock()
    _, waits = drive(proof, clock, [2.0, 3.0], 2)
    assert waits == [15.0, 15.0]
    rf = served(proof)
    assert rf["gap_s"] == 15.0 and rf["slow_build"] is False and rf["build_s"] == 3.0


def test_a_slow_build_publishes_the_alert_the_gap_and_the_duration(proof):
    clock = FakeClock()
    drive(proof, clock, [40.0], 1)
    rf = served(proof)
    assert rf["slow_build"] is True and rf["gap_s"] == 160.0 and rf["build_s"] == 40.0
    assert rf["current"] is True and rf["snapshot_generated_at"] is not None


def test_a_failed_slow_attempt_backs_off_the_same_way(proof, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(proof.dc, "list_jobs_full", lambda: None)
    waits = []

    def read():
        clock.advance(30.0)
        return proof._read_inputs()

    n = {"n": 0}

    def build():
        n["n"] += 1
        proof.publish(proof._CACHE, clock(), read, clock=clock, refresh_s=15.0)
    proof.build_loop(proof._CACHE, build, 15.0, lambda s: (waits.append(s), clock.advance(s)),
                     lambda: n["n"] >= 1)
    rf = served(proof)
    assert rf["current"] is False and rf["slow_build"] is True and rf["gap_s"] == 120.0
    assert waits == [120.0]


def test_the_fields_a_reader_already_uses_are_unchanged(proof):
    clock = FakeClock()
    drive(proof, clock, [40.0], 1)
    rf = served(proof)
    for key in ("current", "attempted_at", "failing_since", "reason", "snapshot_generated_at", "build_s"):
        assert key in rf
    assert rf["failing_since"] is None and rf["reason"] is None


def test_nothing_is_claimed_about_the_pace_before_the_first_build_or_without_an_interval(proof):
    rf = served(proof)
    assert rf["gap_s"] is None and rf["slow_build"] is None and rf["reason"] == proof.NOT_YET_BUILT
    cache = proof.new_cache()
    proof.publish(cache, 5, lambda: {"jobs": ROWS, "pools": {}, "seed_health": {}, "height": 9})
    rf = json.loads(cache["payload"])["refresh"]
    assert rf["gap_s"] is None and rf["slow_build"] is None, "a pace nobody asked for was invented"


def test_a_build_that_raises_does_not_kill_the_loop_and_waits_the_interval(proof, capsys):
    waits, n = [], {"n": 0}

    def build():
        n["n"] += 1
        raise RuntimeError("boom")
    proof.build_loop(proof._CACHE, build, 15.0, waits.append, lambda: n["n"] >= 2)
    assert n["n"] == 2 and waits == [15.0, 15.0]
    assert "RuntimeError" in capsys.readouterr().err


def test_a_stale_gap_from_an_earlier_build_is_not_reused_after_a_crash(proof):
    waits, n = [], {"n": 0}

    def build():
        n["n"] += 1
        if n["n"] == 1:
            proof._CACHE["gap_s"] = 999.0
            return
        raise RuntimeError("boom")
    proof.build_loop(proof._CACHE, build, 15.0, waits.append, lambda: n["n"] >= 2)
    assert waits == [999.0, 15.0]


class _Server:
    def __init__(self, handler):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path, timeout=5):
        url = f"http://127.0.0.1:{self.srv.server_address[1]}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_a_request_never_starts_nor_waits_for_a_build(proof, monkeypatch):
    calls = []
    monkeypatch.setattr(proof, "_refresh", lambda: calls.append(1))
    monkeypatch.setattr(proof.dc, "list_jobs_full", lambda: calls.append("listing"))
    srv = _Server(proof.Handler)
    try:
        for _ in range(5):
            doc = srv.get("/proof")
            assert doc["refresh"]["reason"] == proof.NOT_YET_BUILT
    finally:
        srv.close()
    assert calls == [], "a request started a build"


def test_requests_are_served_the_last_payload_while_a_build_is_running(proof, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    walks = []

    def slow_listing():
        walks.append(1)
        entered.set()
        release.wait(10)
        return ROWS
    monkeypatch.setattr(proof.dc, "list_jobs_full", slow_listing)
    srv = _Server(proof.Handler)
    builder = None
    try:
        builder = proof.start_builder()
        assert entered.wait(5)
        started = time.monotonic()
        during = [srv.get("/proof", timeout=2) for _ in range(5)]
        elapsed = time.monotonic() - started
        assert elapsed < 2, "a request waited for the build"
        assert all(d["refresh"]["reason"] == proof.NOT_YET_BUILT for d in during)
        assert len(walks) == 1, "requests started builds of their own"
        assert proof.start_builder() is builder, "a second builder thread was started"
    finally:
        release.set()
        proof._STOP.set()
        if builder is not None:
            builder.join(10)
        srv.close()
    assert not builder.is_alive()


def test_two_callers_never_build_at_once(proof, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    walks = []

    def slow_listing():
        walks.append(1)
        inside.set()
        release.wait(10)
        return ROWS
    monkeypatch.setattr(proof.dc, "list_jobs_full", slow_listing)
    first = threading.Thread(target=proof._refresh)
    first.start()
    try:
        assert inside.wait(5)
        proof._refresh()                                    # the second caller skips
    finally:
        release.set()
        first.join(10)
    assert len(walks) == 1
    assert served(proof)["current"] is True and served(proof)["gap_s"] is not None


# -- Verdicts about now are withdrawn from a payload whose chain read is old ------------------------------


def _published(proof, read_at, block_age=5, height=9):
    """Publish one payload whose status read was made at `read_at`, on a block `block_age` seconds old."""
    proof.publish(proof._CACHE, read_at, lambda: {"jobs": ROWS, "pools": {}, "seed_health": {}, "height": height,
                                                  "block_epoch": read_at - block_age, "read_at": read_at,
                                                  "now": read_at},
                  refresh_s=15.0)
    return proof._CACHE["payload"]


def _verdicts(body):
    chain = json.loads(body)["chain"]
    return chain["stale"], chain["last_block_age_s"]


def test_a_payload_is_served_with_its_verdicts_until_its_read_is_stale_threshold_old(proof):
    body = _published(proof, 5000)
    limit = proof.STALE_BLOCK_S
    assert _verdicts(proof.serve_body(body, 5000)) == (False, 5)
    assert _verdicts(proof.serve_body(body, 5000 + limit - 1)) == (False, 5)
    assert proof.serve_body(body, 5000 + limit - 1) == body, "a fresh enough payload is served as published"


def test_a_payload_whose_read_is_stale_threshold_old_loses_its_verdicts_but_keeps_its_facts(proof):
    body = _published(proof, 5000)
    before = bytes(proof._CACHE["payload"])
    for now in (5000 + proof.STALE_BLOCK_S, 5000 + 15 * 60):
        out = json.loads(proof.serve_body(body, now))
        assert (out["chain"]["stale"], out["chain"]["last_block_age_s"]) == (None, None)
        assert out["chain"]["read_at"] == 5000 and out["chain"]["last_block_epoch"] == 4995
        assert out["height"] == 9 and out["jobs"]["total"] == 1 and out["refresh"]["current"] is True
    assert proof._CACHE["payload"] == before and _verdicts(before) == (False, 5), "the cached payload was changed"


def test_a_clock_behind_the_read_by_the_threshold_withdraws_the_verdicts_too(proof):
    body = _published(proof, 5000)
    assert _verdicts(proof.serve_body(body, 5000 - proof.STALE_BLOCK_S + 1)) == (False, 5)
    assert _verdicts(proof.serve_body(body, 5000 - proof.STALE_BLOCK_S)) == (None, None)


def test_a_payload_without_a_chain_section_is_served_as_it_is(proof):
    body = proof._CACHE["payload"]
    assert proof.serve_body(body, 10 ** 9) == body


def test_a_chain_section_without_a_usable_read_time_is_not_served_as_current(proof):
    doc = json.loads(_published(proof, 5000))
    doc["chain"]["read_at"] = None
    body = json.dumps(doc).encode()
    assert _verdicts(proof.serve_body(body, 5000)) == (None, None)


def test_the_handler_ages_the_payload_against_its_clock(proof, monkeypatch):
    _published(proof, 5000)
    clock = FakeClock()
    clock.t = 5000.0
    monkeypatch.setattr(proof, "_NOW", clock)
    srv = _Server(proof.Handler)
    try:
        first = srv.get("/proof")
        assert (first["chain"]["stale"], first["chain"]["last_block_age_s"]) == (False, 5)
        clock.advance(proof.STALE_BLOCK_S - 1)
        assert srv.get("/proof")["chain"]["stale"] is False
        clock.advance(1)
        doc = srv.get("/proof")
        assert doc["chain"]["stale"] is None and doc["chain"]["last_block_age_s"] is None
        assert doc["chain"]["read_at"] == 5000
    finally:
        srv.close()
    assert _verdicts(proof._CACHE["payload"]) == (False, 5)


# -- DENDRA_PROOF_REFRESH_S is read once, at start, and refused when it is not usable ---------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "1e999", "0", "0.0", "-1", "-0.5", "0.04", "0.5",
                                 "0.99", "86400.5", "1e9", "", "abc", "15s"])
def test_an_unusable_refresh_interval_is_refused_with_a_message(proof, raw):
    with pytest.raises(SystemExit) as exc:
        proof.read_refresh_s(raw)
    assert "DENDRA_PROOF_REFRESH_S" in str(exc.value) and repr(raw) in str(exc.value)


@pytest.mark.parametrize("raw,want", [("1", 1.0), ("15", 15.0), ("1.04", 1.04), (" 30 ", 30.0), ("86400", 86400.0)])
def test_a_usable_refresh_interval_is_read_as_written(proof, raw, want):
    assert proof.read_refresh_s(raw) == want


# DENDRA_MODEA_DIR points this bench at a copy of the modules, so a mutation can be checked against it.
HERE = os.environ.get("DENDRA_MODEA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_service(**setting):
    """Start the real module with `setting` (environment variables) and return the finished process. Every
    address it could reach is pinned to a closed loopback port, and the listener to an ephemeral one, so that
    a refusal that goes missing starts a service that touches nothing; it is then killed at the timeout."""
    env = dict(os.environ, DENDRA_PROOF_PORT="0", DENDRA_PROOF_HOST="127.0.0.1", DENDRA_NODE="tcp://127.0.0.1:1",
               DENDRA_PROOF_RPC="http://127.0.0.1:1", DENDRA_REST_URL="http://127.0.0.1:1",
               DENDRA_RPC_URL="http://127.0.0.1:1", DENDRA_PROOF_SEASON_STATUS="http://127.0.0.1:1/")
    env.update(setting)
    return subprocess.run([sys.executable, "-I", os.path.join(HERE, "the_proof.py")], env=env, cwd=HERE,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("name,raw", [("DENDRA_PROOF_REFRESH_S", "nan"), ("DENDRA_PROOF_REFRESH_S", "0.04"),
                                      ("DENDRA_PROOF_STALE_S", "59"), ("DENDRA_PROOF_STALE_S", "86401"),
                                      ("DENDRA_PROOF_STALE_S", "abc"),
                                      ("DENDRA_PROOF_CHAIN_REFRESH_S", "4"),
                                      ("DENDRA_PROOF_CHAIN_REFRESH_S", "301")])
def test_the_service_exits_at_start_on_a_bad_setting(name, raw):
    r = _start_service(**{name: raw})
    assert r.returncode != 0 and f"{name}={raw!r} is refused" in r.stderr


def test_the_service_exits_at_start_when_the_chain_reading_cannot_beat_the_threshold():
    r = _start_service(DENDRA_PROOF_CHAIN_REFRESH_S="40", DENDRA_PROOF_STALE_S="60")
    assert r.returncode != 0 and "DENDRA_PROOF_CHAIN_REFRESH_S=40 is refused" in r.stderr


@pytest.mark.parametrize("raw", ["", "abc", "300.5", "59", "86401", "-300", "0", "nan", "1e3"])
def test_an_unusable_stale_threshold_is_refused_with_a_message(proof, raw):
    with pytest.raises(SystemExit) as exc:
        proof.read_stale_s(raw)
    assert "DENDRA_PROOF_STALE_S" in str(exc.value) and repr(raw) in str(exc.value)


@pytest.mark.parametrize("raw,want", [("60", 60), ("300", 300), (" 900 ", 900), ("86400", 86400)])
def test_a_usable_stale_threshold_is_read_as_written(proof, raw, want):
    assert proof.read_stale_s(raw) == want


@pytest.mark.parametrize("raw", ["nan", "inf", "-5", "0", "4.9", "300.1", "1e9", "", "abc", "30s"])
def test_an_unusable_chain_refresh_interval_is_refused_with_a_message(proof, raw):
    with pytest.raises(SystemExit) as exc:
        proof.read_chain_refresh_s(raw)
    assert "DENDRA_PROOF_CHAIN_REFRESH_S" in str(exc.value) and repr(raw) in str(exc.value)


@pytest.mark.parametrize("raw,want", [("5", 5.0), ("30", 30.0), (" 7.5 ", 7.5), ("300", 300.0)])
def test_a_usable_chain_refresh_interval_is_read_as_written(proof, raw, want):
    assert proof.read_chain_refresh_s(raw) == want


def test_a_failed_wait_does_not_end_the_builder_loop(proof, capsys):
    waits, n = [], {"n": 0}

    def build():
        n["n"] += 1

    def wait(s):
        waits.append(s)
        raise OverflowError("timeout value is too large")
    proof.build_loop(proof._CACHE, build, 15.0, wait, lambda: n["n"] >= 2)
    assert n["n"] == 2 and waits == [15.0, 15.0], "the loop ended at the failed wait"
    assert "OverflowError" in capsys.readouterr().err


def test_the_real_event_wait_cannot_end_the_loop_when_its_timeout_overflows(proof, capsys):
    n = {"n": 0}

    def build():
        n["n"] += 1
    proof.build_loop(proof._CACHE, build, 1e300, threading.Event().wait, lambda: n["n"] >= 2)
    assert n["n"] == 2
    assert "OverflowError" in capsys.readouterr().err


# -- main() starts the builder -----------------------------------------------------------------------------


def test_main_starts_the_threads_before_it_serves(proof, monkeypatch, capsys):
    events = []

    class StubServer:
        def __init__(self, address, handler):
            events.append(("bind", address, handler))

        def serve_forever(self):
            # What is true when the listener starts taking requests: both threads exist and run.
            events.append(("serve", proof._BUILDER is not None and proof._BUILDER.is_alive(),
                           proof._CHAIN_READER is not None and proof._CHAIN_READER.is_alive()))
    monkeypatch.setattr(proof, "ThreadingHTTPServer", StubServer)
    proof.main()
    builder, chain = proof._BUILDER, proof._CHAIN_READER
    try:
        assert events == [("bind", (proof.HOST, proof.PORT), proof.Handler), ("serve", True, True)]
        assert builder.name == "the-proof-builder" and chain.name == "the-proof-chain"
        assert builder.is_alive() and chain.is_alive()
    finally:
        proof._STOP.set()
        builder.join(10)
        chain.join(10)
    assert not builder.is_alive() and not chain.is_alive()


# -- The pace alert ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("seconds,slow", [(14.9, False), (15.0, False), (15.1, True)])
def test_a_build_of_exactly_the_interval_is_not_slow(proof, seconds, slow):
    clock = FakeClock()
    drive(proof, clock, [seconds], 1)
    rf = served(proof)
    assert rf["build_s"] == round(seconds, 1) and rf["slow_build"] is slow


def test_a_failed_attempt_of_exactly_the_interval_is_not_slow_either(proof):
    clock = FakeClock()

    def read():
        clock.advance(15.0)
        raise proof.Unmeasured("list-job unreadable or incomplete")
    proof.publish(proof._CACHE, clock(), read, clock=clock, refresh_s=15.0)
    rf = served(proof)
    assert rf["current"] is False and rf["slow_build"] is False and rf["build_s"] == 15.0


# -- The bench itself stays inside the process -------------------------------------------------------------


def test_the_readers_reach_nothing_outside_whatever_the_node_setting_says(proof, outside, monkeypatch):
    monkeypatch.setattr(proof.dc, "NODE", "tcp://192.0.2.1:26657")
    monkeypatch.setenv("DENDRA_REST_URL", "http://192.0.2.1:1317")
    monkeypatch.setenv("DENDRA_RPC_URL", "http://192.0.2.1:26657")
    monkeypatch.setattr(proof, "SEASON_STATUS", "http://192.0.2.1:8093/final-season/v1/status")
    got = proof._read_inputs()
    assert got["jobs"] == ROWS and got["miners"] == [] and got["commits"] == []
    assert outside == [], f"the bench left the process: {outside}"


def test_the_guard_refuses_a_connection_a_lookup_and_a_child_process(outside):
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 9), timeout=1)
    with pytest.raises(OSError):
        socket.socket().connect(("192.0.2.1", 9))
    with pytest.raises(OSError):
        subprocess.run(["dendrad", "version"])
    with pytest.raises(OSError):
        subprocess.Popen(["dendrad", "version"])
    assert {a[0] for a in outside} == {"lookup", "connect", "spawn"}



# -- The published gap never rounds below the interval ----------------------------------------------------


@pytest.mark.parametrize("refresh_s,took", [(1.04, 0.1), (15.04, 2.0), (1.0, 0.0), (15.0, 3.8)])
def test_the_published_gap_is_never_below_the_interval_that_was_asked_for(proof, refresh_s, took):
    ticks = iter([0.0, took])
    cache = proof.new_cache()
    proof.publish(cache, 2000, lambda: {"jobs": ROWS, "pools": {}, "seed_health": {}, "height": 9},
                  clock=lambda: next(ticks), refresh_s=refresh_s)
    gap = json.loads(cache["payload"])["refresh"]["gap_s"]
    assert gap >= refresh_s and cache["gap_s"] == gap


# -- The chain section is kept current by its own light reading ------------------------------------------


def _status(height="12", when="2026-10-19T10:00:00.123456789Z"):
    sync = {"latest_block_height": height}
    if when is not None:
        sync["latest_block_time"] = when
    return {"jsonrpc": "2.0", "result": {"sync_info": sync}}


EPOCH = 1792404000  # 2026-10-19T10:00:00Z


def _build_at(proof, read_at, block_age=5, height=9, cache=None, refresh_s=15.0):
    cache = proof._CACHE if cache is None else cache
    proof.publish(cache, read_at, lambda: {"jobs": ROWS, "pools": {}, "seed_health": {}, "height": height,
                                           "block_epoch": read_at - block_age, "read_at": read_at, "now": read_at},
                  refresh_s=refresh_s)
    return cache


def _chain_at(proof, read_at, block_age=3, height=40):
    return proof.chain_block(height, read_at - block_age, read_at)


def _failing_read(proof):
    def read():
        raise proof.Unmeasured("list-job unreadable")
    return read


def test_a_light_reading_replaces_only_the_chain_section_and_never_touches_what_was_published(proof):
    _build_at(proof, 5000)
    before_bytes = proof._CACHE["payload"]
    before_doc = proof._CACHE["doc"]
    before_chain = dict(before_doc["chain"])
    assert proof.publish_chain(proof._CACHE, _chain_at(proof, 5200)) is True
    now_doc = json.loads(proof._CACHE["payload"])
    old_doc = json.loads(before_bytes)
    assert now_doc["chain"] == {"height": 40, "last_block_epoch": 5197, "last_block_age_s": 3, "stale": False,
                                "stale_threshold_s": proof.STALE_BLOCK_S, "read_at": 5200}
    assert {k: v for k, v in now_doc.items() if k != "chain"} == {k: v for k, v in old_doc.items() if k != "chain"}
    assert now_doc["height"] == 9, "the top-level height belongs to the counts it was read with"
    assert before_bytes == json.dumps(old_doc, ensure_ascii=False).encode(), "bytes a handler holds changed"
    assert proof._CACHE["doc"] is not before_doc and before_doc["chain"] == before_chain, "the old doc was mutated"


def test_a_light_reading_keeps_the_verdicts_served_between_heavy_builds(proof):
    # The heavy build ran at 5000 and the next one is far away: at 5600 the build's own chain reading is
    # older than the threshold and is withdrawn, the light one is 10 s old and is not.
    _build_at(proof, 5000)
    assert _verdicts(proof.serve_body(proof._CACHE["payload"], 5600)) == (None, None)
    proof.publish_chain(proof._CACHE, _chain_at(proof, 5590))
    assert _verdicts(proof.serve_body(proof._CACHE["payload"], 5600)) == (False, 3)
    assert "verdict_withdrawn" not in json.loads(proof.serve_body(proof._CACHE["payload"], 5600))["chain"]


def test_the_fields_the_site_reads_from_the_chain_section_are_all_present_after_a_light_reading(proof):
    # site/network/index.html reads stale, last_block_age_s, stale_threshold_s and read_at; the top-level
    # height is the one site/chat/index.html reads. last_block_epoch and height complete the section.
    _build_at(proof, 5000)
    proof.publish_chain(proof._CACHE, _chain_at(proof, 5030))
    doc = json.loads(proof._CACHE["payload"])
    assert set(doc["chain"]) == {"height", "last_block_epoch", "last_block_age_s", "stale", "stale_threshold_s",
                                 "read_at"}
    assert isinstance(doc["height"], int) and doc["refresh"]["current"] is True


def test_a_reading_that_is_not_newer_changes_nothing(proof):
    _build_at(proof, 5000)
    proof.publish_chain(proof._CACHE, _chain_at(proof, 5030))
    kept = proof._CACHE["payload"]
    assert proof.publish_chain(proof._CACHE, _chain_at(proof, 5030, height=41)) is False
    assert proof.publish_chain(proof._CACHE, _chain_at(proof, 4000)) is False
    assert proof._CACHE["payload"] == kept


def test_a_light_reading_before_the_first_build_is_kept_for_the_build_and_serves_nothing(proof):
    unbuilt = proof._CACHE["payload"]
    assert proof.publish_chain(proof._CACHE, _chain_at(proof, 5030)) is False
    assert proof._CACHE["payload"] == unbuilt and "chain" not in json.loads(unbuilt)
    _build_at(proof, 5000)
    assert json.loads(proof._CACHE["payload"])["chain"]["read_at"] == 5030


def test_a_build_does_not_publish_a_chain_older_than_the_light_reading_taken_while_it_ran(proof):
    _build_at(proof, 5000)
    proof.publish_chain(proof._CACHE, _chain_at(proof, 5300))
    _build_at(proof, 5100)                          # a build whose status read is older than the light one
    assert json.loads(proof._CACHE["payload"])["chain"]["read_at"] == 5300
    _build_at(proof, 5400)                          # and one that is newer replaces it
    assert json.loads(proof._CACHE["payload"])["chain"]["read_at"] == 5400


def test_a_light_reading_held_between_its_read_and_its_swap_cannot_undo_a_build_published_meanwhile(proof, monkeypatch):
    # The light thread reads the served payload, then swaps a copy in. If a heavy build could publish between
    # the two, the copy taken from the older payload would replace it, and the build would be lost until the
    # next one. The swap lock is what forbids that: the build waits for the light thread to finish.
    _build_at(proof, 5000, height=9)
    entered, go = threading.Event(), threading.Event()
    real_loads = proof.json.loads

    def held_loads(*a, **k):
        if threading.current_thread().name == "light-race":
            entered.set()
            go.wait(5)
        return real_loads(*a, **k)
    monkeypatch.setattr(proof.json, "loads", held_loads)
    light = threading.Thread(name="light-race", target=proof.publish_chain, args=(proof._CACHE, _chain_at(proof, 5300)))
    light.start()
    assert entered.wait(5)
    heavy = threading.Thread(name="heavy-race", target=_build_at, args=(proof, 5200), kwargs={"height": 77})
    heavy.start()
    time.sleep(0.3)                                 # the build runs now if nothing holds it back
    go.set()
    light.join(5)
    heavy.join(5)
    monkeypatch.setattr(proof.json, "loads", real_loads)
    doc = json.loads(proof._CACHE["payload"])
    assert doc["height"] == 77                      # the build published meanwhile is the one served
    assert doc["chain"]["read_at"] == 5300          # with the newer light reading


def test_a_failed_build_serves_the_light_reading_as_a_measurement_and_a_kept_one_with_its_reason(proof):
    _build_at(proof, 5000)
    proof.publish_chain(proof._CACHE, _chain_at(proof, 5300))
    proof.publish(proof._CACHE, 5310, _failing_read(proof))
    doc = json.loads(proof._CACHE["payload"])
    assert doc["refresh"]["current"] is False and doc["chain"]["read_at"] == 5300
    assert (doc["chain"]["stale"], doc["chain"]["last_block_age_s"]) == (False, 3)
    # Without a light reading newer than the snapshot's own, the verdicts are withdrawn and the copy says why.
    other = proof.new_cache()
    _build_at(proof, 6000, cache=other)
    proof.publish(other, 6010, _failing_read(proof))
    kept = json.loads(other["payload"])["chain"]
    assert (kept["stale"], kept["last_block_age_s"]) == (None, None)
    assert kept["verdict_withdrawn"] == "the last refresh failed"


def _read_chain_from(proof, monkeypatch, answer, now=EPOCH + 2):
    monkeypatch.setattr(proof.dc, "rpc_json", lambda *a, **k: answer)
    monkeypatch.setattr(proof, "_NOW", lambda: now)
    return proof._read_chain()


def test_the_light_reader_dates_the_block_from_the_status_it_read(proof, monkeypatch):
    got = _read_chain_from(proof, monkeypatch, _status("12"))
    assert got == {"height": 12, "last_block_epoch": EPOCH, "last_block_age_s": 2, "stale": False,
                   "stale_threshold_s": proof.STALE_BLOCK_S, "read_at": EPOCH + 2}


def test_a_status_without_a_block_time_is_a_reading_with_no_verdict(proof, monkeypatch):
    got = _read_chain_from(proof, monkeypatch, _status("12", when=None))
    assert got["height"] == 12 and got["last_block_epoch"] is None
    assert got["stale"] is None and got["last_block_age_s"] is None


@pytest.mark.parametrize("answer", [None, {}, {"result": None}, {"result": {}}, {"result": {"sync_info": []}},
                                    _status(height="0"), _status(height="-3"), _status(height="abc"),
                                    {"result": {"sync_info": {"latest_block_time": "2026-10-19T10:00:00Z"}}}])
def test_a_status_with_no_usable_height_is_unmeasured_never_a_default_block(proof, monkeypatch, answer):
    with pytest.raises(proof.Unmeasured):
        _read_chain_from(proof, monkeypatch, answer)


def test_the_light_step_publishes_a_reading_and_a_failed_one_leaves_the_payload_to_the_strip(proof, monkeypatch,
                                                                                            capsys):
    _build_at(proof, EPOCH - 400)
    monkeypatch.setattr(proof, "_NOW", lambda: EPOCH + 2)
    monkeypatch.setattr(proof.dc, "rpc_json", lambda *a, **k: _status("12"))
    proof._chain_step()
    served_now = json.loads(proof.serve_body(proof._CACHE["payload"], EPOCH + 2))["chain"]
    assert served_now["height"] == 12 and served_now["stale"] is False and served_now["read_at"] == EPOCH + 2
    # The node stops answering: the section ages, and past the threshold the copy says it was withdrawn.
    monkeypatch.setattr(proof.dc, "rpc_json", lambda *a, **k: None)
    before = proof._CACHE["payload"]
    proof._chain_step()
    proof._chain_step()
    assert proof._CACHE["payload"] == before
    late = json.loads(proof.serve_body(proof._CACHE["payload"], EPOCH + 2 + proof.STALE_BLOCK_S))["chain"]
    assert (late["stale"], late["last_block_age_s"]) == (None, None)
    assert late["verdict_withdrawn"] == "the chain reading is older than stale_threshold_s"
    err = capsys.readouterr().err
    assert err.count("chain reading: node status unreadable") == 1, "a long outage was logged on every attempt"
    monkeypatch.setattr(proof.dc, "rpc_json", lambda *a, **k: _status("13"))
    proof._chain_step()
    assert "chain reading: recovered" in capsys.readouterr().err


def test_the_light_reader_never_starts_the_chain_cli(proof, monkeypatch, outside):
    monkeypatch.setattr(proof.dc, "rpc_json", lambda *a, **k: _status("12"))
    monkeypatch.setenv(proof.dc.CHAIN_READ_ENV, "cli")
    monkeypatch.setattr(proof.dc, "head", lambda: pytest.fail("the light reader used head()"))
    proof._chain_step()
    assert outside == []


@pytest.mark.parametrize("read_at,now,why", [
    (5000, 5000 + 300, "the chain reading is older than stale_threshold_s"),
    (5000, 5000 - 300, "the clock of the feed is behind the chain reading by stale_threshold_s or more"),
    (None, 5000, "the chain reading carries no usable time"),
    ("5000", 5000, "the chain reading carries no usable time"),
])
def test_a_payload_served_without_its_verdicts_says_why_in_the_copy_it_serves(proof, read_at, now, why):
    doc = json.loads(_published(proof, 5000))
    doc["chain"]["read_at"] = read_at
    out = json.loads(proof.serve_body(json.dumps(doc).encode(), now))["chain"]
    assert (out["stale"], out["last_block_age_s"]) == (None, None) and out["verdict_withdrawn"] == why


def test_a_payload_served_in_time_carries_no_withdrawal_mark(proof):
    out = json.loads(proof.serve_body(_published(proof, 5000), 5000))["chain"]
    assert "verdict_withdrawn" not in out and out["stale"] is False


def test_the_first_reason_stays_when_a_kept_payload_is_stripped_again(proof):
    other = proof.new_cache()
    _build_at(proof, 6000, cache=other)
    proof.publish(other, 6010, _failing_read(proof))
    out = json.loads(proof.serve_body(other["payload"], 6000 + proof.STALE_BLOCK_S))["chain"]
    assert out["verdict_withdrawn"] == "the last refresh failed"


def test_the_chain_thread_reads_the_node_on_its_own_and_publishes_while_a_build_runs(proof, monkeypatch):
    # A build is held open: the light reading must still land in the served payload, which proves it does
    # not wait for the heavy build nor for its lock.
    _build_at(proof, 5000)
    entered, release, read = threading.Event(), threading.Event(), threading.Event()

    def slow_listing():
        entered.set()
        release.wait(10)
        return ROWS

    def status(*a, **k):
        read.set()
        return _status("77")
    monkeypatch.setattr(proof.dc, "list_jobs_full", slow_listing)
    monkeypatch.setattr(proof.dc, "rpc_json", status)
    monkeypatch.setattr(proof, "_NOW", lambda: 6000)
    try:
        proof.start_builder()
        assert entered.wait(5) and read.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and json.loads(proof._CACHE["payload"])["chain"]["height"] != 77:
            time.sleep(0.02)
        assert json.loads(proof._CACHE["payload"])["chain"]["height"] == 77
        assert proof._CHAIN_READER.is_alive() and proof._CHAIN_READER.name == "the-proof-chain"
    finally:
        release.set()
        proof._STOP.set()
