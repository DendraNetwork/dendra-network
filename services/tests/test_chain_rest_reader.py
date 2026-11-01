"""Bench of the in-process chain reader of client: REST on 1317, CometBFT RPC on 26657.

Every `dendrad query` is a process (about 0.35 CPU-s and 120 MB resident); on the host that also carries
the validator these reads were a large share of the load. The shipped readers now ask the node over a
kept-alive HTTP connection and keep the CLI for transactions and for the fallback switch
(DENDRA_CHAIN_READ=cli).

What this bench holds, with the shipped functions and a fabricated node on 127.0.0.1 (stdlib http.server):
  * the page walk: `pagination.limit=500`, the continuation key sent back quoted (base64 holds + / =),
    two pages joined, a failed later page is None and never a prefix, an empty collection is [];
  * the zero rule: a field absent from a row is the zero of its type, a failed read is None;
  * what the gateway writes differently from the CLI (zero fields, a null next_key) changes no verdict;
  * the connection is kept, and one the server dropped while idle is retried once;
  * `registered_miners(balances=False)` asks the bank nothing; `submit_job` passes it;
  * the params are read once in 60 s and a failed read is never kept;
  * the CLI switch routes every read back to `subprocess`, and no REST request is made then; an empty
    switch (compose passes it empty when the host leaves it unset) is the default, REST;
  * an error status with a JSON body (400, 404) is an unread page, on the first page and on a later one;
  * the balance, the anchored key, the head and the params are asked with the CLI's 60 s, not the reader's 20 s;
  * a switch that is not empty, `rest` or `cli` is refused, and a fresh import of the client fails on it;
  * an answer past the size cap is not retried; a JSON answer that is not an object says so on stderr;
  * the points snapshot asks for the registry without balances; only a read with balances is kept as the
    last-good rows.
"""
import calendar
import importlib
import json
import os
import socket
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
sys.path.insert(0, MODEA or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402

J1 = {"job_id": "j1", "state": "open+paid+optimistic", "miner_id": "m1", "client": "c1", "fee": "100"}
J2 = {"job_id": "j2", "state": "open", "miner_id": "m2", "client": "c2", "fee": "50"}
KEY = "QUJD+/=="            # a continuation key as the SDK writes it: base64, with the three characters a URL changes


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        st = self.server.state
        u = urlsplit(self.path)
        qs = parse_qs(u.query)
        st.connections.add(self.client_address[1])
        st.requests.append((u.path, qs))
        status, doc = st.respond(u.path, qs)
        body = json.dumps(doc).encode() if not isinstance(doc, bytes) else doc
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if st.drop_after > 0:        # closes the socket without telling the client: an idle drop
            st.drop_after -= 1
            self.close_connection = True

    def log_message(self, *a):
        pass


@pytest.fixture()
def node(monkeypatch):
    """A fabricated node. `respond(path, qs)` is replaced per case; REST and RPC share the one port."""
    st = types.SimpleNamespace(requests=[], connections=set(), drop_after=0, spawned=[], routes={})

    def respond(path, qs):
        r = st.routes.get(path)
        if r is None:
            return 501, {"code": 12, "message": "Not Implemented", "details": []}
        return r(qs) if callable(r) else r
    st.respond = respond
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.state = st
    threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    st.base = base
    monkeypatch.delenv("DENDRA_CHAIN_READ", raising=False)
    monkeypatch.setenv("DENDRA_REST_URL", base)
    monkeypatch.setenv("DENDRA_RPC_URL", base)

    def no_cli(cmd, **kw):
        st.spawned.append(list(cmd))
        raise OSError("dendrad is not available in this bench")
    monkeypatch.setattr(dc.subprocess, "run", no_cli)
    dc.reset_chain_read_state()
    yield st
    dc.reset_chain_read_state()
    srv.shutdown()
    srv.server_close()


def _jobs_two_pages(st):
    def job(qs):
        if qs.get("pagination.key") == [KEY]:
            return 200, {"job": [J2], "pagination": {"next_key": None, "total": "0"}}
        return 200, {"job": [J1], "pagination": {"next_key": KEY, "total": "0"}}
    st.routes["/dendra/jobs/v1/job"] = job


# -- the page walk -------------------------------------------------------------------------------------
def test_two_pages_are_joined_with_the_limit_and_the_quoted_key(node):
    _jobs_two_pages(node)
    rows = dc.list_all("list-job", "job")
    assert [r["job_id"] for r in rows] == ["j1", "j2"]
    (p1, q1), (p2, q2) = node.requests
    assert p1 == p2 == "/dendra/jobs/v1/job"
    assert q1["pagination.limit"] == ["500"] and "pagination.key" not in q1
    assert q2["pagination.limit"] == ["500"]
    assert q2["pagination.key"] == [KEY]       # '+' would arrive as a space if the key were not quoted
    assert node.spawned == []                  # no dendrad process


def test_the_listing_reader_runs_on_the_same_pages(node):
    _jobs_two_pages(node)
    rows = dc.list_jobs_full()
    assert [(r["id"], r["miner_id"], r["fee"]) for r in rows] == [("j1", "m1", 100), ("j2", "m2", 50)]
    assert node.spawned == []


def test_a_failed_second_page_is_none_never_the_first_page(node):
    def job(qs):
        if qs.get("pagination.key") == [KEY]:
            return 500, {"code": 13, "message": "internal", "details": []}
        return 200, {"job": [J1], "pagination": {"next_key": KEY}}
    node.routes["/dendra/jobs/v1/job"] = job
    assert dc.list_all("list-job", "job") is None
    assert dc.list_jobs_full() is None


@pytest.mark.parametrize("status", [400, 404])
@pytest.mark.parametrize("page", ["first", "second"])
def test_an_error_status_with_a_json_body_is_none_on_any_page(node, status, page):
    err = (status, {"code": 3, "message": "invalid request", "details": []})

    def job(qs):
        if page == "first":
            return err
        if qs.get("pagination.key") == [KEY]:
            return err
        return 200, {"job": [J1], "pagination": {"next_key": KEY}}
    node.routes["/dendra/jobs/v1/job"] = job
    assert dc.list_all("list-job", "job") is None
    assert dc.list_jobs_full() is None


@pytest.mark.parametrize("answer", [
    (500, {"code": 13, "message": "internal"}),     # the gateway's error status
    (501, {"code": 12, "message": "Not Implemented"}),
    (200, b"not json"),                              # printed no JSON
    (200, b"[]"),                                    # JSON, not an object
    (200, {"job": {"job_id": "j1"}}),                # the list is not a list
    (200, {"job": [], "pagination": "next"}),        # the continuation is not an object
    (200, {"job": [J1], "pagination": {"next_key": 7}}),   # the key is not text
])
def test_an_unread_page_is_none_never_an_empty_list(node, answer):
    node.routes["/dendra/jobs/v1/job"] = answer
    assert dc.list_all("list-job", "job") is None
    assert dc.list_jobs_full() is None


def test_a_refused_connection_is_none(node, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                        # nothing listens there now
    monkeypatch.setenv("DENDRA_REST_URL", "http://127.0.0.1:%d" % port)
    assert dc.list_all("list-job", "job") is None
    assert dc.list_all("list-miner", "miner") is None
    assert dc.registered_miners(balances=False) is None


@pytest.mark.parametrize("doc", [
    {"job": [], "pagination": {"next_key": None, "total": "0"}},   # what the gateway writes for nothing
    {"pagination": {"total": "0"}},
    {},                                                              # what the CLI prints for nothing
])
def test_an_empty_collection_is_an_empty_list(node, doc):
    node.routes["/dendra/jobs/v1/job"] = (200, doc)
    assert dc.list_all("list-job", "job") == []
    assert dc.list_jobs_full() == []


def test_the_walk_that_never_ends_is_refused(node):
    node.routes["/dendra/jobs/v1/job"] = (200, {"job": [J1], "pagination": {"next_key": "again"}})
    assert dc.list_all("list-job", "job", max_pages=3) is None
    assert len(node.requests) == 3


@pytest.mark.parametrize("sub,path,field", [("list-commit", "commit", "commit"), ("list-miner", "miner", "miner"),
                                            ("list-job", "job", "job")])
def test_each_collection_has_its_own_route(node, sub, path, field):
    node.routes["/dendra/jobs/v1/" + path] = (200, {field: [{"x": "1"}]})
    assert dc.list_all(sub, field) == [{"x": "1"}]


# -- the zero rule -------------------------------------------------------------------------------------
def test_a_field_absent_from_a_row_is_the_zero_of_its_type(node):
    node.routes["/dendra/jobs/v1/job"] = (200, {"job": [{"job_id": "j9", "state": "open"}]})
    j = dc.list_jobs_full()[0]
    assert (j["id"], j["fee"], j["audit_voters"], j["slashes"], j["miner_id"], j["client"]) == \
        ("j9", 0, 0, [], "", "")
    node.routes["/dendra/jobs/v1/miner"] = (200, {"miner": [{"miner_id": "dm1a", "operator": "dendra1opa"}]})
    m = dc.registered_miners(balances=False)[0]
    assert (m["stake"], m["demand"], m["enc_pubkey"]) == (0, 0, "")


def test_zero_fields_the_gateway_writes_read_as_the_rows_without_them(node):
    explicit = {"miner_id": "dm1a", "operator": "dendra1opa", "stake": "0", "demand": "0", "enc_pubkey": ""}
    node.routes["/dendra/jobs/v1/miner"] = (200, {"miner": [explicit], "pagination": {"next_key": None}})
    with_zeros = dc.registered_miners(balances=False)
    node.routes["/dendra/jobs/v1/miner"] = (200, {"miner": [{"miner_id": "dm1a", "operator": "dendra1opa"}]})
    assert dc.registered_miners(balances=False) == with_zeros


def test_a_row_that_is_not_an_object_makes_the_registry_unread(node):
    node.routes["/dendra/jobs/v1/miner"] = (200, {"miner": [{"miner_id": "dm1a", "operator": "o"}, "dm1b"]})
    assert dc.registered_miners(balances=False) is None


# -- the connection --------------------------------------------------------------------------------------
def test_the_connection_is_kept_between_reads(node):
    _jobs_two_pages(node)
    node.routes["/dendra/jobs/v1/params"] = (200, {"params": {"verification_mode": "1"}})
    dc.list_all("list-job", "job")
    dc.list_all("list-job", "job")
    dc.jobs_params()
    assert len(node.requests) == 5 and len(node.connections) == 1


def test_a_connection_dropped_while_idle_is_retried_once(node):
    node.routes["/dendra/jobs/v1/job"] = (200, {"job": [J1]})
    node.drop_after = 1                    # the server closes the socket after this answer, silently
    assert [r["job_id"] for r in dc.list_all("list-job", "job")] == ["j1"]
    assert [r["job_id"] for r in dc.list_all("list-job", "job")] == ["j1"]
    assert len(node.requests) == 2 and len(node.connections) == 2


def test_an_answer_past_the_size_cap_is_not_retried(node, monkeypatch):
    monkeypatch.setattr(dc, "_MAX_BODY", 200)
    node.routes["/small"] = (200, {"a": 1})
    node.routes["/big"] = (200, {"pad": "x" * 500})
    assert dc.rest_json("/small") == {"a": 1}          # leaves a kept connection, the case a retry applies to
    assert dc.rest_json("/big") is None
    assert [p for p, _q in node.requests] == ["/small", "/big"]


def test_a_json_answer_that_is_not_an_object_says_so_on_stderr(node, capsys):
    node.routes["/dendra/jobs/v1/params"] = (200, [1, 2])
    assert dc.rest_json("/dendra/jobs/v1/params") is None
    assert "not an object" in capsys.readouterr().err


def test_each_thread_has_its_own_connection(node):
    node.routes["/dendra/jobs/v1/job"] = (200, {"job": [J1]})
    seen = []
    t = threading.Thread(target=lambda: seen.append(dc.list_all("list-job", "job")))
    dc.list_all("list-job", "job")
    t.start()
    t.join()
    assert seen == [[J1]] and len(node.connections) == 2


# -- the registry and its balances -----------------------------------------------------------------------
MINERS = [{"miner_id": "dm1a", "operator": "dendra1opa", "stake": "1000", "enc_pubkey": "aa" * 32},
          {"miner_id": "dm1b", "operator": "dendra1opa", "stake": "2000", "demand": "4", "enc_pubkey": "bb" * 32},
          {"miner_id": "dm1c", "operator": "dendra1opc", "stake": "3000", "enc_pubkey": "cc" * 32}]


def _registry(st):
    st.routes["/dendra/jobs/v1/miner"] = (200, {"miner": MINERS})
    st.routes[dc._BANK_BALANCES + "dendra1opa"] = (200, {"balances": [{"denom": "udndr", "amount": "7"}]})
    st.routes[dc._BANK_BALANCES + "dendra1opc"] = (200, {"balances": []})        # proto3 omits an empty account


def test_without_balances_the_bank_is_never_asked(node):
    _registry(node)
    got = dc.registered_miners(balances=False)
    assert [(m["id"], m["stake"], m["enc_pubkey"][:2]) for m in got] == \
        [("dm1a", 1000, "aa"), ("dm1b", 2000, "bb"), ("dm1c", 3000, "cc")]
    assert all(m["balance"] is None for m in got)        # not measured, never 0
    assert [p for p, _q in node.requests] == ["/dendra/jobs/v1/miner"]


def test_with_balances_each_operator_is_asked_once(node):
    _registry(node)
    got = dc.registered_miners()
    assert [m["balance"] for m in got] == [7, 7, 0]
    assert sorted(p for p, _q in node.requests if "bank" in p) == \
        [dc._BANK_BALANCES + "dendra1opa", dc._BANK_BALANCES + "dendra1opc"]


def test_an_operator_the_bank_cannot_answer_keeps_its_row_with_none(node):
    _registry(node)
    del node.routes[dc._BANK_BALANCES + "dendra1opc"]    # the gateway answers 501
    got = dc.registered_miners()
    assert [m["balance"] for m in got] == [7, 7, None] and len(got) == 3


def test_the_balance_the_anchored_key_the_head_and_the_params_wait_as_long_as_the_cli_did(monkeypatch):
    asked = []
    monkeypatch.delenv("DENDRA_CHAIN_READ", raising=False)
    monkeypatch.setattr(dc, "_http_json", lambda base, target, timeout: asked.append(timeout) or {})
    monkeypatch.setitem(dc._PARAMS, "p", None)             # a cached reading would answer without asking
    dc.balance("dendra1opa")
    dc.balance_read("dendra1opa")
    dc.anchored_enc_pubkey("dm1a")
    dc.head()
    dc.jobs_params()
    assert asked == [60, 60, 60, 60, 60]


def test_a_read_without_balances_does_not_replace_the_last_rows_with_balances(node, monkeypatch):
    monkeypatch.setattr(dc, "_MINERS_LAST", {"rows": None, "at": None})
    _registry(node)
    assert dc.registered_miners(balances=False) is not None
    assert dc.last_registered_miners() == (None, None)         # nothing with balances was read yet
    dc.registered_miners()
    assert dc.registered_miners(balances=False) is not None
    last, _age = dc.last_registered_miners()
    assert [m["balance"] for m in last] == [7, 7, 0]


def test_the_points_snapshot_asks_for_the_registry_without_balances(node, monkeypatch):
    _registry(node)
    pi = importlib.import_module("points_indexer")
    monkeypatch.setattr(pi.dc, "list_jobs_full", lambda: [])
    pi.snapshot()
    assert [p for p, _q in node.requests] == ["/dendra/jobs/v1/miner"]      # no bank query


def test_the_operator_is_quoted_into_one_path_segment(node):
    dc.balance_read("a/b?x=1")
    assert node.requests[-1][0] == dc._BANK_BALANCES + "a%2Fb%3Fx%3D1" and node.requests[-1][1] == {}


def test_the_emission_balance_distinguishes_empty_from_failed_read(node):
    node.routes[dc._BANK_BALANCES + "dendra1em"] = (200, {"balances": [{"denom": "udndr", "amount": "90"}]})
    assert dc.balance("dendra1em") == 90
    node.routes[dc._BANK_BALANCES + "dendra1empty"] = (200, {})
    assert dc.balance("dendra1empty") == 0
    assert dc.balance("dendra1nobody") is None


def test_submit_job_asks_for_the_registry_without_balances(monkeypatch):
    asked = []
    monkeypatch.setattr(dc, "tx_from", lambda *a, **k: "")
    monkeypatch.setattr(dc, "wait_tx_reason", lambda o, timeout=24: (True, ""))
    monkeypatch.setattr(dc, "assigned_committee", lambda jid, k: ["dm1a"])
    monkeypatch.setattr(dc, "registered_miners", lambda **kw: asked.append(kw))   # the registry is unreadable
    monkeypatch.setattr(dc, "anchored_enc_pubkey", lambda mid: None)
    out = dc.submit_job("hi", 10, "http://relay.invalid", k=1, jid="jobx1")
    assert "error" in out and asked == [{"balances": False}]


def test_a_miner_record_is_read_over_rest(node):
    node.routes["/dendra/jobs/v1/miner/dm1a"] = (200, {"miner": {"miner_id": "dm1a", "enc_pubkey": "aa" * 32}})
    node.routes["/dendra/jobs/v1/miner/dm1x"] = (200, {"miner": {"miner_id": "dm1other"}})
    assert dc.anchored_enc_pubkey("dm1a") == "aa" * 32
    assert dc.anchored_enc_pubkey("dm1x") is None        # a record that names another miner
    assert dc.anchored_enc_pubkey("dm1missing") is None  # the gateway answers an error


# -- the params, cached ------------------------------------------------------------------------------------
def _count(st, path):
    return sum(1 for p, _q in st.requests if p == path)


def test_the_params_are_read_once_in_sixty_seconds(node):
    node.routes["/dendra/jobs/v1/params"] = (200, {"params": {"verification_mode": "1"}})
    assert dc._verification_mode() == 1 and dc._submit_committee_k() == 1 and dc._verification_mode() == 1
    assert _count(node, "/dendra/jobs/v1/params") == 1
    dc._PARAMS["at"] -= dc.PARAMS_TTL_S + 1              # the cached reading is older than the window
    node.routes["/dendra/jobs/v1/params"] = (200, {"params": {}})
    assert dc._verification_mode() == 0                  # proto3 omits a zero: read, and zero
    assert _count(node, "/dendra/jobs/v1/params") == 2


def test_a_failed_params_read_is_never_kept(node):
    node.routes["/dendra/jobs/v1/params"] = (500, {"code": 13})
    assert dc._verification_mode() == 0 and dc.prune_window_blocks() is None
    assert dc._PARAMS["p"] is None
    node.routes["/dendra/jobs/v1/params"] = (200, {"params": {"verification_mode": "1", "miner_prune_blocks": "99"}})
    assert dc._verification_mode() == 1 and dc.prune_window_blocks() == 99
    assert _count(node, "/dendra/jobs/v1/params") == 3


def test_the_prune_window_falls_back_to_the_compiled_constant_when_omitted(node):
    node.routes["/dendra/jobs/v1/params"] = (200, {"params": {}})
    assert dc.prune_window_blocks() == 604800


# -- status and the other documents --------------------------------------------------------------------------
def test_head_pairs_the_height_with_the_time_of_the_same_document(node):
    node.routes["/status"] = (200, {"jsonrpc": "2.0", "id": -1, "result": {"sync_info": {
        "latest_block_height": "42", "latest_block_time": "2026-10-18T10:00:05.123456789Z",
        "catching_up": False}}})
    assert dc.head() == (42, calendar.timegm((2026, 10, 18, 10, 0, 5, 0, 0, 0)))
    assert dc.height() == 42
    assert node.spawned == []


def test_an_unread_status_is_height_zero_and_no_time(node):
    node.routes["/status"] = (500, {"error": "x"})
    assert dc.head() == (0, None)
    node.routes["/status"] = (200, {"jsonrpc": "2.0", "id": -1})          # no result
    assert dc.head() == (0, None)


def test_seed_health_reads_the_gateways_zeros_as_the_cli_omission(node):
    node.routes["/dendra/jobs/v1/committee_seed_health"] = (200, {
        "committee_seed_source": "1", "committee_min_vrf_contributors": "1", "latest_contributors": "0",
        "contributor_power_bps": "0", "has_recent_seed": False, "latest_seed_height": "0",
        "current_height": "121"})
    d = dc.committee_seed_health()
    assert d["seed_height"] is None and d["has_seed"] is False and d["height"] == 121 and d["source"] == 1
    node.routes["/dendra/jobs/v1/committee_seed_health"] = (200, {
        "latest_contributors": "2", "contributor_power_bps": "5000", "has_recent_seed": True,
        "latest_seed_height": "120", "current_height": "121"})
    d = dc.committee_seed_health()
    assert (d["contributors"], d["power_bps"], d["has_seed"], d["seed_height"]) == (2, 5000, True, 120)
    node.routes["/dendra/jobs/v1/committee_seed_health"] = (200, {})
    d = dc.committee_seed_health()
    assert d is not None and d["contributors"] == 0 and d["seed_height"] is None      # {} is a reading
    node.routes["/dendra/jobs/v1/committee_seed_health"] = (501, {"code": 12})
    assert dc.committee_seed_health() is None


def test_audit_deferred_and_held_summary_distinguish_zero_from_unread(node):
    node.routes["/dendra/jobs/v1/audit_deferred"] = (200, {"job_ids": [], "audits_opened": "0"})
    assert dc.audit_deferred() == {"job_ids": [], "audits_opened": 0}
    node.routes["/dendra/jobs/v1/audit_deferred"] = (200, {"job_ids": ["j1"], "audits_opened": "3"})
    assert dc.audit_deferred() == {"job_ids": ["j1"], "audits_opened": 3}
    node.routes["/dendra/jobs/v1/audit_deferred"] = (200, {})
    assert dc.audit_deferred() == {"job_ids": [], "audits_opened": 0}
    node.routes["/dendra/jobs/v1/audit_deferred"] = (501, {"code": 12})
    assert dc.audit_deferred() is None

    node.routes["/dendra/jobs/v1/held_summary"] = (200, {"pockets_measured": "3", "held_total": "0",
                                                           "held_count": "0", "oldest_job_id": "",
                                                           "jurors_locked_count": "0"})
    h = dc.held_summary()
    assert h["pockets_measured"] == 3 and h["held_total"] == 0 and h["jurors_locked_count"] == 0
    node.routes["/dendra/jobs/v1/held_summary"] = (200, {})                # an older binary: pockets are unmeasured
    h = dc.held_summary()
    assert h["pockets_measured"] == 0 and h["bond_escrowed_total"] is None and h["oldest_lock_job_id"] is None
    node.routes["/dendra/jobs/v1/held_summary"] = (500, {})
    assert dc.held_summary() is None


def test_pools_omit_the_zero_pools_as_the_cli_does(node):
    node.routes["/dendra/jobs/v1/pools"] = (200, {"pools": {"miner_paid": "10", "validators": "0", "team": "0",
                                                              "treasury": "5", "creator": "x", "miner_locked": "0",
                                                              "training_paid": "0"}})
    assert dc.pools() == {"minerpaid": 10, "treasury": 5}
    node.routes["/dendra/jobs/v1/pools"] = (500, {})
    assert dc.pools() == {}


# -- where the node is -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("node_arg,rest,rpc", [
    ("tcp://chain:26657", "http://chain:1317", "http://chain:26657"),
    ("", "http://127.0.0.1:1317", "http://127.0.0.1:26657"),
    ("tcp://node-b:36657", "http://node-b:1317", "http://node-b:36657"),
    ("http://node.example:26657", "http://node.example:1317", "http://node.example:26657"),
    ("tcp://[::1]:26657", "http://[::1]:1317", "http://[::1]:26657"),
    ("chain:26657", "http://chain:1317", "http://chain:26657"),
    ("tcp://chain", "http://chain:1317", "http://chain:26657"),
])
def test_the_endpoints_are_derived_from_the_node_address(monkeypatch, node_arg, rest, rpc):
    monkeypatch.delenv("DENDRA_REST_URL", raising=False)
    monkeypatch.delenv("DENDRA_RPC_URL", raising=False)
    monkeypatch.setattr(dc, "NODE", node_arg)
    assert (dc.rest_base(), dc.rpc_base()) == (rest, rpc)


def test_an_override_wins_and_an_unparsable_node_has_no_endpoint(monkeypatch):
    monkeypatch.setattr(dc, "NODE", "tcp://chain:26657")
    monkeypatch.setenv("DENDRA_REST_URL", "http://rest.example:9999/")
    assert dc.rest_base() == "http://rest.example:9999"
    monkeypatch.delenv("DENDRA_REST_URL")
    monkeypatch.setattr(dc, "NODE", "tcp://:26657")
    assert dc.rest_base() is None and dc.rpc_base() is None
    assert dc.rest_json("/dendra/jobs/v1/params") is None       # no address is an unread answer


# -- the switch back to the CLI -------------------------------------------------------------------------------
class _Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


@pytest.mark.parametrize("mode", ["cli", "CLI", " cli "])
def test_the_switch_routes_every_read_back_to_the_cli(node, monkeypatch, mode):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        sub = cmd[3] if cmd[:3] == ["dendrad", "query", "jobs"] else cmd[1]
        return _Proc(0, json.dumps({"list-job": {"job": [J1]}, "params": {"params": {"verification_mode": "1"}},
                                    "audit-deferred": {}, "held-summary": {}, "committee-seed-health": {},
                                    "get-pools": {}, "status": {"sync_info": {"latest_block_height": "9"}}}[sub]))
    monkeypatch.setattr(dc.subprocess, "run", run)
    monkeypatch.setenv("DENDRA_CHAIN_READ", mode)
    assert dc.list_all("list-job", "job") == [J1]
    assert dc.audit_deferred() == {"job_ids": [], "audits_opened": 0}
    assert dc.held_summary()["pockets_measured"] == 0
    assert dc.committee_seed_health()["seed_height"] is None
    assert dc.pools() == {}
    assert dc.height() == 9
    assert dc._verification_mode() == 1
    assert len(calls) >= 7 and all(c[0] == "dendrad" for c in calls)
    assert node.requests == []                           # nothing went to the node's REST or RPC


@pytest.mark.parametrize("mode", ["", "  ", "rest", "REST"])
def test_an_empty_switch_is_the_default_rest(node, monkeypatch, mode):
    monkeypatch.setenv("DENDRA_CHAIN_READ", mode)           # compose passes it empty when the host sets nothing
    _jobs_two_pages(node)
    assert dc._use_rest() is True
    assert [r["job_id"] for r in dc.list_all("list-job", "job")] == ["j1", "j2"]
    assert node.spawned == [] and len(node.requests) == 2


@pytest.mark.parametrize("mode", ["bogus", "res", "cli rest", "0"])
def test_a_switch_that_is_neither_rest_nor_cli_is_refused(monkeypatch, mode):
    monkeypatch.setenv("DENDRA_CHAIN_READ", mode)
    with pytest.raises(ValueError):
        dc.check_chain_read_setting()
    with pytest.raises(ValueError):
        dc._use_rest()


@pytest.mark.parametrize("mode,refused", [("bogus", True), ("cli", False), ("", False), ("rest", False)])
def test_a_service_importing_the_client_refuses_a_bad_switch_at_start(mode, refused):
    """Runs the import in a fresh interpreter: the refusal is at import, so no service can start on it."""
    import subprocess
    env = dict(os.environ, DENDRA_CHAIN_READ=mode)
    moda = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import client",
                        MODEA or moda], env=env, capture_output=True, text=True, timeout=60)
    assert (p.returncode != 0) is refused
    if refused:
        assert "DENDRA_CHAIN_READ" in p.stderr and "ValueError" in p.stderr


def test_the_cli_path_still_carries_the_page_key(node, monkeypatch):
    calls = []
    pages = [_Proc(0, json.dumps({"job": [J1], "pagination": {"next_key": "K2"}})),
             _Proc(0, json.dumps({"job": [J2]}))]

    def run(cmd, **kw):
        calls.append(list(cmd))
        return pages[len(calls) - 1]
    monkeypatch.setattr(dc.subprocess, "run", run)
    monkeypatch.setenv("DENDRA_CHAIN_READ", "cli")
    assert [r["job_id"] for r in dc.list_all("list-job", "job")] == ["j1", "j2"]
    assert calls[1][calls[1].index("--page-key") + 1] == "K2" and "--page-limit" in calls[0]
    assert node.requests == []


def test_a_subcommand_without_a_route_stays_on_the_cli(node, monkeypatch):
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", lambda cmd, **kw: (calls.append(list(cmd)), _Proc(0, "{}"))[1])
    assert dc.list_all("list-other", "other") == []
    assert len(calls) == 1 and node.requests == []
