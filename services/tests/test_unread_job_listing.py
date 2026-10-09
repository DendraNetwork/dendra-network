"""Bench of an UNREAD job listing, from the reader to every consumer that publishes it.

Every consumer turns the job listing into counts it publishes. Answered as `[]`, an unread listing
would serve an outage of the node as a network with no job, no audit and no slash -- on the reassuring
side, in a form nothing downstream can tell apart from a real reading. So `list_jobs_full()` answers
None for "not read", and [] stays what it is: a measurement.

Each consumer is driven through its SHIPPED function, with only the chain reads replaced:
  * the reader      -- `list_all` / `list_jobs_full`: `{}` is a read page of zero entries, a failed
                       command is None, and a later page that fails never yields a prefix;
  * The Proof       -- `_refresh` keeps the last snapshot it published and SAYS so (`refresh`), minus
                       its verdicts about NOW (a kept "the chain is live" outlives the chain);
  * the exporter    -- `refresh` keeps its R2 gauges instead of computing R2 over nothing, which
                       would raise `dendra_r2_stop_breach` on a node outage;
  * the points      -- `snapshot` refuses, so its server keeps the payload it last built, marked,
                       and retries at the refresh pace, never once per request.
The two servers are also driven through their real HTTP handler, on a loopback port: what a request
is served during the first walk, and how many walks N requests start, are properties of the handler
and of the cache it starts from, not of the functions alone.
"""
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402


class _Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _script(monkeypatch, *pages):
    """`subprocess.run` answering the scripted pages in order. A page is (rc, stdout) or an exception."""
    calls = []
    queue = list(pages)

    def run(cmd, **kw):
        calls.append(list(cmd))
        p = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(p, BaseException):
            raise p
        return _Proc(*p)
    monkeypatch.setattr(dc.subprocess, "run", run)
    return calls


J1 = {"job_id": "j1", "state": "open+paid+optimistic", "miner_id": "m1", "client": "c1", "fee": "100"}
J2 = {"jobId": "j2", "state": "open", "minerId": "m2", "client": "c2", "fee": "50"}


# ── the reader ──────────────────────────────────────────────────────────────────────────────────
def test_an_empty_object_is_a_page_of_zero_jobs_not_a_failure(monkeypatch):
    _script(monkeypatch, (0, "{}"))
    assert dc.list_all("list-job", "job") == []
    assert dc.list_jobs_full() == []


@pytest.mark.parametrize("page", [
    (1, ""),                                               # the command failed
    (1, '{"job": []}'),                                    # failed, whatever it printed
    (0, ""),                                               # printed nothing
    (0, "Error: post failed"),                             # printed no JSON
    (0, "[]"),                                             # JSON, not an object
    (0, '{"job": {"job_id": "j1"}}'),                      # the list is not a list
    (0, '{"job": [], "pagination": "next"}'),              # the continuation is not an object
    subprocess.TimeoutExpired(["dendrad"], 180),           # the node never answered
    OSError("dendrad not found"),                          # no binary
])
def test_an_unread_page_is_none_never_an_empty_list(monkeypatch, page):
    _script(monkeypatch, page)
    assert dc.list_all("list-job", "job") is None
    assert dc.list_jobs_full() is None


def test_pages_are_followed_and_a_failing_later_page_never_yields_a_prefix(monkeypatch):
    calls = _script(monkeypatch, (0, json.dumps({"job": [J1], "pagination": {"next_key": "K2"}})),
                    (0, json.dumps({"job": [J2]})))
    assert [r.get("job_id") or r.get("jobId") for r in dc.list_all("list-job", "job")] == ["j1", "j2"]
    assert calls[1][calls[1].index("--page-key") + 1] == "K2"
    _script(monkeypatch, (0, json.dumps({"job": [J1], "pagination": {"next_key": "K2"}})), (1, ""))
    assert dc.list_jobs_full() is None


def test_a_listing_that_never_ends_is_refused(monkeypatch):
    _script(monkeypatch, (0, json.dumps({"job": [J1], "pagination": {"next_key": "again"}})))
    assert dc.list_all("list-job", "job", max_pages=3) is None


def test_rows_are_normalised_and_a_row_that_is_not_an_object_makes_the_listing_unread(monkeypatch):
    _script(monkeypatch, (0, json.dumps({"job": [J1, J2]})))
    rows = dc.list_jobs_full()
    assert [(r["id"], r["miner_id"], r["fee"]) for r in rows] == [("j1", "m1", 100), ("j2", "m2", 50)]
    _script(monkeypatch, (0, json.dumps({"job": [J1, "j2"]})))
    assert dc.list_jobs_full() is None


# ── The Proof ───────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def proof(monkeypatch):
    tp = importlib.import_module("the_proof")
    listing = {"next": None, "calls": 0}

    def list_jobs_full():
        listing["calls"] += 1
        return listing["next"]
    monkeypatch.setattr(tp.dc, "list_jobs_full", list_jobs_full)
    monkeypatch.setattr(tp.dc, "head", lambda: (9, None))
    monkeypatch.setattr(tp.dc, "pools", lambda: {})
    monkeypatch.setattr(tp.dc, "committee_seed_health", lambda: None)
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: None)
    monkeypatch.setattr(tp.dc, "held_summary", lambda: None)
    monkeypatch.setattr(tp.dc, "prune_window_blocks", lambda: None)
    # The SHIPPED initial state, never one written here: a bench that fabricates the cache it starts
    # from cannot see what the server serves before its first refresh completes.
    monkeypatch.setattr(tp, "_CACHE", tp.new_cache())
    monkeypatch.setattr(tp, "REFRESH_S", 0.0)
    return tp, listing


def _served(tp):
    return json.loads(tp._CACHE["payload"])


class _Server:
    """The module's real `Handler` on a loopback port chosen by the system."""

    def __init__(self, handler):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def get(self, path, timeout=10):
        url = f"http://127.0.0.1:{self.srv.server_address[1]}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _leaves(d, prefix=()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _leaves(v, prefix + (k,))
    else:
        yield prefix, d


def test_the_proof_keeps_its_last_snapshot_and_says_so(proof):
    tp, listing = proof
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic", "fee": 1, "slashes": []},
                       {"id": "j2", "state": "open+paid+optimistic+disputed", "fee": 1, "slashes": []}]
    tp._refresh()
    first = _served(tp)
    assert first["jobs"]["total"] == 2 and first["jobs"]["audited"] == 1
    assert first["refresh"]["current"] is True and first["refresh"]["reason"] is None

    listing["next"] = None                                  # list-job unreadable
    tp._refresh()
    kept = _served(tp)
    assert kept["jobs"] == first["jobs"], "an unread listing must never be served as zeros"
    assert kept["generated_at"] == first["generated_at"]
    assert kept["refresh"]["current"] is False
    assert kept["refresh"]["reason"] == "list-job unreadable or incomplete"
    assert kept["refresh"]["snapshot_generated_at"] == first["generated_at"]
    assert kept["refresh"]["failing_since"] is not None

    listing["next"] = []                                    # read, and empty: a measurement
    tp._refresh()
    healed = _served(tp)
    assert healed["jobs"]["total"] == 0 and healed["refresh"]["current"] is True
    assert healed["refresh"]["failing_since"] is None


def test_the_proof_never_built_serves_no_counts(proof):
    tp, listing = proof
    tp._refresh()
    served = _served(tp)
    assert "jobs" not in served and "slashes" not in served
    assert served["refresh"]["current"] is False and served["refresh"]["snapshot_generated_at"] is None


def test_the_proof_retries_a_failing_read_at_the_refresh_pace(proof, monkeypatch):
    tp, listing = proof
    monkeypatch.setattr(tp, "REFRESH_S", 3600.0)
    tp._refresh()
    tp._refresh()
    assert listing["calls"] == 1, "a failed attempt must not be retried on every request"


def test_a_kept_snapshot_never_keeps_saying_the_chain_is_live(proof, monkeypatch):
    # A DATED block: with `head` answering no timestamp, `stale` is null in every phase and a frozen
    # "live" verdict cannot be produced, let alone seen.
    tp, listing = proof
    block = int(time.time()) - 5
    monkeypatch.setattr(tp.dc, "head", lambda: (100, block))
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic", "fee": 1, "slashes": []}]
    tp._refresh()
    built = _served(tp)["chain"]
    assert built["stale"] is False and built["last_block_age_s"] is not None

    listing["next"] = None                                  # the node goes down
    tp._refresh()
    kept = _served(tp)
    assert kept["refresh"]["current"] is False
    assert kept["chain"]["stale"] is None, "a kept snapshot still answers 'new blocks keep arriving'"
    assert kept["chain"]["last_block_age_s"] is None, "a kept snapshot still serves the age of its build"
    assert kept["chain"]["height"] == 100 and kept["chain"]["last_block_epoch"] == block
    assert kept["jobs"]["total"] == 1


def test_every_field_computed_against_the_clock_is_withdrawn_from_a_kept_snapshot():
    # DERIVED, not listed: two builds that differ only by `now`. A field that moves with the clock
    # alone is a verdict about now, and a kept snapshot must not carry it. A field added later that
    # depends on `now` without being listed in NOW_VERDICTS turns this red.
    tp = importlib.import_module("the_proof")
    args = dict(jobs=[{"id": "j1", "state": "open+paid+optimistic+disputed", "fee": 3, "slashes": []}],
                pools={"treasury": 1}, seed_health={"contributors": 2}, height=100, block_epoch=1000)
    early = tp.build_proof(**args, now=1005)
    late = tp.build_proof(**args, now=1000 + tp.STALE_BLOCK_S + 5)
    e, late_leaves = dict(_leaves(early)), dict(_leaves(late))
    assert set(e) == set(late_leaves)
    moved = {p for p in e if e[p] != late_leaves[p]} - {("generated_at",)}
    assert moved == set(tp.NOW_VERDICTS), f"fields moved by the clock alone: {sorted(moved)}"

    kept = dict(_leaves(tp.kept_payload(early)))
    assert all(kept[p] is None for p in tp.NOW_VERDICTS)
    assert {p: v for p, v in kept.items() if p not in tp.NOW_VERDICTS} == \
           {p: v for p, v in e.items() if p not in tp.NOW_VERDICTS}, "a fact of the snapshot was dropped"
    assert early["chain"]["stale"] is False, "kept_payload modified the snapshot it was given"


def test_a_request_during_the_first_refresh_is_served_a_marked_payload(proof, monkeypatch):
    tp, listing = proof
    entered, release = threading.Event(), threading.Event()

    def slow_listing():
        entered.set()
        release.wait(10)
        return [{"id": "j1", "state": "open", "fee": 1, "slashes": []}]
    monkeypatch.setattr(tp.dc, "list_jobs_full", slow_listing)
    srv = _Server(tp.Handler)
    first = {}
    try:
        t = threading.Thread(target=lambda: first.update(srv.get("/proof")))
        t.start()
        assert entered.wait(5), "the first request never reached the job listing"
        during = srv.get("/proof", timeout=5)
        release.set()
        t.join(10)
    finally:
        release.set()
        srv.close()
    assert "refresh" in during, f"served without any mark during the first walk: {during}"
    assert during["refresh"]["current"] is False and during["refresh"]["reason"] == tp.NOT_YET_BUILT
    assert "jobs" not in during and "chain" not in during
    assert first["refresh"]["current"] is True and first["jobs"]["total"] == 1


# ── the exporter ────────────────────────────────────────────────────────────────────────────────
class _Gauge:
    def __init__(self, *a, **k):
        self.v, self.children = None, {}

    def set(self, v):
        self.v = v

    def labels(self, **kw):
        return self.children.setdefault(tuple(sorted(kw.items())), _Gauge())

    def clear(self):
        self.children.clear()


@pytest.fixture()
def exporter(monkeypatch):
    fake = types.ModuleType("prometheus_client")
    fake.Gauge, fake.start_http_server = _Gauge, (lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "prometheus_client", fake)
    monkeypatch.delitem(sys.modules, "exporter", raising=False)
    ex = importlib.import_module("exporter")
    emitted = 200 * 10 ** 6                                 # above the significance floor
    monkeypatch.setattr(ex.dc, "network_state", lambda: {
        "miners": [{"id": "m1", "operator": "opA", "stake": 1, "balance": 0}], "pools": {}, "height": 5})
    monkeypatch.setattr(ex.dc, "balance", lambda addr, denom="udndr": ex.RESERVE_INIT - emitted)
    monkeypatch.setattr(ex.dc, "committee_seed_health", lambda: None)
    monkeypatch.setattr(ex, "_resolve_excluded", lambda: set())
    launch = []
    monkeypatch.setattr(ex, "_refresh_launch", lambda jobs, mids: launch.append(jobs))
    yield ex, launch
    sys.modules.pop("exporter", None)


def test_the_exporter_keeps_its_r2_gauges_on_an_unread_listing(exporter, monkeypatch, capsys):
    ex, launch = exporter
    monkeypatch.setattr(ex.dc, "list_jobs_full", lambda: [
        {"state": "open+paid+finalized", "client": "ext1", "fee": 300 * 10 ** 6}])
    ex._R2_NEXT = 0.0
    ex.refresh()
    assert ex.g_r2.v == 1.5 and ex.g_r2_stop.v == 0 and len(launch) == 1

    monkeypatch.setattr(ex.dc, "list_jobs_full", lambda: None)
    ex._R2_NEXT = 0.0
    capsys.readouterr()
    ex.refresh()
    assert ex.g_r2.v == 1.5, "R2 recomputed over an unread listing"
    assert ex.g_r2_stop.v == 0, "a node outage raised the R2 stop breach"
    assert len(launch) == 1, "the launch gauges were fed an unread listing"
    # The log names the cause and the consequence: an operator reading it must not have to tell an
    # outage of the node from an error raised further down.
    out = capsys.readouterr().out
    assert "list-job unreadable" in out and "NOT updated" in out and "Error" not in out, out


# ── the points indexer ──────────────────────────────────────────────────────────────────────────
def test_the_points_snapshot_refuses_an_unread_listing(monkeypatch):
    pi = importlib.import_module("points_indexer")
    monkeypatch.setattr(pi.dc, "registered_miners", lambda: [])
    monkeypatch.setattr(pi.dc, "list_jobs_full", lambda: None)
    with pytest.raises(RuntimeError, match="NOT measured"):
        pi.snapshot()
    monkeypatch.setattr(pi.dc, "list_jobs_full", lambda: [])
    assert pi.snapshot()["leaderboard"] == []


@pytest.fixture()
def points(monkeypatch):
    pi = importlib.import_module("points_indexer")
    listing = {"next": None, "calls": 0}

    def list_jobs_full():
        listing["calls"] += 1
        return listing["next"]
    monkeypatch.setattr(pi.dc, "list_jobs_full", list_jobs_full)
    monkeypatch.setattr(pi.dc, "registered_miners", lambda: [])
    monkeypatch.setattr(pi, "_fetch_verdicts", lambda jobs, miners: {})
    monkeypatch.setattr(pi, "_CACHE", pi.new_cache())       # the SHIPPED initial state
    monkeypatch.setattr(pi, "REFRESH_S", 0.0)
    return pi, listing


J_POINTS = {"id": "j1", "state": "open+paid+finalized", "miner_id": "m1", "client": "ext1", "fee": 1000,
            "slashes": []}


def test_the_points_server_walks_the_listing_once_per_refresh_during_an_outage(points, monkeypatch):
    # /points is public: a failed rebuild that leaves the refresh clock where it was makes every
    # request start a full walk of list-job for as long as the node stays down.
    pi, listing = points
    monkeypatch.setattr(pi, "REFRESH_S", 3600.0)
    srv = _Server(pi.Handler)
    try:
        bodies = [srv.get("/points") for _ in range(5)]
    finally:
        srv.close()
    assert listing["calls"] == 1, f"{listing['calls']} walks of list-job for 5 requests"
    for b in bodies:
        assert "leaderboard" not in b, "a leaderboard was served from an unread listing"
        assert b["refresh"]["current"] is False and "NOT measured" in b["refresh"]["reason"]


def test_the_points_server_keeps_its_last_leaderboard_and_says_so(points):
    pi, listing = points
    listing["next"] = [J_POINTS]
    srv = _Server(pi.Handler)
    try:
        first = srv.get("/points")
        listing["next"] = None                              # list-job unreadable
        kept = srv.get("/points")
        listing["next"] = []                                # read, and empty: a measurement
        healed = srv.get("/points")
    finally:
        srv.close()
    assert first["refresh"]["current"] is True and "m1" in [r["id"] for r in first["leaderboard"]]
    assert kept["leaderboard"] == first["leaderboard"], "an unread listing erased the leaderboard"
    assert kept["generated_at"] == first["generated_at"]
    assert kept["refresh"]["current"] is False and kept["refresh"]["failing_since"] is not None
    assert kept["refresh"]["snapshot_generated_at"] == first["generated_at"]
    assert healed["leaderboard"] == [] and healed["refresh"]["current"] is True
    assert healed["refresh"]["failing_since"] is None


def test_a_kept_leaderboard_carries_no_verdict_about_now(points, monkeypatch):
    # `publish` serves a kept leaderboard WHOLE, on the ground that nothing in it but `generated_at`
    # depends on the clock. Derived here, not assumed: two snapshots that differ only by the clock.
    pi, listing = points
    listing["next"] = [J_POINTS]
    monkeypatch.setattr(pi.time, "time", lambda: 1000.0)
    early = pi.snapshot()
    monkeypatch.setattr(pi.time, "time", lambda: 1000.0 + 10 ** 6)
    late = pi.snapshot()
    e, late_leaves = dict(_leaves(early)), dict(_leaves(late))
    assert set(e) == set(late_leaves)
    assert {p for p in e if e[p] != late_leaves[p]} == {("generated_at",)}


def test_the_points_server_marks_what_it_serves_during_its_first_walk(points, monkeypatch):
    pi, listing = points
    entered, release = threading.Event(), threading.Event()

    def slow_listing():
        entered.set()
        release.wait(10)
        return [J_POINTS]
    monkeypatch.setattr(pi.dc, "list_jobs_full", slow_listing)
    srv = _Server(pi.Handler)
    first = {}
    try:
        t = threading.Thread(target=lambda: first.update(srv.get("/points")))
        t.start()
        assert entered.wait(5), "the first request never reached the job listing"
        during = srv.get("/points", timeout=5)
        release.set()
        t.join(10)
    finally:
        release.set()
        srv.close()
    assert "refresh" in during, f"served without any mark during the first walk: {during}"
    assert during["refresh"]["current"] is False and during["refresh"]["reason"] == pi.NOT_YET_BUILT
    assert "leaderboard" not in during
    assert first["refresh"]["current"] is True
