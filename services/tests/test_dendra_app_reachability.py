"""Bench of the reachability card of the Dendra desktop application (C16): `reachability_view` runs
deploy/node_reachability.sh --json against a node and says its answer; the page draws it.

What is replaced: the node. A local HTTP server answers /net_info and /status the way a node's RPC does, and the
node kit's .env names its port, so the shipped script runs for real (bash, curl, python3) and its JSON is what the
application reads. What is real: the script, the application's reading of it, the routes with their token, the cache,
and the page's own script (run by node when it is installed).

The script under test is the shipped one unless DENDRA_REACH_SH names another (a mutated copy); the application is the
shipped one unless DENDRA_APP_DIR names another (see test_dendra_app.py)."""
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# The fixtures and helpers of the application's own bench: one way to start the application, one way to call it.
import test_dendra_app as T
from test_dendra_app import app, call, run_ui  # noqa: F401

DASH = chr(0x2014)

HERE = os.path.dirname(os.path.abspath(__file__))
# DENDRA_REPO names the tree when pytest runs from a copy of prototype/ (the battery does); otherwise the tree is
# found by walking up from this file.
ROOT = os.environ.get("DENDRA_REPO") or HERE
while ROOT != os.path.dirname(ROOT) and not os.path.isfile(os.path.join(ROOT, "deploy", "node_reachability.sh")):
    ROOT = os.path.dirname(ROOT)
SCRIPT = os.environ.get("DENDRA_REACH_SH") or os.path.join(ROOT, "deploy", "node_reachability.sh")
if os.environ.get("DENDRA_REACH_SH") and not os.path.isfile(SCRIPT):
    # A named script that is not there is a refusal that names it, never a run of tests against a missing subject.
    sys.stderr.write("DENDRA_REACH_SH names no file: %s\n" % SCRIPT)
    sys.exit(2)
COMPOSE = os.path.join(ROOT, "deploy", "testnet-node", "docker-compose.yml")
NEEDS_CURL = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed: the script cannot ask the node (declared, not passed)")


class _Rpc(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        doc = self.server.docs.get(urllib.parse.urlsplit(self.path).path)
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode() if doc is not None else b"{}"
        self.send_response(200 if doc is not None else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def rpc():
    """A node's RPC: node(listen, inbound, outbound) sets what it answers and returns its port."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Rpc)
    srv.docs = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def node(listen, inbound=0, outbound=1, rpc_address="tcp://127.0.0.1:26657"):
        peers = [{"is_outbound": False, "remote_ip": "198.51.100.%d" % i} for i in range(inbound)]
        peers += [{"is_outbound": True, "remote_ip": "192.0.2.%d" % i} for i in range(outbound)]
        srv.docs["/net_info"] = {"result": {"n_peers": str(len(peers)), "peers": peers}}
        srv.docs["/status"] = {"result": {"node_info": {"id": "abc", "listen_addr": listen, "other": {"rpc_address": rpc_address}},
                                          "sync_info": {"latest_block_height": "1000", "catching_up": False}}}
        return srv.server_address[1]
    node.docs = srv.docs
    yield node
    srv.shutdown()


@pytest.fixture()
def rig(app, tmp_path, monkeypatch):
    """The application with a miner kit whose node runs on this machine, a node kit whose .env names `port`, and the
    script under test. The kit's .env is what join.sh writes for a miner on its own node."""
    A, kit = app
    nodekit = tmp_path / "nodekit"
    nodekit.mkdir()
    (kit / ".env").write_text("DENDRA_NODE=tcp://dendra-node:26657" + chr(10), encoding="utf-8")
    monkeypatch.setattr(A, "NODE_KIT", str(nodekit))
    monkeypatch.setattr(A, "REACHABILITY_SH", SCRIPT)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("DENDRA_NETINFO_FILE", raising=False)
    monkeypatch.delenv("DENDRA_STATUS_FILE", raising=False)

    def at(port, extra=""):
        (nodekit / ".env").write_text(f"DENDRA_RPC_PORT={port}" + chr(10) + extra, encoding="utf-8")
        A._REACH.update(at=0.0, view=None)
    return A, kit, nodekit, at


@NEEDS_CURL
def test_an_open_node_is_announced_at_its_public_address(rig, rpc):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://203.0.113.7:26656", inbound=2, outbound=3))
    v = A.reachability_view()
    assert (v["state"], v["case"], v["word"]) == ("open", "open", "reachable")
    assert v["reachable_at"] == "203.0.113.7:26656" and "203.0.113.7:26656" in v["says"]
    assert v["peers"] == "2 inbound, 3 outbound" and v["advertised"] == "tcp://203.0.113.7:26656"
    # Nothing to fix, so nothing to do: no steps, and the card does not tell an open node how to open itself.
    assert v["steps"] == [] and v["wsl_steps"] == [] and v["optional"] is True


@NEEDS_CURL
@pytest.mark.parametrize("listen, inbound, case, words", [
    ("tcp://0.0.0.0:26656", 0, "wildcard", "no address that anyone can dial"),
    ("tcp://127.0.0.1:26656", 1, "loopback", "only this machine can reach"),
    ("tcp://192.168.1.42:26656", 2, "private", "only your own network can reach"),
    ("tcp://169.254.10.1:26656", 1, "link-local", "network segment"),
    ("tcp://203.0.113.7:26656", 0, "no-inbound", "no node has ever connected in"),
])
def test_a_node_nobody_can_dial_is_not_reachable_and_the_steps_that_open_it_are_given(rig, rpc, listen, inbound, case, words):
    A, _kit, nodekit, at = rig
    at(rpc(listen, inbound=inbound))
    v = A.reachability_view()
    assert (v["state"], v["case"], v["word"]) == ("closed", case, "not reachable")
    assert words in v["says"]
    # Never announced as an address peers dial.
    assert v["reachable_at"] is None
    s = " ".join(v["steps"])
    assert "forward TCP port 26656" in s and "never put this machine in the router's DMZ" in s and "DENDRA_EXTERNAL_ADDRESS=" in s
    assert f"{nodekit}/.env" in s and "docker compose up -d" in s
    assert v["wsl_steps"] == []


@NEEDS_CURL
def test_carrier_grade_nat_is_not_fixed_on_the_router(rig, rpc):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://100.64.0.7:26656", inbound=1))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("closed", "carrier-nat")
    assert "provider" in v["says"] and "cannot open it" in v["says"]
    # A router forward would send the reader to the wrong place.
    assert v["steps"] == [] and v["wsl_steps"] == [] and v["reachable_at"] is None


@NEEDS_CURL
def test_a_node_that_does_not_answer_is_not_measured_and_never_open_or_closed(rig):
    A, _kit, _nodekit, at = rig
    # Port 9 (discard) is closed on this machine: the script's curl is refused.
    at(9)
    v = A.reachability_view()
    assert (v["state"], v["case"], v["word"]) == ("unknown", "node-unreachable", "not measured")
    assert v["steps"] == [] and v["reachable_at"] is None and v["peers"] == "not read"


@NEEDS_CURL
def test_an_answer_the_script_cannot_read_is_not_measured(rig, rpc):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://203.0.113.7:26656", inbound=1))
    assert A.reachability_view()["state"] == "open"        # the witness: the same node, readable, is open
    # net_info that is not JSON: the node answered, the payload is unreadable.
    rpc.docs["/net_info"] = b"not json"
    v = A.reachability_view(force=True)
    assert (v["state"], v["case"], v["word"]) == ("unknown", "unparsable", "not measured")
    assert v["steps"] == [] and v["reachable_at"] is None


def test_a_miner_that_reads_a_node_elsewhere_has_no_port_to_open_and_the_script_is_not_run(rig, monkeypatch):
    A, kit, _nodekit, at = rig
    at(26657)
    (kit / ".env").write_text("DENDRA_NODE=tcp://rpc.example.org:26657" + chr(10), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("the script must not run for a miner that reads a node elsewhere")
    monkeypatch.setattr(A, "run_reachability", boom)
    v = A.reachability_view()
    assert (v["state"], v["case"], v["word"]) == ("none", "remote-rpc", "no node here")
    assert "rpc.example.org" in v["says"] and "no port to open" in v["says"] and v["steps"] == []


def test_without_a_miner_kit_there_is_no_node_to_test(rig, monkeypatch):
    A, kit, _nodekit, _at = rig
    (kit / ".env").unlink()
    monkeypatch.setattr(A, "run_reachability", lambda rpc: pytest.fail("the script must not run without a kit"))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("none", "no-kit")


def _fake_run(monkeypatch, A, rc=None, out="", exc=None):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    monkeypatch.setattr(A.subprocess, "run", run)
    return seen


def _doc(**over):
    d = {"state": "closed", "case": "wildcard", "source": "node", "exit": 1, "listen": "tcp://0.0.0.0:26656",
         "advertised": "wildcard", "inbound": 0, "outbound": 1, "height": 5, "catching_up": False, "rpc_wildcard": False,
         "address": None}
    d.update(over)
    return json.dumps(d)


def test_the_application_reads_the_scripts_line_when_it_agrees_with_its_exit_code(rig, monkeypatch):
    A, _kit, _nodekit, at = rig
    at(26657)
    seen = _fake_run(monkeypatch, A, rc=1, out="noise" + chr(10) + _doc() + chr(10))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("closed", "wildcard")
    argv, kw = seen[0]
    assert argv[0] == "bash" and argv[-1] == "--json" and argv[1] == SCRIPT
    assert kw["env"]["DENDRA_LOCAL_RPC"] == "http://127.0.0.1:26657" and kw["timeout"] == A.REACH_TIMEOUT_S


@pytest.mark.parametrize("rc, out, what", [
    (0, _doc(), "exit 0 for a line that says closed"),
    (1, _doc(state="open", case="open", exit=1, address="203.0.113.7:26656"), "exit 1 for a line that says open"),
    (2, _doc(), "exit 2 for a line that says closed"),
    (3, _doc(), "an exit code the script does not have"),
    (1, _doc(exit=0), "a line whose own exit differs"),
    (0, _doc(state="open", case="open", exit=0, source="injected", address="203.0.113.7:26656"), "a reading of fixtures"),
    (0, _doc(state="open", case="open", exit=0, address=None), "open with no address to announce"),
    (0, _doc(state="open", case="open", exit=0, address=""), "open with an empty address"),
    (1, _doc(case="a-case-the-table-lacks"), "a case the application does not know"),
    (0, _doc(state="open", case="wildcard", exit=0, address="198.51.100.4:5"), "a case of another state"),
    (1, _doc(inbound="two"), "a count that is not a number"),
    (1, _doc(listen=5), "an address that is not text"),
    (1, "not json at all", "no JSON"),
    (1, "", "no output"),
])
def test_a_line_that_disagrees_with_the_exit_code_or_the_table_is_not_read(rig, monkeypatch, rc, out, what):
    A, _kit, _nodekit, at = rig
    at(26657)
    _fake_run(monkeypatch, A, rc=rc, out=out)
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("unknown", "not-read"), what
    assert v["steps"] == [] and v["reachable_at"] is None and "does not read" in v["says"]


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("bash", 45), OSError("no bash")])
def test_a_check_that_did_not_run_is_not_read(rig, monkeypatch, exc):
    A, _kit, _nodekit, at = rig
    at(26657)
    _fake_run(monkeypatch, A, exc=exc)
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("unknown", "not-read") and "did not run" in v["says"]


def test_a_clone_without_the_script_is_not_read(rig, monkeypatch, tmp_path):
    A, _kit, _nodekit, at = rig
    at(26657)
    monkeypatch.setattr(A, "REACHABILITY_SH", str(tmp_path / "absent.sh"))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("unknown", "not-read") and "not in this clone" in v["says"]


@NEEDS_CURL
def test_the_fixtures_of_the_scripts_own_bench_never_decide_what_is_read(rig, rpc, monkeypatch, tmp_path):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://0.0.0.0:26656", inbound=0))
    green_net, green_st = tmp_path / "n.json", tmp_path / "s.json"
    green_net.write_text(json.dumps({"result": {"peers": [{"is_outbound": False}]}}))
    green_st.write_text(json.dumps({"result": {"node_info": {"listen_addr": "tcp://203.0.113.7:26656", "other": {}},
                                              "sync_info": {"latest_block_height": "1", "catching_up": False}}}))
    monkeypatch.setenv("DENDRA_NETINFO_FILE", str(green_net))
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(green_st))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("closed", "wildcard")


def test_the_reading_is_kept_for_a_minute_and_a_forced_read_goes_to_the_script(rig, monkeypatch):
    A, _kit, _nodekit, at = rig
    at(26657)
    calls = []
    monkeypatch.setattr(A, "run_reachability", lambda rpc: calls.append(rpc) or A._reach_unread("bench"))
    A.reachability_view()
    A.reachability_view()
    assert len(calls) == 1
    A.reachability_view(force=True)
    assert len(calls) == 2
    now = A.time.monotonic()
    monkeypatch.setattr(A.time, "monotonic", lambda: now + A.REACH_TTL_S + 1)
    A.reachability_view()
    assert len(calls) == 3


def test_the_routes_need_the_token_and_the_forced_read_is_one_at_a_time(rig, monkeypatch):
    A, _kit, _nodekit, at = rig
    at(26657)
    monkeypatch.setattr(A, "run_reachability", lambda rpc: A._reach_unread("bench"))
    assert call(A, "/api/reachability", token=False)[0] == 401
    assert call(A, "/api/reachability", token="wrong")[0] == 401
    assert call(A, "/api/reachability/run", body={}, token=False)[0] == 401
    code, doc = call(A, "/api/reachability")
    assert code == 200 and doc["state"] == "unknown" and doc["case"] == "not-read"
    code, doc = call(A, "/api/reachability/run", body={})
    assert code == 200 and doc["case"] == "not-read"
    # The route releases the lock after it has sent its answer, so the answer can arrive first: wait for the release.
    assert A._REACH_RUN.acquire(timeout=10)
    try:
        assert call(A, "/api/reachability/run", body={})[0] == 409
    finally:
        A._REACH_RUN.release()


def test_the_overview_does_not_run_the_check(app):
    # The overview is read every fifteen seconds; the check asks a node twice. It has its own route.
    A, _kit = app
    assert "reachab" not in inspect.getsource(A.Handler._overview).lower()


def test_the_node_kits_ports_are_read_from_its_env_and_a_wrong_one_is_not_guessed_around(rig, monkeypatch):
    A, _kit, nodekit, at = rig
    assert A.reach_rpc({}) == ("http://127.0.0.1:26657", None)
    assert A.reach_rpc({"DENDRA_RPC_PORT": "27000"}) == ("http://127.0.0.1:27000", None)
    assert A.reach_rpc({"DENDRA_RPC_BIND": "0.0.0.0"}) == ("http://127.0.0.1:26657", None)
    assert A.reach_rpc({"DENDRA_RPC_BIND": "10.0.0.5", "DENDRA_RPC_PORT": "26700"}) == ("http://10.0.0.5:26700", None)
    for bad in ("abc", "0", "65536", "-1", "26657 ", "026657"):
        url, why = A.reach_rpc({"DENDRA_RPC_PORT": bad})
        assert url is None and "DENDRA_RPC_PORT" in why, bad
    for bad in ("localhost", "::1", "1.2.3", "999.1.1.1"):
        url, why = A.reach_rpc({"DENDRA_RPC_BIND": bad})
        assert url is None and "DENDRA_RPC_BIND" in why, bad
    # ... and the card says so instead of asking a port it made up.
    at("abc")
    monkeypatch.setattr(A, "run_reachability", lambda rpc: pytest.fail("no port was readable"))
    v = A.reachability_view()
    assert (v["state"], v["case"]) == ("unknown", "not-read") and "DENDRA_RPC_PORT" in v["says"]


def test_the_defaults_are_the_node_kits_compose_defaults(app):
    A, _kit = app
    with open(COMPOSE, encoding="utf-8") as f:
        text = f.read()
    rpc = re.search(r"[$][{]DENDRA_RPC_BIND:-([0-9.]+)[}]:[$][{]DENDRA_RPC_PORT:-([0-9]+)[}]:26657", text)
    p2p = re.search(r"[$][{]DENDRA_P2P_PORT:-([0-9]+)[}]:26656", text)
    assert rpc and p2p
    assert A.NODE_DEFAULTS == {"rpc_bind": rpc.group(1), "rpc_port": rpc.group(2), "p2p_port": p2p.group(1)}


@NEEDS_CURL
def test_the_steps_follow_the_p2p_port_of_the_node_kit_and_the_wsl_steps_come_only_under_wsl(rig, rpc, monkeypatch):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://0.0.0.0:26656"), extra="DENDRA_P2P_PORT=26700" + chr(10))
    v = A.reachability_view()
    assert "to this machine, port 26700" in v["steps"][0] and v["wsl_steps"] == []
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    v = A.reachability_view(force=True)
    w = " ".join(v["wsl_steps"])
    assert "networkingMode=mirrored" in w and "wsl --shutdown" in w and "-LocalPorts 26700" in w
    assert "New-NetFirewallHyperVRule" in w and "Windows 10" in w


@NEEDS_CURL
def test_the_steps_warn_about_the_refusal_and_give_the_way_back(rig, rpc):
    # docker/node-join.sh exits on a bad DENDRA_EXTERNAL_ADDRESS, so `docker compose up -d` with one leaves the node
    # stopped and the miner without a chain to read: the steps say what the node refuses, that the command restarts it,
    # and how to undo the line.
    A, _kit, nodekit, at = rig
    at(rpc("tcp://0.0.0.0:26656"))
    steps = A.reachability_view()["steps"]
    s = " ".join(steps)
    assert "Use the address the internet sees for your router, not the one this machine has on your network" in s
    assert "refuses to start when the port is missing" in s and "carrier-grade NAT" in s
    assert "This restarts the node, and the miner above cannot read the chain until it is back" in s
    assert "empty the DENDRA_EXTERNAL_ADDRESS line and run the same command again" in s
    assert "press Check now" in s
    # The command comes after the warning about what to put in the line, never before it.
    assert s.index("refuses to start") < s.index("docker compose up -d") < s.index("empty the DENDRA_EXTERNAL_ADDRESS line")
    assert len(steps) == 4 and f"{nodekit}/.env" in steps[1]


@NEEDS_CURL
def test_a_value_the_node_would_refuse_is_named_before_the_command_is_run(rig, rpc):
    A, _kit, nodekit, at = rig
    # The common mistake: the address this machine has on the local network, written as the public one.
    at(rpc("tcp://203.0.113.7:26656", inbound=0), extra="DENDRA_EXTERNAL_ADDRESS=192.168.1.20:26656" + chr(10))
    v = A.reachability_view()
    assert v["case"] == "no-inbound" and "192.168.1.20:26656" in v["steps"][0]
    assert "refuses to start with it" in v["steps"][0] and "private" in v["steps"][0] and f"{nodekit}/.env" in v["steps"][0]
    assert "forward TCP port 26656" in v["steps"][1] and len(v["steps"]) == 5
    # A value without a port is named for its port.
    at(rpc("tcp://203.0.113.7:26656", inbound=0), extra="DENDRA_EXTERNAL_ADDRESS=203.0.113.7" + chr(10))
    assert "host:port" in A.reachability_view(force=True)["steps"][0]
    # A value the node accepts is not named, and neither is no value.
    at(rpc("tcp://203.0.113.7:26656", inbound=0), extra="DENDRA_EXTERNAL_ADDRESS=203.0.113.7:26656" + chr(10))
    v = A.reachability_view(force=True)
    assert "forward TCP port 26656" in v["steps"][0] and len(v["steps"]) == 4
    at(rpc("tcp://203.0.113.7:26656", inbound=0), extra="DENDRA_EXTERNAL_ADDRESS=" + chr(10))
    assert "forward TCP port 26656" in A.reachability_view(force=True)["steps"][0]


NODE_JOIN = os.path.join(ROOT, "docker", "node-join.sh")
# Values around every pattern of the node's test: ranges at both edges, the case of "localhost", a host with no dot after
# its second part, a missing or non-numeric port, the IPv6 forms the node names.
EXTERNAL_VALUES = [
    "", "203.0.113.7:26656", "node.example.org:26656", "203.0.113.7", "203.0.113.7:", ":26656", "203.0.113.7:26x56", "198.51.100.4:26656:26657",
    "0.0.0.0:26656", "127.0.0.1:26656", "127:26656", "localhost:26656", "LOCALHOST:26656", "10.1.2.3:26656", "10.:26656", "100.0.0.1:26656",
    "192.168.1.20:26656", "192.169.1.20:26656", "192.168:26656", "169.254.1.1:26656", "169.253.1.1:26656",
    "172.15.0.1:26656", "172.16.0.1:26656", "172.16.:26656", "172.16:26656", "172.19.4.4:26656", "172.20.4.4:26656", "172.29.4.4:26656",
    "172.30.4.4:26656", "172.31.255.1:26656", "172.32.0.1:26656", "172.3.0.1:26656",
    "100.63.0.1:26656", "100.64.0.1:26656", "100.69.0.1:26656", "100.70.0.1:26656", "100.99.0.1:26656", "100.100.0.1:26656",
    "100.119.0.1:26656", "100.120.0.1:26656", "100.127.255.254:26656", "100.128.0.1:26656", "100.64:26656",
    "[::1]:26656", "[::]:26656", "::1:26656", "[2001:db8::1]:26656", ":::26656",
]


def _shipped_refuses(value: str) -> bool:
    """True when the shipped test of DENDRA_EXTERNAL_ADDRESS in docker/node-join.sh exits 1 on `value`. Its own lines run,
    cut out between two anchors (the assignment of _EA and the line that builds P2P_ARGS) and closed by the `fi` of the
    `if` they open; a block that is not found, or that is longer than the test ever was, fails the case."""
    with open(NODE_JOIN, encoding="utf-8") as f:
        lines = f.read().split(chr(10))
    start = next(i for i, ln in enumerate(lines) if ln == '_EA="${DENDRA_EXTERNAL_ADDRESS:-}"')
    stop = next(i for i, ln in enumerate(lines) if i > start and ln.startswith('  P2P_ARGS="--p2p.external-address'))
    assert stop - start < 40
    script = chr(10).join(lines[start:stop + 1] + ["fi"])
    r = subprocess.run(["bash", "-c", script], env={"PATH": os.environ.get("PATH", ""), "DENDRA_EXTERNAL_ADDRESS": value},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode in (0, 1), r.stderr
    return r.returncode == 1


def test_the_refusal_the_card_announces_is_the_one_the_node_makes_the_shipped_test_of_node_join(app):
    A, _kit = app
    wrong = []
    for value in EXTERNAL_VALUES:
        said = A.external_address_refusal(value) is not None
        if said != _shipped_refuses(value):
            wrong.append((value, "card" if said else "node"))
    assert wrong == [], "card and node disagree (who refuses): %r" % (wrong,)
    # The matrix is not one-sided: it has values both ways.
    assert any(_shipped_refuses(v) for v in EXTERNAL_VALUES) and not all(_shipped_refuses(v) for v in EXTERNAL_VALUES)


@NEEDS_CURL
def test_the_wsl_steps_are_not_given_to_a_node_they_cannot_help(rig, rpc, monkeypatch):
    A, _kit, _nodekit, at = rig
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    at(rpc("tcp://203.0.113.7:26656", inbound=1))
    assert A.reachability_view()["wsl_steps"] == []
    at(rpc("tcp://100.64.0.7:26656", inbound=1))
    assert A.reachability_view(force=True)["wsl_steps"] == []


@NEEDS_CURL
def test_the_whole_way_from_the_page_s_route_to_the_script(rig, rpc):
    A, _kit, _nodekit, at = rig
    at(rpc("tcp://203.0.113.7:26656", inbound=1))
    code, doc = call(A, "/api/reachability")
    assert code == 200 and doc["state"] == "open" and doc["reachable_at"] == "203.0.113.7:26656"
    code, doc = call(A, "/api/reachability/run", body={})
    assert code == 200 and doc["state"] == "open"


# --- the page ---
def _view(**over):
    v = {"state": "closed", "case": "wildcard", "word": "not reachable", "says": "Your node advertises no address.",
         "peers": "0 inbound, 1 outbound", "advertised": "tcp://0.0.0.0:26656", "reachable_at": None,
         "steps": ["Forward the port.", "Set the address."], "wsl_steps": [], "checked_at": 1, "optional": True}
    v.update(over)
    return v


def _ui(tmp_path, reach, run=None, steps=()):
    replies = {"/api/overview": [{"status": 200, "data": {"installed": False, "hint": "x"}}]}
    if reach is not None:
        replies["/api/reachability"] = [{"status": 200, "data": reach}]
    if run is not None:
        replies["/api/reachability/run"] = [run]
    return run_ui(tmp_path, replies, steps)


def test_the_page_draws_an_open_node_in_green_with_its_address_and_no_steps(tmp_path):
    out = _ui(tmp_path, _view(state="open", case="open", word="reachable", says="Peers dial it at 203.0.113.7:26656.",
                              advertised="tcp://203.0.113.7:26656", reachable_at="203.0.113.7:26656", steps=[]))
    t = out["text"]
    assert t["reachState"] == "reachable" and out["cls"]["reachState"] == "pill ok"
    assert t["reachSays"] == "Peers dial it at 203.0.113.7:26656." and t["reachAt"] == "203.0.113.7:26656"
    assert out["hidden"]["reachSteps"] is True and out["hidden"]["reachStepsTitle"] is True and out["hidden"]["reachWsl"] is True


def test_the_page_draws_a_closed_node_with_its_steps_and_no_alarm_colour(tmp_path):
    out = _ui(tmp_path, _view(wsl_steps=["Mirror the network."]))
    t = out["text"]
    assert t["reachState"] == "not reachable" and out["cls"]["reachState"].strip() == "pill"
    assert t["reachAt"] == DASH and t["reachAdvertised"] == "tcp://0.0.0.0:26656" and t["reachPeers"] == "0 inbound, 1 outbound"
    assert out["kids"]["reachSteps"] == ["Forward the port.", "Set the address."] and out["hidden"]["reachSteps"] is False
    assert out["kids"]["reachWsl"] == ["Mirror the network."] and out["hidden"]["reachWsl"] is False
    assert out["hidden"]["reachStepsTitle"] is False and out["hidden"]["reachWslTitle"] is False


def test_the_page_shows_a_measure_that_failed_in_orange_and_a_missing_one_as_not_read(tmp_path):
    out = _ui(tmp_path, _view(state="unknown", case="node-unreachable", word="not measured", says="The RPC did not answer.",
                              steps=[], peers="not read", advertised=None))
    assert out["text"]["reachState"] == "not measured" and out["cls"]["reachState"] == "pill warn"
    assert out["text"]["reachAdvertised"] == DASH and out["hidden"]["reachSteps"] is True
    out = _ui(tmp_path, None)
    assert out["text"]["reachState"] == "not read" and out["cls"]["reachState"] == "pill warn"
    assert "older than the server" in out["text"]["reachSays"]


@pytest.mark.parametrize("bad", [{"state": "fine", "word": "fine", "says": "x"}, {"state": "open", "word": "reachable"},
                                 {"state": "open", "says": "x"}, {"word": "reachable", "says": "x"}])
def test_a_reading_the_page_does_not_know_is_never_drawn_as_reachable(tmp_path, bad):
    out = _ui(tmp_path, bad)
    assert out["text"]["reachState"] == "not read" and out["cls"]["reachState"] == "pill warn"
    assert out["text"]["reachAt"] == DASH


def test_the_page_puts_the_servers_words_in_as_text_and_never_as_markup(tmp_path):
    out = _ui(tmp_path, _view(says="<b>x</b>", steps=["<i>y</i>"]))
    assert out["text"]["reachSays"] == "<b>x</b>" and out["kids"]["reachSteps"] == ["<i>y</i>"]


def test_check_now_posts_to_the_forced_route_and_draws_what_it_returns(tmp_path):
    forced = _view(state="open", case="open", word="reachable", says="Peers dial it.", reachable_at="203.0.113.7:26656", steps=[])
    out = _ui(tmp_path, _view(), run={"status": 200, "data": forced}, steps=[["click", "reachBtn"]])
    assert any(c["path"] == "/api/reachability/run" and c["method"] == "POST" for c in out["calls"])
    assert out["text"]["reachState"] == "reachable" and out["text"]["reachMsg"] == "done"
    assert out["disabled"]["reachBtn"] is False
    out = _ui(tmp_path, _view(), run={"status": 409, "data": {"error": "a reachability check is already running"}},
              steps=[["click", "reachBtn"]])
    assert out["text"]["reachState"] == "not reachable" and "already running" in out["text"]["reachMsg"]


def test_the_page_says_it_is_optional_and_does_not_touch_the_miner_s_state():
    with open(os.path.join(T.APP, "ui", "index.html"), encoding="utf-8") as f:
        page = f.read()
    card = page[page.index('id="reachCard"'):page.index("<h2>Account</h2>")]
    assert "Optional" in card and "needs no inbound port" in card
    # The card does not claim to leave the miner alone: the node restart in its steps pauses the miner's reading of the chain.
    assert "only reads" in card and "node restart" in card and "does not change the state of your miner" not in card
    # The header pill and the state card are the status table's alone: the state function does not read this card.
    script = page[page.index("<script>"):]
    state_part = script[script.index("function renderStatus"):script.index('// "Test my machine"')]
    assert "reachab" not in state_part.lower() and "reachState" not in state_part
