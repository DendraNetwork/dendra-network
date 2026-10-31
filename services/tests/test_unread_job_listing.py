"""Bench of an unread job listing, from the reader to every consumer that publishes it.

Every consumer turns the job listing into counts it publishes. Answered as `[]`, an unread listing
would serve an outage of the node as a network with no job, no audit and no slash -- on the reassuring
side, in a form nothing downstream can tell apart from a real reading. So `list_jobs_full()` answers
None for "not read", and [] stays what it is: a measurement.

Each consumer is driven through its shipped function, with only the chain reads replaced:
  * the reader      -- `list_all` / `list_jobs_full`: `{}` is a read page of zero entries, a failed
                       command is None, and a later page that fails never yields a prefix;
  * The Proof       -- `_refresh` keeps the last snapshot it published and says so (`refresh`), minus
                       its verdicts about now (a kept "the chain is live" outlives the chain);
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


# -- the reader ----------------------------------------------------------------------------------
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


# -- The Proof -----------------------------------------------------------------------------------
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
    # The shipped initial state, never one written here: a bench that fabricates the cache it starts
    # from cannot see what the server serves before its first refresh completes.
    monkeypatch.setattr(tp, "_CACHE", tp.new_cache())
    monkeypatch.setattr(tp, "REFRESH_S", 0.0)
    return tp, listing


@pytest.fixture()
def private_builder(proof, monkeypatch):
    """The module state a started builder relies on, private to one case. `the_proof` keeps its builder thread, its
    chain thread, its refresh lock and its stop flag as module globals, and the other cases of the session (on the same
    worker, in any order) start threads and leave the lock and the flag in the state they found useful: a builder that
    is still alive makes `start_builder` return it without starting ours, and a lock still held makes `_refresh` skip.
    Every global is replaced by a fresh one, the readers that would leave the process (the node's RPC, the season
    service, the registry, the commits) answer "unread" at once, and the threads this case starts are stopped and
    joined before the originals come back."""
    tp, listing = proof
    monkeypatch.setattr(tp, "_BUILDER", None)
    monkeypatch.setattr(tp, "_CHAIN_READER", None)
    monkeypatch.setattr(tp, "_REFRESH_LOCK", threading.Lock())
    monkeypatch.setattr(tp, "_STOP", threading.Event())
    monkeypatch.setattr(tp, "_CHAIN_FAILING", [False])
    monkeypatch.setattr(tp.dc, "rpc_json", lambda *a, **k: None)
    monkeypatch.setattr(tp, "_rpc_get", lambda *a, **k: None)
    monkeypatch.setattr(tp, "_read_season_generator", lambda: None)
    monkeypatch.setattr(tp, "_read_miners", lambda: [])
    monkeypatch.setattr(tp, "_read_commits", lambda: [])
    monkeypatch.setattr(tp, "_read_audit_timing", lambda jobs, height: None)
    yield tp, listing
    tp._STOP.set()
    for th in (tp._BUILDER, tp._CHAIN_READER):
        if th is not None:
            th.join(10)


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
    # The pace lives in the builder loop, not in the request: a failing read is attempted once per gap.
    tp, listing = proof
    monkeypatch.setattr(tp, "REFRESH_S", 3600.0)
    waits = []
    builds = iter(range(3))

    def sleep(s):
        waits.append(s)
    tp.build_loop(tp._CACHE, tp._refresh, tp.REFRESH_S, sleep, lambda: next(builds, None) is None)
    assert listing["calls"] == 3 and waits == [3600.0] * 3, "a failed attempt was retried before the gap"


def test_the_proof_reads_again_when_the_chain_moves_and_never_publishes_false(proof, monkeypatch):
    # An audit that opens between the counter read and the listing makes them disagree for one refresh
    # with no state hiding. The read is made once more; a second move is published as null with its
    # reason, never as partition_ok false, which the explorer shows as a broken partition.
    tp, listing = proof
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic+disputed", "fee": 1, "slashes": []}]
    opened = iter([0, 1, 1, 1])
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: {"job_ids": [], "audits_opened": next(opened)})
    tp._refresh()
    jobs = _served(tp)["jobs"]
    assert listing["calls"] == 2, "a read that straddles a move is made once more"
    assert jobs["partition_ok"] is True and jobs["audits_opened"] == 1

    climbing = iter(range(10, 20))
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: {"job_ids": [], "audits_opened": next(climbing)})
    tp._refresh()
    jobs = _served(tp)["jobs"]
    assert listing["calls"] == 4, "two attempts, never more"
    assert jobs["partition_ok"] is None and jobs["partition_bounds_ok"] is None
    assert jobs["partition_unmeasured_reason"] == "the chain moved during the read"


def test_the_proof_reads_the_counter_before_the_listing(proof, monkeypatch):
    # An audit opens while the listing is walked, after the walk passed its job. Read before the
    # listing, the counter shows the move and the walk is made again; read after it, the counter and
    # the listing would disagree with no move to explain it, and the feed would publish a false.
    tp, listing = proof
    chain = {"opened": 0, "walks": 0}

    def walk():
        chain["walks"] += 1
        state = "open+paid+optimistic" if chain["walks"] == 1 else "open+paid+optimistic+disputed"
        chain["opened"] = 1
        return [{"id": "j1", "state": state, "fee": 1, "slashes": []}]
    monkeypatch.setattr(tp.dc, "list_jobs_full", walk)
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: {"job_ids": [], "audits_opened": chain["opened"]})
    tp._refresh()
    jobs = _served(tp)["jobs"]
    assert chain["walks"] == 2 and jobs["partition_ok"] is True and jobs["audits_opened"] == 1


def test_the_proof_bounds_the_event_search_by_the_heights_around_the_listing(proof, monkeypatch):
    # The index check stops at the height read before the listing and the event search at the one read
    # after it; taking the top for both, or the node's latest, counts unwinds the listing cannot show.
    tp, listing = proof
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic", "fee": 1, "slashes": []}]
    heights = iter([100, 105])
    monkeypatch.setattr(tp.dc, "head", lambda: (next(heights), None))
    seen = []
    monkeypatch.setattr(tp, "read_unwind_origin", lambda jobs, rpc, heights=None, cache=None: seen.append(heights))
    tp._refresh()
    assert seen == [(100, 105)]


def test_the_proof_never_folds_an_unread_first_counter_into_no_move(proof, monkeypatch):
    tp, listing = proof
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic+disputed", "fee": 1, "slashes": []}]
    answers = iter([None, {"job_ids": [], "audits_opened": 1}])
    monkeypatch.setattr(tp.dc, "audit_deferred", lambda: next(answers))
    tp._refresh()
    jobs = _served(tp)["jobs"]
    assert listing["calls"] == 1
    assert jobs["partition_ok"] is None and jobs["partition_bounds_ok"] is None
    assert jobs["partition_unmeasured_reason"] == "counter unread before the listing"


def test_a_kept_snapshot_never_keeps_saying_the_chain_is_live(proof, monkeypatch):
    # A dated block: with `head` answering no timestamp, `stale` is null in every phase and a frozen
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
    # Derived, not listed: two builds that differ only by `now`. A field that moves with the clock
    # alone is a verdict about now, and a kept snapshot must not carry it. A field added later that
    # depends on `now` without being listed in NOW_VERDICTS turns this red. With no `read_at`, the
    # status counts as read at `now`; the two dates, of the document and of the reading, are facts.
    tp = importlib.import_module("the_proof")
    args = dict(jobs=[{"id": "j1", "state": "open+paid+optimistic+disputed", "fee": 3, "slashes": []}],
                pools={"treasury": 1}, seed_health={"contributors": 2}, height=100, block_epoch=1000)
    early = tp.build_proof(**args, now=1005)
    late = tp.build_proof(**args, now=1000 + tp.STALE_BLOCK_S + 5)
    e, late_leaves = dict(_leaves(early)), dict(_leaves(late))
    assert set(e) == set(late_leaves)
    moved = {p for p in e if e[p] != late_leaves[p]} - {("generated_at",), ("chain", "read_at")}
    assert moved == set(tp.NOW_VERDICTS), f"fields moved by the clock alone: {sorted(moved)}"

    kept = dict(_leaves(tp.kept_payload(early)))
    assert all(kept[p] is None for p in tp.NOW_VERDICTS)
    assert {p: v for p, v in kept.items() if p not in tp.NOW_VERDICTS} == \
           {p: v for p, v in e.items() if p not in tp.NOW_VERDICTS}, "a fact of the snapshot was dropped"
    assert early["chain"]["stale"] is False, "kept_payload modified the snapshot it was given"


def test_the_proof_dates_the_block_age_at_the_status_read_not_at_the_end_of_a_slow_build(proof, monkeypatch):
    # The reads after the status (commits, audit timing) take a minute on the live network. Measured at
    # the end of the build, that minute was published as the age of a chain making a block every 5 s.
    tp, listing = proof
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(tp.time, "time", lambda: clock["t"])
    monkeypatch.setattr(tp.time, "monotonic", lambda: clock["t"])
    read_t = int(clock["t"])
    monkeypatch.setattr(tp.dc, "head", lambda: (100, read_t - 2))

    def slow_held_summary():                                # a read made after the status
        clock["t"] += 60
        return None
    monkeypatch.setattr(tp.dc, "held_summary", slow_held_summary)
    listing["next"] = [{"id": "j1", "state": "open+paid+optimistic", "fee": 1, "slashes": []}]
    tp._refresh()
    served = _served(tp)
    assert served["chain"]["last_block_age_s"] == 2, "the build's own minute was added to the age"
    assert served["chain"]["stale"] is False
    assert served["chain"]["read_at"] == read_t and served["generated_at"] == read_t + 60
    assert served["refresh"]["build_s"] == 60.0

    listing["next"] = None                                  # the node goes down
    tp._refresh()
    kept = _served(tp)
    assert kept["chain"]["stale"] is None and kept["chain"]["last_block_age_s"] is None
    assert kept["chain"]["read_at"] == read_t, "the date of the kept reading was dropped"


def test_a_request_during_the_first_refresh_is_served_a_marked_payload(private_builder, monkeypatch):
    tp, listing = private_builder
    entered, release = threading.Event(), threading.Event()

    def slow_listing():
        entered.set()
        release.wait(10)
        return [{"id": "j1", "state": "open", "fee": 1, "slashes": []}]
    monkeypatch.setattr(tp.dc, "list_jobs_full", slow_listing)
    srv = _Server(tp.Handler)
    builder = None
    try:
        builder = tp.start_builder()
        assert entered.wait(5), "the builder never reached the job listing"
        during = srv.get("/proof", timeout=5)
        release.set()
        deadline = time.time() + 10
        after = srv.get("/proof")
        while after["refresh"]["current"] is not True and time.time() < deadline:
            time.sleep(0.05)
            after = srv.get("/proof")
    finally:
        release.set()
        tp._STOP.set()
        if builder is not None:
            builder.join(10)
        srv.close()
    assert "refresh" in during, f"served without any mark during the first walk: {during}"
    assert during["refresh"]["current"] is False and during["refresh"]["reason"] == tp.NOT_YET_BUILT
    assert "jobs" not in during and "chain" not in during
    assert after["refresh"]["current"] is True and after["jobs"]["total"] == 1


# -- the exporter --------------------------------------------------------------------------------
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
    assert "list-job unreadable" in out and "not updated" in out and "Error" not in out, out


# -- the points indexer --------------------------------------------------------------------------
def test_the_points_snapshot_refuses_an_unread_listing(monkeypatch):
    pi = importlib.import_module("points_indexer")
    monkeypatch.setattr(pi.dc, "registered_miners", lambda balances=True: [])
    monkeypatch.setattr(pi.dc, "list_jobs_full", lambda: None)
    with pytest.raises(RuntimeError, match="not measured"):
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
    monkeypatch.setattr(pi.dc, "registered_miners", lambda balances=True: [])
    monkeypatch.setattr(pi, "_fetch_verdicts", lambda jobs, miners: {})
    monkeypatch.setattr(pi, "_CACHE", pi.new_cache())       # the shipped initial state
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
        assert b["refresh"]["current"] is False and "not measured" in b["refresh"]["reason"]


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
    # `publish` serves a kept leaderboard whole, on the ground that nothing in it but `generated_at`
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
