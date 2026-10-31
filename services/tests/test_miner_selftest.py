"""Bench of miner_selftest.py, the check that runs inside the miner container.

What is real: the node's RPC, the network's RPC, the relay, the capacity registry and Ollama are local HTTP
servers that answer what each case sets; the self-test reaches them through its own code (the redirect-
refusing opener of final_season_chain, relay_client, modea.inference). What is replaced: `dendrad` and
`dendra-vrf`, by scripts in a hermetic PATH (that directory and nothing else), which answer from the case's
state -- the fake node answers a transaction search with the bounds it was asked, so a search that asks the
wrong window gets the wrong answer --, /proc, by a directory of cmdline files, and the relay signature, by a
recorder: signing is relay_client's and benched elsewhere; what is pinned here is that the self-test signs,
with height 0, the exact bytes it sends.

Every check is driven to its three states. The rule of zero is pinned document by document: an absent
`vrf_pubkey` is ko (proto3: the empty key), an absent `avail_epoch_blocks` is presence disarmed, an absent
transaction `code` is accepted -- while CometBFT's `catching_up` and the registry's `registered_onchain`,
which are not proto3, are unmeasured when absent, never false.

The subject can be pointed at a mutated copy with DENDRA_SELFTEST_FILE (the mutations of
the miner-health bench of the development repository).
"""
import importlib.util
import io
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

MODEA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODEA)
SUBJECT = os.environ.get("DENDRA_SELFTEST_FILE") or os.path.join(MODEA, "miner_selftest.py")


def _load():
    spec = importlib.util.spec_from_file_location("miner_selftest_under_bench", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MS = _load()
import relay_client  # noqa: E402  (the module the subject imported: one copy in sys.modules)

MID = "dm1bench"
ADDR = "dendra1benchoperator"
SK = "ab" * 64
PK = "cd" * 32
ENC = "ef" * 32
AVAIL = "/dendra.jobs.v1.MsgProveAvailability"
CREATE = "/dendra.jobs.v1.MsgCreateMiner"

FAKE_DENDRAD = r'''#!/usr/bin/env python3
import json, os, re, sys
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
with open(os.path.join(S, "dendrad.log"), "a") as f:
    f.write(json.dumps(a) + "\n")
def load(n, d=None):
    p = os.path.join(S, n)
    if not os.path.exists(p):
        return d
    with open(p) as f:
        return json.load(f)
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a[:2] == ["keys", "show"]:
    given = sys.stdin.read()
    with open(os.path.join(S, "keys_stdin.log"), "a") as f:
        f.write(json.dumps(given) + chr(10))
    if os.path.exists(os.path.join(S, "keyring_refused")):
        print("EOF", file=sys.stderr)
        print(a[2] + " is not a valid name or address: too many failed passphrase attempts", file=sys.stderr)
        sys.exit(1)
    print(load("address.json", "")); sys.exit(0)
if a[:3] == ["query", "jobs", "params"]:
    if os.path.exists(os.path.join(S, "params.fail")):
        print("Error: post failed: connection refused", file=sys.stderr); sys.exit(1)
    print(json.dumps(load("params.json"))); sys.exit(0)
if a[:3] == ["query", "jobs", "get-miner"]:
    mode = load("miner_mode.json", "ok")
    if mode == "notfound":
        print("Error: rpc error: code = NotFound desc = miner not found: key not found", file=sys.stderr); sys.exit(1)
    if mode == "transport":
        print('Error: post failed: Post "http://dendra-node:26657": dial tcp: connection refused', file=sys.stderr); sys.exit(1)
    print(json.dumps(load("miner.json"))); sys.exit(0)
if a[:3] == ["query", "modelregistry", "params"]:
    if os.path.exists(os.path.join(S, "modelregistry.fail")):
        print("Error: post failed: connection refused", file=sys.stderr); sys.exit(1)
    print(json.dumps(load("modelregistry.json", {"params": {}}))); sys.exit(0)
if a[:2] == ["query", "txs"]:
    q = opt("--query"); page = int(opt("--page")); limit = int(opt("--limit"))
    act = re.search(r"message.action='([^']+)'", q).group(1)
    lo = int(re.search(r"tx.height>=(\d+)", q).group(1)); hi = int(re.search(r"tx.height<=(\d+)", q).group(1))
    txs = [t for t in load("txs.json", []) if lo <= int(t["height"]) <= hi
           and any(m.get("@type") == act for m in t["tx"]["body"]["messages"])]
    d = {"txs": txs[(page - 1) * limit: page * limit]}
    if not os.path.exists(os.path.join(S, "omit_total")):
        d["total_count"] = str(len(txs))
    print(json.dumps(d)); sys.exit(0)
print("fake dendrad: unexpected " + " ".join(a), file=sys.stderr); sys.exit(64)
'''

FAKE_VRF = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
with open(os.path.join(S, "vrf.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["pubkey"]:
    pairs = json.load(open(os.path.join(S, "vrf_pairs.json")))
    sk = os.environ.get("DENDRA_VRF_SK", "")
    if sk in pairs:
        print(pairs[sk]); sys.exit(0)
    print("bad key", file=sys.stderr); sys.exit(1)
sys.exit(64)
'''


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _do(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = urllib.parse.urlsplit(self.path).path
        self.server.calls.append((method, path, body, dict(self.headers)))
        fn = self.server.routes.get((method, path)) or self.server.routes.get((method, "*"))
        code, out = fn(path, body) if fn else (404, b'{"error": "route"}')
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._do("GET")

    def do_POST(self):
        self._do("POST")


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    srv.routes, srv.calls = {}, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _j(doc):
    return json.dumps(doc).encode()


def _stat_line(pid, start, comm="python3"):
    """A /proc/<pid>/stat line whose field 22 (the start time) is `start`: after the command name, the state
    (field 3), eighteen fields, then the start time."""
    return f"{pid} ({comm}) " + " ".join(["S"] + ["0"] * 18 + [str(start)] + ["0", "0"])


class World:
    """One miner, its node, the network, the relay, the registry and its Ollama -- all healthy by default."""

    def __init__(self, tmp, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.state = tmp / "state"
        self.state.mkdir()
        self.keys = tmp / "keys"
        self.keys.mkdir()
        (self.keys / "resolved-identity").write_text(MID + "\n")
        (self.keys / f"{MID}.vrf").write_text(SK)
        self.proc = tmp / "proc"
        self.set_procs("miner.py", "reveal_worker.py", "capacity_sender.py")
        # the hermetic PATH: the two fakes and the interpreter their shebang names, nothing else
        b = tmp / "bin"
        b.mkdir()
        (b / "dendrad").write_text(FAKE_DENDRAD)
        (b / "dendra-vrf").write_text(FAKE_VRF)
        for f in ("dendrad", "dendra-vrf"):
            os.chmod(b / f, 0o755)
        os.symlink(sys.executable, b / "python3")
        self.write("address.json", ADDR)
        self.write("vrf_pairs.json", {SK: PK})
        self.write("miner.json", {"miner": {"miner_id": MID, "operator": ADDR, "stake": "5000000",
                                            "vrf_pubkey": PK, "enc_pubkey": ENC}})
        self.write("params.json", {"params": {"avail_epoch_blocks": "100", "avail_deadline_blocks": "0"}})
        self.height, self.climb, self.ref_height = 1250, True, 1260
        self.catching_up, self.earliest = False, 1
        self.write("txs.json", [self.tx(1210, self.proof(MID))])
        self.node, self.ref, self.relay, self.reg, self.oll = _serve(), _serve(), _serve(), _serve(), _serve()
        self.node.routes[("GET", "/status")] = lambda p, b: (200, _j(self.status()))
        self.ref.routes[("GET", "/status")] = lambda p, b: (200, _j({"result": {"sync_info": {
            "latest_block_height": str(self.ref_height), "catching_up": False, "earliest_block_height": "1"}}}))
        self.relay_list = {"req": [f"job7__{MID}", "job8__dm1other"], "res": [], "pub": [MID], "reveal": [], "attest": []}
        self.relay.routes[("GET", "/list")] = lambda p, b: (200, _j(self.relay_list))
        # The kit version `relay_client` read at import comes from the layout it runs in, and the battery runs
        # this folder from a copy without docker/KIT_VERSION. The world fabricates the file and installs what
        # the shipped reader makes of it, so the cases below do not depend on where the bench runs.
        kit_file = tmp / "KIT_VERSION"
        kit_file.write_text("9", encoding="utf-8")
        assert relay_client._kit is not None, "modea.kit_version did not import in relay_client"
        monkeypatch.setattr(relay_client, "_KIT_VERSION", relay_client._kit.read_kit_version(str(kit_file)))
        # The relay's copy, in the form this kit writes: the key and the kit version `relay_client` adds to
        # every body it posts.
        self.pub_stored = json.dumps(relay_client.declaring({"pub": ENC})).encode()
        self.relay.routes[("GET", f"/pub/{MID}")] = lambda p, b: (200, self.pub_stored) if self.pub_stored is not None else (404, b'{"error":"not found"}')
        self.post_answer = (200, b'{"ok":true}')
        self.relay.routes[("POST", f"/pub/{MID}")] = lambda p, b: self.post_answer
        self.rows = [{"node_id": "m-bench", "stale": False, "registered_onchain": True}]
        self.reg.routes[("GET", "/capacity")] = lambda p, b: (200, _j({"live_nodes": 1, "nodes": self.rows}))
        self.oll.routes[("POST", "/api/generate")] = lambda p, b: (200, _j({"response": "ready", "eval_count": 3, "prompt_eval_count": 7}))
        self.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (200, _j({"embedding": [0.1, -0.2, 0.3]}))
        self.hb = self.tmp / "heartbeat.json"
        self.heartbeat(written_at=int(time.time()) - 5, loop_at=int(time.time()) - 5, phase="loop")
        self.signed = []
        self.sign_ok = True

        def _sig(kind, key, data, miner_id, height):
            self.signed.append({"kind": kind, "key": key, "data": data, "miner_id": miner_id, "height": height})
            return {"X-Bench-Signed": "1"} if self.sign_ok else {}
        monkeypatch.setattr(relay_client, "_signature", _sig)
        for k in ("DENDRA_RELAY_TOKEN", "OLLAMA_TIMEOUT", "MINER_ID"):
            monkeypatch.delenv(k, raising=False)
        env = {"PATH": str(b), "FAKE_STATE": str(self.state), "DENDRA_NODE": f"tcp://127.0.0.1:{self.node.server_address[1]}",
               "DENDRA_RELAY": f"http://127.0.0.1:{self.relay.server_address[1]}",
               "OLLAMA_ENDPOINT": f"http://127.0.0.1:{self.oll.server_address[1]}", "OLLAMA_MODEL": "bench-model",
               "DENDRA_EMBED_API_MODEL": "bench-embed", "BACKEND": "ollama", "DENDRA_KEYRING_DIR": str(tmp / "kr"),
               "DENDRA_STATUS_FILE": str(self.hb), "DENDRA_MINER_REVEAL": "1", "DENDRA_MINER_JUDGE": "0",
               "DENDRA_SIGN_KEY": "m-bench", "DENDRA_SELFTEST_LOCK": str(tmp / "selftest.lock"),
               "DENDRA_JUDGE_STATE_FILE": str(tmp / "judge-state.json"),
               "DENDRA_SELFTEST_JUDGE_MEMORY_BASELINE": str(tmp / "judge-memory-baseline.json"),
               "DENDRA_CAPACITY_STATUS_FILE": str(tmp / "capacity-status.json"),
               # what the compose gives the container (the sender's address); --capacity-url points C8 at the bench
               "DENDRA_CAPACITY_URL": "https://registry.bench.example/capacity"}
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    # -- state
    def write(self, name, doc):
        (self.state / name).write_text(json.dumps(doc))

    def flag(self, name):
        (self.state / name).write_text("1")

    def status(self):
        h = self.height
        if self.climb:
            self.height += 1
        si = {"latest_block_height": str(h), "catching_up": self.catching_up, "earliest_block_height": str(self.earliest)}
        if self.catching_up is None:
            del si["catching_up"]
        return {"result": {"sync_info": si}}

    def proof(self, mid):
        return {"@type": AVAIL, "creator": ADDR, "miner_id": mid, "challenge": "c", "vrf_proof": "00"}

    def create(self, mid):
        return {"@type": CREATE, "creator": ADDR, "miner_id": mid, "operator": ADDR}

    def tx(self, height, *msgs, code=None):
        t = {"height": str(height), "txhash": f"H{height}", "tx": {"body": {"messages": list(msgs)}}}
        if code is not None:
            t["code"] = code
        return t

    def set_procs(self, *names):
        import shutil
        shutil.rmtree(self.proc, ignore_errors=True)
        self.proc.mkdir()
        for i, n in enumerate(names):
            d = self.proc / str(100 + i)
            d.mkdir()
            (d / "cmdline").write_bytes(b"\0".join([b"python3", n.encode(), b"--id", MID.encode()]) + b"\0")
            (d / "stat").write_text(_stat_line(100 + i, 9000 + i))
        (self.proc / "self").mkdir()

    def judge_state(self, model="qwen-judge", source="chain", **over):
        """The judge state the running judge_worker.py writes (modea/heartbeat.py::write_judge_state), bound to
        the judge_worker.py of this /proc: its pid and its start time, unless `over` says otherwise."""
        doc = {"schema": 1, "model": model, "source": source, "written_at": int(time.time())}
        for d in sorted(self.proc.iterdir()):
            cmd = d / "cmdline"
            if cmd.exists() and b"judge_worker.py" in cmd.read_bytes():
                doc["pid"] = int(d.name)
                doc["starttime"] = int((d / "stat").read_text().rsplit(")", 1)[1].split()[19])
        doc.update(over)
        self.judge_state_file().write_text(json.dumps(doc))

    def judge_state_file(self):
        return self.tmp / "judge-state.json"

    def heartbeat(self, **doc):
        self.hb.write_text(json.dumps(dict({"schema": 1}, **doc)))

    def posts(self):
        return [c for c in self.relay.calls if c[0] == "POST"]

    def dendrad_calls(self):
        p = self.state / "dendrad.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    # -- run
    def run(self, *args, capsys=None, mode="--json", ref=True, cap=True):
        argv = [mode, "--keydir", str(self.keys), "--proc-root", str(self.proc), "--height-wait", "1", *args]
        if ref:
            argv += ["--reference-rpc", f"tcp://127.0.0.1:{self.ref.server_address[1]}"]
        if cap:
            argv += ["--capacity-url", f"http://127.0.0.1:{self.reg.server_address[1]}/capacity", "--node-id", "m-bench"]
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            rc = MS.main(argv)
        finally:
            sys.stdout = old
        return rc, out.getvalue()

    def check(self, cid, *args, **kw):
        rc, out = self.run("--only", cid, *args, **kw)
        doc = json.loads(out)
        assert doc["rc"] == rc
        rows = [c for c in doc["checks"] if c["id"] == cid]
        assert len(rows) == 1, doc
        return rows[0]

    def close(self):
        for s in (self.node, self.ref, self.relay, self.reg, self.oll):
            s.shutdown()


@pytest.fixture()
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.close()


# -- the whole run ----------------------------------------------------------------------------------
def test_a_healthy_miner_is_ok_on_every_check(w):
    rc, out = w.run()
    doc = json.loads(out)
    bad = [c for c in doc["checks"] if c["state"] != "ok"]
    assert rc == 0 and not bad, bad
    assert [c["id"] for c in doc["checks"]] == [f"C{i}" for i in range(1, 10)]
    assert doc["summary"] == {"ok": 9, "ko": 0, "unmeasured": 0, "checks": 9}


def test_the_host_protocol_carries_the_document_and_its_counts(w):
    w.write("miner_mode.json", "notfound")
    rc, out = w.run(mode="--host")
    lines = out.splitlines()
    js = [ln for ln in lines if ln.startswith("DENDRA_SELFTEST_JSON ")]
    end = [ln for ln in lines if ln.startswith("DENDRA_SELFTEST_END ")]
    assert len(js) == 1 and len(end) == 1 and lines[-1] == end[0]
    doc = json.loads(js[0][len("DENDRA_SELFTEST_JSON "):])
    f = dict(kv.split("=") for kv in end[0].split()[1:])
    s = doc["summary"]
    assert rc == 1 == doc["rc"] == int(f["rc"])
    assert (int(f["ok"]), int(f["ko"]), int(f["unmeasured"]), int(f["checks"])) == (s["ok"], s["ko"], s["unmeasured"], s["checks"])
    assert any(ln.startswith("DENDRA_SELFTEST_TEXT ") and "[KO]" in ln for ln in lines)


def test_zero_checks_run_is_a_refusal_never_a_green(w):
    rc, out = w.run("--only", "C6", "--no-write", mode="--host")
    assert rc == 2
    assert out.splitlines()[-1] == "DENDRA_SELFTEST_END rc=2 ok=0 ko=0 unmeasured=0 checks=0"
    rc, out = w.run("--only", "")
    assert rc == 2 and json.loads(out)["summary"]["checks"] == 0


# -- the aggregate ----------------------------------------------------------------------------------
def test_the_aggregate_is_the_worst_state_never_the_last_one():
    R = MS.result
    assert MS.aggregate([R("C1", "a", "ok"), R("C2", "b", "ok")]) == 0
    assert MS.aggregate([R("C1", "a", "ko"), R("C2", "b", "ok")]) == 1
    assert MS.aggregate([R("C1", "a", "ok"), R("C2", "b", "unmeasured")]) == 2
    assert MS.aggregate([R("C1", "a", "unmeasured"), R("C2", "b", "ko"), R("C3", "c", "ok")]) == 1


def test_a_state_that_is_none_of_the_three_words_is_not_ok():
    R = MS.result
    assert MS.aggregate([R("C1", "a", "ok"), R("C2", "b", "fine")]) == 2
    assert MS.aggregate([R("C1", "a", "ok"), R("C2", "b", None)]) == 2
    assert MS.counts([R("C1", "a", "fine")]) == {"ok": 0, "ko": 0, "unmeasured": 1}


def test_a_check_that_raises_is_unmeasured_and_named(w, monkeypatch):
    def boom(ctx):
        raise RuntimeError("bench")
    monkeypatch.setattr(MS, "CHECKS", (("C5", boom),))
    rc, out = w.run()
    row = json.loads(out)["checks"][0]
    assert rc == 2 and row["state"] == "unmeasured" and "RuntimeError" in row["measured"]


# -- C1 the node RPC --------------------------------------------------------------------------------
def test_c1_a_climbing_height_is_ok(w):
    r = w.check("C1")
    assert r["state"] == "ok" and "1250 -> 1251" in r["measured"]


def test_c1_a_height_that_does_not_move_is_ko_and_the_bound_is_declared(w):
    w.climb = False
    r = w.check("C1")
    assert r["state"] == "ko" and "no new block in 1 s" in r["measured"] and "--height-wait" in r["reason"]


def test_c1_a_node_that_does_not_answer_is_ko(w, monkeypatch):
    port = w.node.server_address[1]
    w.node.shutdown()
    w.node.server_close()
    r = w.check("C1")
    assert r["state"] == "ko" and f"127.0.0.1:{port}" in r["measured"]


def test_c1_a_status_without_a_height_is_unmeasured(w):
    w.node.routes[("GET", "/status")] = lambda p, b: (200, _j({"result": {"sync_info": {"catching_up": False}}}))
    assert w.check("C1")["state"] == "unmeasured"


# -- C2 caught up -----------------------------------------------------------------------------------
def test_c2_a_node_close_behind_the_network_is_ok(w):
    r = w.check("C2")
    assert r["state"] == "ok" and "10 block(s) behind" in r["measured"] and "avail_epoch_blocks = 100" in r["measured"]


def test_c2_a_lag_beyond_the_deadline_is_ko(w):
    w.write("params.json", {"params": {"avail_epoch_blocks": "100", "avail_deadline_blocks": "5"}})
    r = w.check("C2")
    assert r["state"] == "ko" and "avail_deadline_blocks = 5" in r["reason"]


def test_c2_an_ABSENT_catching_up_is_unmeasured_never_false(w):
    # CometBFT's /status is not proto3: it always writes the flag, so an absent one is not "caught up".
    w.catching_up = None
    assert w.check("C2")["state"] == "unmeasured"


def test_c2_a_catching_up_that_is_not_a_boolean_is_unmeasured(w):
    w.node.routes[("GET", "/status")] = lambda p, b: (200, _j({"result": {"sync_info": {
        "latest_block_height": "1250", "catching_up": "false"}}}))
    assert w.check("C2")["state"] == "unmeasured"


def test_c2_a_node_catching_up_within_the_bound_is_said_not_alerted(w):
    w.catching_up = True
    r = w.check("C2")
    assert r["state"] == "ok" and r["measured"].startswith("catching up") and "block by block" in r["reason"]


def test_c2_without_the_network_s_rpc_the_lag_is_unmeasured(w):
    r = w.check("C2", ref=False)
    assert r["state"] == "unmeasured" and "no reference RPC" in r["reason"]


def test_c2_presence_disarmed_reports_the_lag_without_judging_it(w):
    w.write("params.json", {"params": {}})
    w.ref_height = 99999
    r = w.check("C2")
    assert r["state"] == "ok" and "reported, not judged" in r["reason"]


def test_c2_presence_disarmed_and_catching_up_is_ko(w):
    # No bound is published (avail_epoch_blocks 0), so the lag is not judged -- but the node's own flag is:
    # a node that says it is behind is behind, whatever the chain publishes.
    w.write("params.json", {"params": {}})
    w.catching_up = True
    r = w.check("C2")
    assert r["state"] == "ko" and r["measured"].startswith("catching up") and "the node itself says it is behind" in r["reason"]


# -- C3 registration and keys -----------------------------------------------------------------------
def test_c3_registered_with_the_key_held_here_is_ok(w):
    r = w.check("C3")
    assert r["state"] == "ok" and ADDR in r["measured"]


def test_c3_the_vrf_secret_never_goes_through_argv(w):
    w.check("C3")
    calls = (w.state / "vrf.log").read_text()
    assert SK not in calls and json.loads(calls.splitlines()[0]) == ["pubkey"]


def test_c3_an_ABSENT_vrf_pubkey_is_ko_not_unmeasured(w):
    # proto3 omits the empty string: an absent key is no key, and that is a reading.
    w.write("miner.json", {"miner": {"miner_id": MID, "operator": ADDR, "stake": "5000000", "enc_pubkey": ENC}})
    r = w.check("C3")
    assert r["state"] == "ko" and "no VRF key anchored" in r["measured"]


def test_c3_notfound_is_not_registered_and_a_dead_node_is_unmeasured(w):
    # Two opposite facts that a lazy reader renders alike: the chain says the miner is absent, versus
    # nothing answered at all.
    w.write("miner_mode.json", "notfound")
    r = w.check("C3")
    assert r["state"] == "ko" and "not registered" in r["measured"]
    (w.state / "miner_mode.json").unlink()
    w.write("miner_mode.json", "transport")
    MS_r = w.check("C3")
    assert MS_r["state"] == "unmeasured" and "connection refused" in MS_r["reason"]


def test_c3_a_key_mismatch_is_ko_and_names_the_rotation(w):
    w.write("vrf_pairs.json", {SK: "11" * 32})
    r = w.check("C3")
    assert r["state"] == "ko" and "mismatch" in r["measured"] and r["fix"].endswith("--new-vrf-pubkey " + "11" * 32)


def test_c3_no_vrf_secret_on_this_node_is_ko(w):
    (w.keys / f"{MID}.vrf").unlink()
    assert w.check("C3")["state"] == "ko"


def test_c3_another_operator_is_ko(w):
    w.write("address.json", "dendra1someoneelse")
    r = w.check("C3")
    assert r["state"] == "ko" and "dendra1someoneelse" in r["measured"]


def test_c3_on_a_slot_k_never_offers_slot_0_s_new_identity(w):
    # `deploy/join.sh --id` names slot 0's identity: printed in a slot k's report it would act on another
    # identity. A slot k's remedy is its own key, restored into its own keyring.
    w.write("address.json", "dendra1someoneelse")
    r = w.check("C3", "--compose", "bash /kit/slots.sh run 1")
    assert r["state"] == "ko" and "--id <new-id>" not in r["fix"]
    assert "keyring of this slot" in r["fix"] and "names slot 0 only" in r["fix"]
    # slot 0 keeps the remedy it always had
    assert "deploy/join.sh --id <new-id>" in w.check("C3")["fix"]


# -- C4 presence ------------------------------------------------------------------------------------
def test_c4_a_proof_in_the_current_window_is_present(w):
    assert w.check("C4")["state"] == "ok"


def test_c4_a_proof_in_the_PREVIOUS_window_is_present_and_the_search_asks_that_window(w):
    # height 1250, windows of 100: the chain counts windows 11 and 12 (presence.go::minerPresentAt)
    w.climb = False
    w.write("txs.json", [w.tx(1180, w.proof(MID))])
    r = w.check("C4")
    assert r["state"] == "ok", r
    q = [c for c in w.dendrad_calls() if c[:2] == ["query", "txs"]][0]
    query = q[q.index("--query") + 1]
    assert "tx.height>=1100" in query and "tx.height<=1250" in query


def test_c4_a_proof_two_windows_back_does_not_count(w):
    w.climb = False
    w.write("txs.json", [w.tx(1050, w.proof(MID))])
    assert w.check("C4")["state"] == "ko"


def test_c4_a_refused_proof_proves_nothing_and_an_absent_code_is_accepted(w):
    w.climb = False
    w.write("txs.json", [w.tx(1210, w.proof(MID), code=5)])
    assert w.check("C4")["state"] == "ko"
    w.write("txs.json", [w.tx(1210, w.proof(MID))])            # no `code`: the normal shape of a success
    assert w.check("C4")["state"] == "ok"


def test_c4_a_newcomer_has_the_grace_the_chain_gives(w):
    w.climb = False
    w.write("txs.json", [w.tx(1150, w.create(MID))])
    r = w.check("C4")
    assert r["state"] == "ok" and "newcomer" in r["measured"]


def test_c4_absent_is_ko_and_carries_the_daemon_s_last_refusal(w):
    w.climb = False
    w.write("txs.json", [w.tx(1210, w.proof("dm1other"))])
    w.heartbeat(written_at=int(time.time()), presence={"at": int(time.time()) - 60, "result": "refused",
                                                       "why": "invalid VRF proof for this challenge"})
    r = w.check("C4")
    assert r["state"] == "ko" and "no accepted availability proof" in r["measured"] and "invalid VRF proof" in r["reason"]


def test_c4_an_ABSENT_avail_epoch_blocks_is_presence_disarmed(w):
    w.write("params.json", {"params": {"work_gate_bps": "100"}})
    r = w.check("C4")
    assert r["state"] == "ok" and "disarmed" in r["measured"]


def test_c4_a_search_without_total_count_is_unmeasured(w):
    w.flag("omit_total")
    assert w.check("C4")["state"] == "unmeasured"


def test_c4_a_node_whose_history_does_not_reach_back_is_unmeasured(w):
    w.earliest = 1150
    r = w.check("C4")
    assert r["state"] == "unmeasured" and "state sync" in r["fix"]


# -- C5 the work queue ------------------------------------------------------------------------------
def test_c5_a_readable_queue_is_ok(w):
    r = w.check("C5")
    assert r["state"] == "ok" and "1 request(s) for this miner" in r["measured"]


def test_c5_a_refused_queue_is_ko(w):
    w.relay.routes[("GET", "/list")] = lambda p, b: (401, b'{"error":"unauthorized"}')
    assert w.check("C5")["state"] == "ko"


def test_c5_an_unreachable_relay_is_ko(w, monkeypatch):
    monkeypatch.setenv("DENDRA_RELAY", "http://127.0.0.1:1")
    assert w.check("C5")["state"] == "ko"


# -- C6 the relay key copy and the signed write -----------------------------------------------------
def test_c6_without_write_it_reads_and_writes_NOTHING(w):
    # Nothing changes without an explicit request: by default C6 compares the relay's copy with the chain's.
    r = w.check("C6")
    assert r["state"] == "ok" and "nothing was written" in r["measured"] and "--write" in r["reason"], r
    assert w.posts() == [] and w.signed == []


def test_c6_an_equal_copy_is_re_deposited_byte_for_byte_signed_at_height_0(w):
    r = w.check("C6", "--write")
    assert r["state"] == "ok", r
    posts = w.posts()
    assert len(posts) == 1 and posts[0][2] == w.pub_stored and posts[0][3].get("X-Bench-Signed") == "1"
    assert w.signed == [{"kind": "pub", "key": MID, "data": w.pub_stored, "miner_id": MID, "height": 0}]


def test_c6_a_replay_refusal_proves_the_path(w):
    w.post_answer = (401, _j({"error": "unauthorized", "why": "REPLAY (digest already accepted)"}))
    r = w.check("C6", "--write")
    assert r["state"] == "ok" and "replay" in r["measured"]


def test_c6_a_replay_refusal_from_an_older_relay_proves_the_path_too(w):
    # An older relay spells the same refusal in French; the kit is updated apart from it.
    w.post_answer = (401, _j({"error": "unauthorized", "why": "REJEU (digest already accepted)"}))
    r = w.check("C6", "--write")
    assert r["state"] == "ok" and "replay" in r["measured"]


def test_c6_another_refusal_is_ko_with_the_relay_s_reason(w):
    w.post_answer = (401, _j({"error": "unauthorized", "why": "the signer is not the miner's operator"}))
    r = w.check("C6", "--write")
    assert r["state"] == "ko" and "not the miner's operator" in r["measured"]


def test_c6_a_DIVERGENT_copy_is_ko_and_nothing_is_written(w):
    w.pub_stored = json.dumps({"pub": "00" * 32}).encode()
    for args in ((), ("--write",)):
        r = w.check("C6", *args)
        assert r["state"] == "ko" and "different key" in r["measured"]
    assert w.posts() == [] and w.signed == []


def test_c6_no_write_and_quick_write_nothing(w):
    for flag in ("--no-write", "--quick"):
        rc, out = w.run("--only", "C6", flag)
        doc = json.loads(out)
        assert doc["checks"] == [] and doc["skipped"] == [{"id": "C6", "why": flag}] and rc == 2
    assert w.posts() == []


def test_c6_write_and_no_write_together_are_refused(w):
    with pytest.raises(SystemExit):
        w.run("--only", "C6", "--write", "--no-write")
    assert w.posts() == []


def test_c6_an_absent_copy_is_said_and_written_only_on_request(w):
    w.pub_stored = None
    r = w.check("C6")
    assert r["state"] == "ok" and "no copy" in r["measured"] and "nothing was written" in r["measured"]
    assert "--write" in r["reason"] and w.posts() == []
    r = w.check("C6", "--write")
    assert r["state"] == "ok" and "no copy" in r["measured"]
    assert [p[2] for p in w.posts()] == [json.dumps(relay_client.declaring({"pub": ENC})).encode()]
    assert json.loads(w.posts()[0][2])["kit_version"] == relay_client._KIT_VERSION == 9


def test_c6_an_unreadable_kit_file_writes_no_field_never_zero(w, monkeypatch):
    # The daemon's rule: a kit file that cannot be read leaves the body without `kit_version`; a 0 would
    # describe a kit older than every release and hide the defect behind a refusal that looks like age.
    monkeypatch.setattr(relay_client, "_KIT_VERSION", relay_client._kit.read_kit_version(str(w.tmp / "absent")))
    w.pub_stored = None
    r = w.check("C6", "--write")
    assert r["state"] == "ok" and len(w.posts()) == 1
    assert json.loads(w.posts()[0][2]) == {"pub": ENC}


def test_c6_an_older_kit_s_copy_is_not_re_deposited_and_says_so(w):
    # A copy deposited before the kit version travelled in the body equals the chain's key, in a form this
    # kit does not write: nothing is written over it, and the result says the write path was not measured.
    w.pub_stored = json.dumps({"pub": ENC}).encode()
    r = w.check("C6", "--write")
    assert r["state"] == "unmeasured" and "older kit" in r["measured"] and w.posts() == []


def test_c6_a_deposit_that_cannot_be_signed_is_NEVER_sent(w):
    # An unsigned write is attributable to nobody: a relay that still accepts one would take it, and this
    # check would have caused it. Nothing goes out, and the verdict says so.
    w.sign_ok = False
    for stored in (w.pub_stored, None):
        w.pub_stored = stored
        r = w.check("C6", "--write")
        assert r["state"] == "ko" and "could not sign" in r["measured"] and "nothing was written" in r["measured"]
    assert w.posts() == []


def test_c6_a_relay_that_does_not_answer_is_unmeasured(w, monkeypatch):
    monkeypatch.setenv("DENDRA_RELAY", "http://127.0.0.1:1")
    assert w.check("C6")["state"] == "unmeasured"


# -- C7 the model -----------------------------------------------------------------------------------
def test_c7_a_generation_and_an_embedding_are_ok_and_latency_is_not_judged(w):
    r = w.check("C7")
    assert r["state"] == "ok" and "3 dimensions" in r["measured"] and "not judged" in r["reason"]


def test_c7_a_model_that_is_not_served_is_ko(w):
    w.oll.routes[("POST", "/api/generate")] = lambda p, b: (404, b'{"error":"model not found"}')
    assert w.check("C7")["state"] == "ko"


def test_c7_an_embedder_that_returns_nothing_is_ko(w):
    w.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (500, b'{"error":"boom"}')
    r = w.check("C7")
    assert r["state"] == "ko" and "bench-embed" in r["measured"]


def test_c7_quick_skips_the_model(w):
    rc, out = w.run("--only", "C7", "--quick")
    assert json.loads(out)["skipped"] == [{"id": "C7", "why": "--quick"}]
    assert not [c for c in w.oll.calls]


# C7's fixes name the service the miner calls. deploy/join.sh points the miner at `ollama-cpu` on a machine
# that joins as a judge on the CPU (write_cpu_judge_override) and leaves it on `ollama` otherwise, a miner on a
# card that also judges included. The endpoint below names the service as join.sh writes it; the bench's Ollama
# is reached through HTTP_PROXY, so the host is never resolved and the call still lands on the fake engine --
# and the Host header it records proves which endpoint the self-test actually called.
def _endpoint_names(w, monkeypatch, host):
    proxy = f"http://127.0.0.1:{w.oll.server_address[1]}"
    monkeypatch.setenv("OLLAMA_ENDPOINT", f"http://{host}:11434")
    for k in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(k, proxy)
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "127.0.0.1,localhost")


def _called(w, host):
    return [c for c in w.oll.calls if c[3].get("Host") == f"{host}:11434"]


def test_c7_on_the_cpu_judge_engine_the_embedder_fix_names_ollama_cpu(w, monkeypatch):
    _endpoint_names(w, monkeypatch, "ollama-cpu")
    monkeypatch.setenv("DENDRA_MINER_JUDGE", "1")
    w.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (500, b'{"error":"model not found"}')
    r = w.check("C7")
    assert r["state"] == "ko" and _called(w, "ollama-cpu"), (r, w.oll.calls)
    assert r["fix"] == "docker compose -p dendra-miner exec ollama-cpu ollama pull bench-embed", r["fix"]


def test_c7_on_the_cpu_judge_engine_a_failed_generation_names_ollama_cpu(w, monkeypatch):
    _endpoint_names(w, monkeypatch, "ollama-cpu")
    w.oll.routes[("POST", "/api/generate")] = lambda p, b: (404, b'{"error":"model not found"}')
    r = w.check("C7")
    assert r["state"] == "ko" and _called(w, "ollama-cpu"), (r, w.oll.calls)
    assert r["fix"] == ("docker compose -p dendra-miner logs --tail 50 ollama-cpu; "
                        "docker compose -p dendra-miner exec ollama-cpu ollama list"), r["fix"]


def test_c7_a_miner_on_a_card_that_judges_is_sent_to_ollama_not_to_the_judge_s_engine(w, monkeypatch):
    # The judge role is not the engine: a card's miner with --judge serves its model from `ollama`, and a fix
    # keyed on DENDRA_MINER_JUDGE would send it to pull into the judge's CPU instance instead.
    _endpoint_names(w, monkeypatch, "ollama")
    monkeypatch.setenv("DENDRA_MINER_JUDGE", "1")
    w.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (500, b'{"error":"model not found"}')
    r = w.check("C7")
    assert r["state"] == "ko" and _called(w, "ollama"), (r, w.oll.calls)
    assert r["fix"] == "docker compose -p dendra-miner exec ollama ollama pull bench-embed", r["fix"]


def test_c7_an_endpoint_that_is_no_kit_service_names_none(w):
    # The bench's own endpoint (127.0.0.1) is no kit service: the fix names the endpoint, and no compose command
    # that would act on an engine the miner does not call.
    ep = os.environ["OLLAMA_ENDPOINT"]
    w.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (500, b'{"error":"boom"}')
    r = w.check("C7")
    assert r["state"] == "ko" and ep in r["fix"] and "none of the kit's services" in r["fix"], r["fix"]
    assert "docker compose" not in r["fix"] and "bench-embed" in r["fix"], r["fix"]
    w.oll.routes[("POST", "/api/generate")] = lambda p, b: (404, b'{"error":"model not found"}')
    r = w.check("C7")
    assert r["state"] == "ko" and ep in r["fix"] and "docker compose" not in r["fix"], r["fix"]


def test_c7_the_fix_follows_a_slot_k_compose_project(w, monkeypatch):
    # Slot 1's command written out in full, as deploy/testnet-miner/slots.sh::slot_argv builds it from the files
    # deploy/join.sh::gpu_slot_compose_files lists: -p, --env-file and one -f per file. A bare `-p` would read the
    # kit's .env and override, so slot 0's identity (slots.sh, fact (a)). The prefix starts with slot 0's project
    # name followed by `-g1`: a retargeting that matched the name without its boundary would corrupt it. Kept on
    # one line: a line naming a slot's project is judged on its own, and must carry --env-file and -f together.
    slot1 = "docker compose -p dendra-miner-g1 --env-file gpu/1/.env -f docker-compose.yml -f docker-compose.local-node.yml -f gpu/1/override.yml"
    _endpoint_names(w, monkeypatch, "ollama")
    w.oll.routes[("POST", "/api/embeddings")] = lambda p, b: (500, b'{"error":"model not found"}')
    r = w.check("C7", "--compose", slot1)
    assert r["fix"] == slot1 + " exec ollama ollama pull bench-embed", r["fix"]


@pytest.mark.parametrize("endpoint,service", [
    ("http://ollama:11434", "ollama"), ("http://ollama-cpu:11434", "ollama-cpu"), ("http://OLLAMA-CPU:11434", "ollama-cpu"),
    ("http://dendra-judge-cpu:11434", ""), ("http://ollama-cpu.example:11434", ""), ("http://localhost:11434", ""),
    ("", ""), (None, ""), ("not a url", ""), ("http://[::1", ""),
])
def test_c7_the_service_is_read_from_the_endpoint_and_never_guessed(endpoint, service):
    assert MS.model_service(endpoint) == service


# -- C8 the capacity registry -----------------------------------------------------------------------
def test_c8_a_fresh_attributed_line_is_ok(w):
    assert w.check("C8")["state"] == "ok"


def test_c8_no_line_stale_and_unattributed_are_ko(w):
    w.rows = []
    assert w.check("C8")["state"] == "ko"
    w.rows = [{"node_id": "m-bench", "stale": True, "registered_onchain": True}]
    assert "is stale" in w.check("C8")["measured"]
    w.rows = [{"node_id": "m-bench", "stale": False, "registered_onchain": False}]
    assert "not attributed" in w.check("C8")["measured"]


def test_c8_an_ABSENT_registered_onchain_is_unmeasured(w):
    # The registry is not proto3: it writes both flags on every row.
    w.rows = [{"node_id": "m-bench", "stale": False}]
    assert w.check("C8")["state"] == "unmeasured"


def test_c8_a_registry_down_is_unmeasured(w):
    w.reg.routes[("GET", "/capacity")] = lambda p, b: (500, b"{}")
    assert w.check("C8")["state"] == "unmeasured"


def test_c8_without_a_public_address_for_the_sender_is_ko_and_says_the_pay(w, monkeypatch):
    # The sender reads this environment: no public address, no report, and the season pays nothing. That is
    # measured, not unknown, and the application's red line speaks only for a ko.
    pay = "the season pays nothing to an identity without a signed capacity report"
    monkeypatch.delenv("DENDRA_CAPACITY_URL")
    r = w.check("C8", cap=False)
    assert r["state"] == "ko" and "local node" in r["measured"], r
    assert pay in r["reason"] and "DENDRA_CAPACITY_URL" in r["fix"], r
    # --capacity-url reads the registry, and the fix still says the sender has no address to post to
    w.rows = []
    r = w.check("C8")
    assert r["state"] == "ko" and "cannot send it" in r["fix"] and "by itself" not in r["fix"], r


def test_c8_reads_the_registry_the_sender_derives_from_a_public_node(w, monkeypatch):
    # A hand-written .env: DENDRA_CAPACITY_URL empty, DENDRA_NODE public. The sender posts to https://<host>/capacity
    # (capacity_sender.registry_url), so C8 reads there, never "no address".
    seen = []
    monkeypatch.setattr(MS, "http_get", lambda url, *a, **k: (seen.append(url), (200, json.dumps(
        {"nodes": [{"node_id": "m-bench", "stale": False, "registered_onchain": True}]})))[1])
    monkeypatch.setenv("DENDRA_CAPACITY_URL", "")
    monkeypatch.setenv("DENDRA_NODE", "tcp://rpc.bench.example:26657")
    r = w.check("C8", cap=False)
    assert r["state"] == "ok" and seen == ["https://rpc.bench.example/capacity"], (r, seen)


def test_c8_fix_follows_what_the_senders_last_round_can_do(w):
    w.rows = []
    status = w.tmp / "capacity-status.json"
    cases = [("no-inventory", "no hardware inventory at /data/keys/capacity-inventory.json", "cannot send it until"),
             ("no-key", "the signing key does not resolve", "cannot send it until"),
             ("other", "registered for the operator dendra1else", "cannot send it until"),
             ("absent", "the chain records no miner yet", "sends it by itself once the chain records"),
             ("refused", "HTTP 503", "tries again by itself"),
             ("sent", "accepted", "the last round of the miner's own sender was sent")]
    for outcome, why, gesture in cases:
        status.write_text(json.dumps({"outcome": outcome, "why": why, "at": int(time.time()) - 60}))
        r = w.check("C8")
        assert r["state"] == "ko" and gesture in r["fix"], (outcome, r)
        assert "publish-capacity.sh" in r["fix"] and "grep capacity" in r["fix"], (outcome, r)
        if gesture.startswith("cannot"):
            assert why in r["fix"] and "by itself" not in r["fix"], (outcome, r)


def test_c8_every_line_says_the_season_pays_nothing_without_the_report(w, monkeypatch):
    # The report decides the season's pay (final_season_calc.kit_allows_pay): each state of C8 says so, and none of
    # them sends the operator to a crontab, since the miner's own sender runs in its container.
    pay = "the season pays nothing to an identity without a signed capacity report"
    states = []
    for rows in ([], [{"node_id": "m-bench", "stale": True, "registered_onchain": True}],
                 [{"node_id": "m-bench", "stale": False, "registered_onchain": False}],
                 [{"node_id": "m-bench", "stale": False, "registered_onchain": True}]):
        w.rows = rows
        r = w.check("C8")
        states.append(r["state"])
        assert pay in r["reason"], r
        assert "crontab" not in r["fix"] and "hourly job" not in r["fix"], r
        if r["state"] == "ko":
            assert "grep capacity" in r["fix"] and "publish-capacity.sh" in r["fix"], r
    assert states == ["ko", "ko", "ko", "ok"]
    w.rows = []
    monkeypatch.delenv("DENDRA_CAPACITY_URL")
    r = w.check("C8", cap=False)
    assert pay in r["reason"], r
    assert w.check("C8")["name"] == "signed capacity report"


def test_c8_names_what_the_miners_own_sender_did(w):
    w.rows = []
    assert "has reported no round yet" in w.check("C8")["reason"]
    (w.tmp / "capacity-status.json").write_text(json.dumps(
        {"outcome": "absent", "why": "the chain records no miner dm1x yet", "at": int(time.time()) - 600}))
    r = w.check("C8")
    assert "the miner's own sender, 10 min ago: absent (the chain records no miner dm1x yet)" in r["reason"], r
    # A status without a whole `at` is not a round: it is reported as none, never as a round of age zero.
    (w.tmp / "capacity-status.json").write_text(json.dumps({"outcome": "sent", "at": "now"}))
    assert "has reported no round yet" in w.check("C8")["reason"]


def test_c9_a_dead_capacity_sender_is_ko_and_names_the_pay(w):
    w.set_procs("miner.py", "reveal_worker.py")
    r = w.check("C9")
    assert r["state"] == "ko" and "capacity_sender.py" in r["measured"], r
    assert "the season pays nothing to an identity without a signed capacity report" in r["reason"], r


# -- C9 processes and heartbeat ---------------------------------------------------------------------
def test_c9_the_daemon_and_its_reveal_worker_with_a_fresh_heartbeat_are_ok(w):
    assert w.check("C9")["state"] == "ok"


def test_c9_an_unreadable_commitment_journal_is_ko_with_its_path_and_the_way_out(w):
    # miner.py::_journal_for_pass writes journal_error and stops every pass: the loop turns, no job is served.
    now = int(time.time())
    path = "/data/keys/pending-commitments.json"
    w.heartbeat(written_at=now, loop_at=now, phase="loop",
                journal_error={"at": now, "path": path, "why": "JSONDecodeError"})
    r = w.check("C9")
    assert r["state"] == "ko" and path in r["measured"] and "no job is served" in r["measured"], r
    assert f"mv {path} {path}.unreadable" in r["fix"], r
    w.heartbeat(written_at=now, loop_at=now, phase="loop", journal_error=None)
    assert w.check("C9")["state"] == "ok"


def test_c9_a_commitment_journal_that_cannot_be_written_is_ko_and_never_told_to_be_moved(w):
    # miner.py::_journal_commitment: a write that fails deposits nothing and stops the pass. Moving the file
    # aside would not help, and would lose the commitments it holds.
    now = int(time.time())
    path = "/data/keys/pending-commitments.json"
    w.heartbeat(written_at=now, loop_at=now, phase="loop",
                journal_error={"at": now, "path": path, "kind": "write", "why": "OSError: No space left on device"})
    r = w.check("C9")
    assert r["state"] == "ko" and "cannot be written" in r["measured"] and "No space left" in r["measured"], r
    assert ".unreadable" not in r["fix"] and "writable" in r["fix"], r


def test_c9_a_dead_reveal_worker_is_ko(w):
    w.set_procs("miner.py")
    r = w.check("C9")
    assert r["state"] == "ko" and "reveal_worker.py" in r["measured"]


def test_c9_a_judge_is_expected_when_the_judge_role_is_on(w, monkeypatch):
    monkeypatch.setenv("DENDRA_MINER_JUDGE", "1")
    assert "judge_worker.py" in w.check("C9")["measured"]


def test_c9_a_stale_heartbeat_is_ko_and_an_absent_one_is_unmeasured(w):
    w.heartbeat(written_at=int(time.time()) - 100000, loop_at=0)
    assert w.check("C9")["state"] == "ko"
    w.hb.unlink()
    assert w.check("C9")["state"] == "unmeasured"


# -- C10 judge engine (one CPU judge per machine, reached by every identity of a multi-GPU rig) -----------
def _room(w, monkeypatch, available_mb=200000):
    """A machine with room for any model, as far as the memory guard (modea/memwatch.py) can read it: a fabricated /proc,
    and an engine that says what it holds (/api/ps: nothing) and lists the miner's model with its size. The guard checks the
    room before every call that could load the judge model, from this machine's own memory, so a case about something else
    must not depend on how much memory the host that runs the bench has free."""
    root = w.tmp / "memroot"
    (root / "proc" / "pressure").mkdir(parents=True, exist_ok=True)
    rows = [("MemTotal", available_mb + 4096), ("MemFree", available_mb // 2), ("MemAvailable", available_mb),
            ("Active(file)", 2048), ("SwapTotal", 0), ("SwapFree", 0)]
    (root / "proc" / "meminfo").write_text("".join(f"{k}: {v * 1024} kB" + chr(10) for k, v in rows))
    (root / "proc" / "vmstat").write_text("pswpout 0" + chr(10))
    (root / "proc" / "pressure" / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0" + chr(10)
                                                      + "full avg10=0.00 avg60=0.00 avg300=0.00 total=0" + chr(10))
    monkeypatch.setenv("DENDRA_MEMWATCH_PROC_ROOT", str(root))
    prior = w.oll.routes.get(("GET", "/api/tags"))
    mine = {"name": "bench-model", "model": "bench-model", "size": 5 * 1024 ** 3}

    def tags(p, b):
        code, body = prior(p, b) if prior else (200, _j({"models": []}))
        try:
            doc = json.loads(body)
            doc["models"] = list(doc.get("models") or []) + [mine]
            return code, _j(doc)
        except (ValueError, AttributeError):
            return code, body
    w.oll.routes[("GET", "/api/tags")] = tags
    w.oll.routes[("GET", "/api/ps")] = lambda p, b: (200, _j({"models": []}))
    w.oll.routes[("POST", "/api/show")] = lambda p, b: (200, _j({"model_info": {
        "general.architecture": "llama", "llama.block_count": 32, "llama.attention.head_count_kv": 8,
        "llama.attention.key_length": 128, "llama.attention.value_length": 128}}))


def _judge(w, monkeypatch, tags=None, code=200, pinned="qwen-judge"):
    """The judge role on, its endpoint the bench's Ollama, which answers /api/tags with `tags` (a document, or
    raw bytes), and the chain pinning `pinned` (None: the registry query fails)."""
    monkeypatch.setenv("DENDRA_MINER_JUDGE", "1")
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", f"http://127.0.0.1:{w.oll.server_address[1]}")
    monkeypatch.delenv("DENDRA_JUDGE_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("DENDRA_JUDGE_MODEL_ID", raising=False)
    body = tags if isinstance(tags, bytes) else _j(tags if tags is not None else {"models": [{"name": "qwen-judge:latest"}]})
    w.oll.routes[("GET", "/api/tags")] = lambda p, b: (code, body)
    if pinned is None:
        w.flag("modelregistry.fail")
    else:
        w.write("modelregistry.json", {"params": {"audit_judge_model": pinned}})


def test_c10_runs_only_when_the_judge_role_is_on(w):
    rc, out = w.run()
    doc = json.loads(out)
    assert {"id": "C10", "why": "the judge role is off (DENDRA_MINER_JUDGE)"} in doc["skipped"]
    assert "C10" not in [c["id"] for c in doc["checks"]] and rc == 0


def test_c10_the_engine_holds_the_model_the_chain_pins(w, monkeypatch):
    _judge(w, monkeypatch)
    r = w.check("C10")
    assert r["state"] == "ok" and "qwen-judge" in r["measured"] and "(chain)" in r["measured"]
    assert ("GET", "/api/tags") in [(c[0], c[1]) for c in w.oll.calls]


def test_c10_a_model_the_engine_does_not_hold_is_ko_and_its_remedy_names_slot_0(w, monkeypatch):
    _judge(w, monkeypatch, tags={"models": [{"name": "llama3.1:8b"}]})
    r = w.check("C10")
    assert r["state"] == "ko" and "does not hold qwen-judge" in r["measured"] and "mute" in r["reason"]
    assert "slots.sh run 0 logs judge-model-init" in r["fix"]


def test_c10_an_engine_that_does_not_answer_is_ko_never_unmeasured(w, monkeypatch):
    _judge(w, monkeypatch)
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "http://127.0.0.1:9")
    r = w.check("C10")
    assert r["state"] == "ko" and "does not answer" in r["measured"] and "mute jury seat" in r["reason"]


def test_c10_an_answer_that_cannot_be_read_is_unmeasured(w, monkeypatch):
    for tags, code in ((b"not json", 200), ({"models": "qwen-judge"}, 200), ([], 200), ({"models": []}, 500)):
        _judge(w, monkeypatch, tags=tags, code=code)
        assert w.check("C10")["state"] == "unmeasured", (tags, code)
    # a read list with no model in it is a reading: the engine holds nothing, ko
    _judge(w, monkeypatch, tags={"models": []})
    assert w.check("C10")["state"] == "ko"


def test_c10_the_model_is_the_one_the_worker_would_use(w, monkeypatch):
    # judge_worker.py::resolve_judge_model decides it: an explicit choice first, then the chain, then the kit.
    _judge(w, monkeypatch, tags={"models": [{"model": "chosen:7b"}]})
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_OVERRIDE", "chosen:7b")
    r = w.check("C10")
    assert r["state"] == "ok" and "(cli)" in r["measured"]
    _judge(w, monkeypatch, tags={"models": [{"name": "kit-model:latest"}]}, pinned=None)
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", "kit-model")
    r = w.check("C10")
    assert r["state"] == "ok" and "(env)" in r["measured"]


def test_a_slot_k_report_names_its_own_command_in_every_fix(w):
    # A slot k's self-test is told its compose command (miner_health.sh --slot k): no fix may point at slot 0's
    # project, whose remedy would act on another identity. The judge's remedies name slot 0 on purpose.
    w.set_procs("miner.py")
    w.write("miner_mode.json", "notfound")
    rc, out = w.run("--compose", "bash /kit/slots.sh run 2")
    doc = json.loads(out)
    texts = [c.get(k, "") for c in doc["checks"] + doc["advice"] for k in ("measured", "reason", "fix")]
    assert not [t for t in texts if "docker compose -p dendra-miner " in t]
    assert any("bash /kit/slots.sh run 2 restart miner" in t for t in texts)
    # without it, slot 0's command, as always
    rc, out = w.run()
    assert any("docker compose -p dendra-miner restart miner" in c["fix"] for c in json.loads(out)["checks"])
    with pytest.raises(SystemExit):
        MS.parse_args(["--compose", "a" + chr(10) + "b"])


def test_c9_the_heartbeat_bound_is_held_at_its_own_value(w):
    # The bound is derived (heartbeat.max_age_s), so the ages are too: a heartbeat a hundred thousand
    # seconds old would stay stale under a bound fifty times too wide.
    bound = MS.hb.max_age_s()
    w.heartbeat(written_at=int(time.time()) - 3 * bound, loop_at=0)
    assert w.check("C9")["state"] == "ko"
    w.heartbeat(written_at=int(time.time()) - (bound - 1), loop_at=0)
    assert w.check("C9")["state"] == "ok"


def test_c9_reports_the_commit_counters_as_read_and_never_invents_them(w):
    # The two counts the HiveOS stats show as `ar` (miner._COMMITS): reported, never judged.
    now = int(time.time())
    w.heartbeat(written_at=now, loop_at=now, commits_anchored=1, commits_refused=2)
    r = w.check("C9")
    assert r["state"] == "ok" and "1 anchored, 2 refused" in r["measured"]
    w.heartbeat(written_at=now, loop_at=now, commits_anchored=0, commits_refused=0)
    assert "0 anchored, 0 refused" in w.check("C9")["measured"], "a zero read is a reading"
    for partial in ({"commits_anchored": 3}, {"commits_anchored": True, "commits_refused": 0},
                    {"commits_anchored": "3", "commits_refused": 0}, {"commits_anchored": -1, "commits_refused": 0}):
        w.heartbeat(written_at=now, loop_at=now, **partial)
        r = w.check("C9")
        assert r["state"] == "ok" and "anchored" not in r["measured"], (
            "a heartbeat that does not carry both counters as whole numbers shows none, never a zero: %r" % partial)


# -- --heartbeat, the image's healthcheck -----------------------------------------------------------
def test_the_healthcheck_reads_only_the_heartbeat_s_age(w):
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 0
    w.heartbeat(written_at=int(time.time()) - 100000)
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 1
    w.hb.unlink()
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 1
    w.heartbeat(phase="loop")                                   # no written_at: malformed, never fresh
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 1


def test_the_healthcheck_bound_is_held_at_its_own_value(w):
    bound = MS.hb.max_age_s()
    w.heartbeat(written_at=int(time.time()) - 3 * bound)
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 1
    w.heartbeat(written_at=int(time.time()) - (bound - 1))
    assert w.run(mode="--heartbeat", ref=False, cap=False)[0] == 0


# -- the deadline and the lock, both inside the container -------------------------------------------
def test_the_deadline_stops_a_check_and_reports_the_rest_unmeasured(w):
    # C1 would wait 5 s for a block that never comes; the run was given 1 s. The check is stopped where it
    # runs -- a bound applied to `docker exec` from the host would leave it running in the container.
    w.climb = False
    t0 = time.monotonic()
    rc, out = w.run("--only", "C1,C2", "--height-wait", "5", "--deadline-s", "1")
    elapsed = time.monotonic() - t0
    doc = json.loads(out)
    c1, c2 = doc["checks"]
    assert rc == 2 and elapsed < 4, (rc, elapsed)
    assert c1["state"] == "unmeasured" and "deadline" in c1["measured"]
    assert c2["state"] == "unmeasured" and c2["measured"].startswith("not run")


def test_a_second_self_test_runs_nothing_while_one_holds_the_lock(w):
    import fcntl
    with open(os.environ["DENDRA_SELFTEST_LOCK"], "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out = w.run(mode="--host")
    assert rc == 2 and out.splitlines()[0].startswith("DENDRA_SELFTEST_BUSY ")
    assert not any(ln.startswith("DENDRA_SELFTEST_END ") for ln in out.splitlines())
    assert w.dendrad_calls() == [] and w.node.calls == []
    assert w.run()[0] == 0                                      # released: the next run goes on


# -- the readers, document by document --------------------------------------------------------------
def test_the_readers_keep_each_document_s_zero_policy():
    assert MS.status_fields({"sync_info": {"latest_block_height": "9"}}) == (9, None)
    assert MS.status_fields({"sync_info": {"latest_block_height": "9", "catching_up": False}}) == (9, False)
    assert MS.status_fields({}) == (None, None)
    assert MS.miner_fields({"miner": {"miner_id": "x"}}) == {"operator": "", "vrf_pubkey": "", "enc_pubkey": "", "stake": 0}
    assert MS.miner_fields({"error": "x"}) is None
    assert MS.tx_accepted({"height": "1"}) is True and MS.tx_accepted({"code": 0}) is True
    assert MS.tx_accepted({"code": 13}) is False
    with pytest.raises(MS.fsc.ChainUnreadable):
        MS.tx_accepted({"code": "abc"})
    assert MS.capacity_flags({"stale": False}) == (None, False)
    assert MS.capacity_flags({"stale": "no", "registered_onchain": True}) == (True, None)


# -- the keyring (modea/keyring.py), read by C3 ---------------------------------------------------------
PASSPHRASE = "q" * 64


def _encrypt(w, monkeypatch, with_passphrase=True):
    """The miner's keyring made `file` (keyring-file/ holds its key), its VRF secret sealed, and the
    passphrase file in place unless the case removes it."""
    from modea import crypto
    kdir = w.tmp / "kr" / "keyring-file"
    kdir.mkdir(parents=True)
    (kdir / "keyhash").write_text("h")
    (kdir / f"{MID}.info").write_text("x")
    pf = w.tmp / "keyring-passphrase"
    if with_passphrase:
        pf.write_text(PASSPHRASE + "\n")
    monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(pf))
    crypto.store_secret(str(w.keys / f"{MID}.vrf"), SK.encode(), PASSPHRASE, crypto.AAD_VRF)


def test_c3_an_encrypted_keyring_signs_with_its_passphrase_on_stdin(w, monkeypatch):
    _encrypt(w, monkeypatch)
    r = w.check("C3")
    assert r["state"] == "ok", r
    given = [json.loads(x) for x in (w.state / "keys_stdin.log").read_text().splitlines()]
    assert given and given[0] == (PASSPHRASE + chr(10)) * 2
    assert all(PASSPHRASE not in " ".join(c) for c in w.dendrad_calls()), "the passphrase is never in argv"
    # the sealed VRF secret was opened, never re-encrypted nor rewritten by the self-test
    assert (w.keys / f"{MID}.vrf").read_bytes().startswith(b"DENDRA-SKENC1")


def test_c3_a_keyring_that_does_not_open_is_ko_never_unmeasured(w, monkeypatch):
    # The miner cannot sign: a reading, not a failure to read -- and reported before the registration,
    # since a daemon that cannot open its keys stops before it registers.
    _encrypt(w, monkeypatch)
    w.flag("keyring_refused")
    w.write("miner_mode.json", "notfound")
    r = w.check("C3")
    assert r["state"] == "ko" and "cannot sign" in r["measured"] and "passphrase is missing or wrong" in r["reason"]


def test_c3_an_encrypted_keyring_without_its_passphrase_is_ko_and_dendrad_is_not_asked(w, monkeypatch):
    _encrypt(w, monkeypatch, with_passphrase=False)
    r = w.check("C3")
    assert r["state"] == "ko" and "passphrase missing" in r["reason"]
    assert not [c for c in w.dendrad_calls() if c[:2] == ["keys", "show"]]


def test_c3_a_sealed_vrf_secret_that_does_not_open_is_ko(w, monkeypatch):
    from modea import crypto
    crypto.store_secret(str(w.keys / f"{MID}.vrf"), SK.encode(), "another-passphrase-entirely", crypto.AAD_VRF)
    r = w.check("C3")
    assert r["state"] == "ko" and "encrypted and does not open" in r["measured"]


# -- advice: apart from the checks, and never in the exit code -----------------------------------------
def test_advice_never_changes_the_exit_code_and_is_said_apart(w, monkeypatch):
    # Keys unencrypted (the bench's keyring is the test one), the phrase on the volume, no payout declared:
    # three pieces of advice, and the run is still the healthy one -- rc 0, every check ok.
    (w.keys / "recovery-phrase.json").write_text("{}")
    rc, out = w.run()
    doc = json.loads(out)
    assert rc == 0 and doc["rc"] == 0 and doc["summary"] == {"ok": 9, "ko": 0, "unmeasured": 0, "checks": 9}
    assert [a["id"] for a in doc["advice"]] == ["A1", "A2", "A3"]
    assert all(a["fix"] for a in doc["advice"])
    assert "encrypt-keys.sh" in doc["advice"][0]["fix"] and "--payout-address" in doc["advice"][2]["fix"]
    rc, out = w.run(mode="--host")
    lines = out.splitlines()
    assert rc == 0 and lines[-1].startswith("DENDRA_SELFTEST_END rc=0 ok=9 ko=0 unmeasured=0 checks=9")
    assert any(ln.startswith("DENDRA_SELFTEST_TEXT   [advice] A1") for ln in lines)


def test_in_owner_mode_the_fixes_name_what_the_owner_does(w, monkeypatch):
    # Owner mode (deploy/join.sh --owner): the miner never registers itself, and the programme takes the
    # payout declaration from the owner's key only. Both fixes say so, instead of pointing at a step this
    # machine cannot take; the advice still never changes the exit code.
    monkeypatch.setenv("DENDRA_MINER_OWNER", "dendra1owner")
    rc, out = w.run()
    doc = json.loads(out)
    adv = {a["id"]: a for a in doc["advice"]}
    assert rc == 0 and "payout-prepare" in adv["A3"]["fix"] and "--payout-address" not in adv["A3"]["fix"]
    w.write("miner_mode.json", "notfound")
    r = w.check("C3")
    assert r["state"] == "ko" and "OWNER MODE" in r["fix"] and "registers itself" not in r["fix"]
    monkeypatch.delenv("DENDRA_MINER_OWNER")
    assert "registers itself" in w.check("C3")["fix"]


def test_no_advice_when_the_keys_are_sealed_the_phrase_gone_and_the_payout_declared(w, monkeypatch):
    _encrypt(w, monkeypatch)
    from modea import crypto
    for n, aad in ((f"{MID}.sk", crypto.AAD_SK), (f"{MID}.attestkey", crypto.AAD_ATTEST)):
        crypto.store_secret(str(w.keys / n), b"0" * 32, PASSPHRASE, aad)
    (w.keys / "payout-declared.json").write_text(json.dumps({"miner_id": MID, "address": "dendra1cold"}))
    rc, out = w.run("--only", "C3")
    doc = json.loads(out)
    assert doc["advice"] == [] and rc == 0
    # one key file still unencrypted under an encrypted keyring: advice, with the restart that seals it
    (w.keys / f"{MID}.sk").write_bytes(b"0" * 32)
    doc = json.loads(w.run("--only", "C3")[1])
    assert [a["id"] for a in doc["advice"]] == ["A1"] and "restart" in doc["advice"][0]["fix"]
    # a declaration recorded for another identity is not this one's
    (w.keys / "payout-declared.json").write_text(json.dumps({"miner_id": "dm1other", "address": "dendra1cold"}))
    assert "A3" in [a["id"] for a in json.loads(w.run("--only", "C3")[1])["advice"]]


# -- C11 the judge role this identity declares, and --judge-role, which publish-capacity.sh reads ----------
# The chain draws declared judges into a jury. The role this identity declares in its
# signed capacity report is read here, in its container: active (the worker runs, its engine holds the model that
# worker resolved at its start (its judge state) and that model is the chain's pin), mute (the worker or that
# model measured absent), off (not requested), unknown (not readable).
def _role(w, monkeypatch, worker=True, **kw):
    """The judge role on (see _judge), and judge_worker.py running beside the daemon unless `worker` is False --
    with the judge state it wrote at its start: the model the chain pins (`pinned`), read from the chain."""
    _judge(w, monkeypatch, **kw)
    # judge_worker.py keeps pid 102, which the cases below name; the capacity sender comes after it
    w.set_procs("miner.py", "reveal_worker.py", *(("judge_worker.py",) if worker else ()), "capacity_sender.py")
    if worker:
        w.judge_state(kw.get("pinned") or "qwen-judge", "chain")


def _probe(w, *args):
    """--judge-role: exactly two lines, exit 0, and the word."""
    rc, out = w.run(*args, mode="--judge-role", ref=False, cap=False)
    lines = out.splitlines()
    roles = [ln[len("DENDRA_JUDGE_ROLE "):] for ln in lines if ln.startswith("DENDRA_JUDGE_ROLE ")]
    why = [ln for ln in lines if ln.startswith("DENDRA_JUDGE_ROLE_WHY ")]
    assert rc == 0 and len(lines) == 2 and len(roles) == 1 and len(why) == 1, out
    return roles[0], why[0]


def test_judge_role_c11_is_skipped_when_the_role_is_off_neither_ko_nor_advice(w):
    rc, out = w.run()
    doc = json.loads(out)
    assert [s for s in doc["skipped"] if s["id"] == "C11" and "off" in s["why"]]
    assert "C11" not in [c["id"] for c in doc["checks"]] and rc == 0
    assert not [a for a in doc["advice"] if "judge" in json.dumps(a).lower()]


def test_judge_role_active_needs_the_worker_and_the_model(w, monkeypatch):
    _role(w, monkeypatch)
    r = w.check("C11")
    assert r["state"] == "ok" and r["measured"].startswith("active:") and "qwen-judge" in r["measured"]
    assert "only a verdict" in r["reason"]
    assert _probe(w)[0] == "active"
    _room(w, monkeypatch)                   # C7 calls the judge's engine too: the bench machine has room for the model
    # The role is on and the daemon's heartbeat carries no judge_memory: C12 cannot read the memory, and a run
    # that could not read it is not a green.
    rc, out = w.run()
    doc = json.loads(out)
    assert rc == 2 and [c["id"] for c in doc["checks"]] == [f"C{i}" for i in range(1, 13)]
    assert [c["state"] for c in doc["checks"] if c["id"] == "C12"] == ["unmeasured"]
    _beat(w)
    rc, out = w.run()
    doc = json.loads(out)
    assert rc == 0 and [c["id"] for c in doc["checks"]] == [f"C{i}" for i in range(1, 13)]


def test_judge_role_a_dead_worker_is_a_mute_seat_with_its_restart(w, monkeypatch):
    _role(w, monkeypatch, worker=False)
    r = w.check("C11")
    assert r["state"] == "ko" and r["measured"].startswith("MUTE:") and "judge_worker.py is not running" in r["measured"]
    assert "draws this identity into juries" in r["reason"]
    assert r["fix"] == "docker compose -p dendra-miner restart miner; then docker compose -p dendra-miner logs --tail 80 miner"
    assert _probe(w)[0] == "mute"


def test_judge_role_a_model_the_engine_lacks_is_a_mute_seat_and_the_fix_pulls_THAT_model(w, monkeypatch):
    _role(w, monkeypatch, tags={"models": [{"name": "llama3.1:8b"}]})
    r = w.check("C11")
    assert r["state"] == "ko" and "does not hold qwen-judge" in r["measured"]
    assert r["fix"] == "bash deploy/testnet-miner/slots.sh run 0 exec ollama-cpu ollama pull qwen-judge"
    assert _probe(w)[0] == "mute"


def test_judge_role_an_engine_that_is_down_is_a_mute_seat_and_the_fix_starts_slot_0_s_engine(w, monkeypatch):
    _role(w, monkeypatch)
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "http://127.0.0.1:9")
    r = w.check("C11")
    assert r["state"] == "ko" and r["fix"] == "bash deploy/testnet-miner/slots.sh run 0 --profile judge up -d ollama-cpu"
    assert _probe(w)[0] == "mute"


def test_judge_role_what_cannot_be_read_is_unknown_never_off_nor_active(w, monkeypatch):
    # the engine answers something that is not a list of models; the worker runs
    _role(w, monkeypatch, tags=b"not json")
    r = w.check("C11")
    assert r["state"] == "unmeasured" and r["measured"].startswith("unknown:")
    assert _probe(w)[0] == "unknown"
    # no process readable, the engine healthy: whether the worker runs is not known
    _role(w, monkeypatch)
    rc, out = w.run("--only", "C11", "--proc-root", str(w.tmp / "no-proc"))
    assert json.loads(out)["checks"][0]["state"] == "unmeasured"
    assert _probe(w, "--proc-root", str(w.tmp / "no-proc"))[0] == "unknown"


def test_judge_role_mute_wins_over_unknown_and_active_needs_every_part_read(w, monkeypatch):
    # unreadable processes and an engine that holds nothing: the seat is known to be silent
    _role(w, monkeypatch, tags={"models": []})
    assert _probe(w, "--proc-root", str(w.tmp / "no-proc"))[0] == "mute"
    # a dead worker and an unreadable engine: mute too
    _role(w, monkeypatch, worker=False, tags=b"not json")
    assert _probe(w)[0] == "mute"


def test_judge_role_off_asks_nothing_of_the_engine_nor_the_chain(w, monkeypatch):
    role, why = _probe(w)
    assert role == "off" and "DENDRA_MINER_JUDGE" in why
    assert w.oll.calls == [] and not [c for c in w.dendrad_calls() if c[:2] == ["query", "modelregistry"]]
    # The entrypoint starts judge_worker.py on "1" and on nothing else: every other value is the role off.
    for v in ("0", "", "yes", "true", " 1", "1 "):
        monkeypatch.setenv("DENDRA_MINER_JUDGE", v)
        assert _probe(w)[0] == "off", repr(v)


def test_judge_role_a_reading_that_raises_is_unknown_never_off(w, monkeypatch):
    _role(w, monkeypatch)

    def boom(*a):
        raise RuntimeError("bench")
    monkeypatch.setattr(MS, "judge_engine", boom)
    role, why = _probe(w)
    assert role == "unknown" and "RuntimeError" in why


def test_judge_role_a_reading_stopped_by_its_bound_is_unknown_never_mute(w, monkeypatch):
    # The engine takes longer than the bound: the alarm must not be read as "the engine does not answer" (a
    # measured absence, hence mute) by the reader it interrupts -- it is a reading that did not finish.
    _role(w, monkeypatch)

    def slow(p, b):
        time.sleep(3)
        return 200, _j({"models": [{"name": "qwen-judge:latest"}]})
    w.oll.routes[("GET", "/api/tags")] = slow
    monkeypatch.setattr(MS, "JUDGE_ROLE_DEADLINE_S", 1)
    t0 = time.monotonic()
    role, why = _probe(w)
    assert role == "unknown" and "not read within 1 s" in why and time.monotonic() - t0 < 3


def test_judge_role_c10_and_c11_judge_ONE_reading_of_the_engine(w, monkeypatch):
    _role(w, monkeypatch)
    rc, out = w.run("--only", "C9,C10,C11")
    doc = json.loads(out)
    assert [c["state"] for c in doc["checks"]] == ["ok", "ok", "ok"]
    assert [c[1] for c in w.oll.calls].count("/api/tags") == 1


def test_judge_role_a_slot_k_restarts_its_own_miner_and_pulls_on_slot_0(w, monkeypatch):
    _role(w, monkeypatch, worker=False)
    rc, out = w.run("--only", "C11", "--compose", "bash /kit/slots.sh run 2")
    assert json.loads(out)["checks"][0]["fix"] == \
        "bash /kit/slots.sh run 2 restart miner; then bash /kit/slots.sh run 2 logs --tail 80 miner"
    # the machine's one judge engine is slot 0's, whichever identity reports it
    _role(w, monkeypatch, tags={"models": []})
    rc, out = w.run("--only", "C11", "--compose", "bash /kit/slots.sh run 2")
    assert json.loads(out)["checks"][0]["fix"] == "bash deploy/testnet-miner/slots.sh run 0 exec ollama-cpu ollama pull qwen-judge"


# -- the model the running worker judges with, and the chain's pin -------------------------------------------
# judge_worker.py resolves its model once, at its start, and writes it in its judge state. A role declared from a
# model resolved again later (a fallback the chain did not confirm, a pin that moved since) would call active a
# seat that judges with another model. Each case below said "active" before the state existed.
def _unflag(w, name):
    p = w.state / name
    if p.exists():
        p.unlink()


def test_judge_role_a_fallback_the_chain_did_not_confirm_is_unknown_never_active(w, monkeypatch):
    # The worker started while the chain did not answer: it judges with the kit's model (env), which the engine
    # holds, and the pin still cannot be read. Whether that model is the pinned one is not known.
    _role(w, monkeypatch, tags={"models": [{"name": "kit-model:latest"}]}, pinned=None)
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", "kit-model")
    for source in ("env", "default"):
        w.judge_state("kit-model", source)
        r = w.check("C11")
        assert r["state"] == "unmeasured" and r["measured"].startswith("unknown:"), (source, r)
        assert f"its {source} fallback" in r["measured"] and "pin was not read" in r["measured"], r
        assert _probe(w)[0] == "unknown", source
    # the engine does hold it: C10, the engine's check, is ok -- what stays unknown is the role
    assert w.check("C10")["state"] == "ok"
    # the chain answers and pins that very model: the fallback is the pin, and the role is active
    _unflag(w, "modelregistry.fail")
    w.write("modelregistry.json", {"params": {"audit_judge_model": "kit-model"}})
    w.judge_state("kit-model", "env")
    assert _probe(w)[0] == "active"
    # the chain pins another model: unknown, and the remedy restarts the worker so that it reads the pin
    w.write("modelregistry.json", {"params": {"audit_judge_model": "qwen-judge"}})
    r = w.check("C11")
    assert r["state"] == "unmeasured" and "the chain pins qwen-judge; judge_worker.py judges with kit-model" in r["measured"]
    assert r["fix"].startswith("docker compose -p dendra-miner restart miner"), r
    assert _probe(w)[0] == "unknown"


def test_judge_role_the_model_is_the_running_worker_s_own_never_one_resolved_now(w, monkeypatch):
    # The worker resolved old-judge at its start (that day's pin); the chain now pins qwen-judge, which is all the
    # engine holds. A second resolution would have said active: the worker judges with old-judge, and gets nothing.
    _role(w, monkeypatch)
    w.judge_state("old-judge", "chain")
    c10 = w.check("C10")
    assert c10["state"] == "ko" and "does not hold old-judge" in c10["measured"], c10
    assert "resolved at its start" in c10["measured"], c10
    r = w.check("C11")
    assert r["state"] == "ko" and r["fix"] == "bash deploy/testnet-miner/slots.sh run 0 exec ollama-cpu ollama pull old-judge"
    assert _probe(w)[0] == "mute"
    # the engine holds old-judge as well: the seat votes, with a model the chain no longer pins -- unknown, restart
    _role(w, monkeypatch, tags={"models": [{"name": "qwen-judge:latest"}, {"name": "old-judge:latest"}]})
    w.judge_state("old-judge", "chain")
    r = w.check("C11")
    assert r["state"] == "unmeasured" and "the chain pins qwen-judge; judge_worker.py judges with old-judge" in r["measured"]
    assert "restart miner" in r["fix"]
    # the pin cannot be read now: a model the worker took from the chain at its start is the pin it read
    w.flag("modelregistry.fail")
    role, why = _probe(w)
    assert role == "active" and "as judge_worker.py read it at its start" in why, why


def test_judge_role_a_state_that_is_not_the_running_worker_s_is_never_read(w, monkeypatch):
    # Everything else is right -- the worker runs, the engine holds the pin, the chain pins it: only the state is
    # wrong, and resolving the model now would have said active every time.
    def unknown(part):
        r = w.check("C11")
        assert r["state"] == "unmeasured" and part in r["measured"], (part, r)
        assert _probe(w)[0] == "unknown", part

    _role(w, monkeypatch)
    assert _probe(w)[0] == "active"
    w.judge_state_file().unlink()
    unknown("no judge state at")
    w.judge_state(pid=100)                  # the daemon's process
    unknown("which is not judge_worker.py")
    w.judge_state(pid=4242)                 # no such process
    unknown("which does not run")
    w.judge_state(starttime=1)              # this pid's judge_worker.py started after the state was written
    unknown("a pid reused")
    for bad in ({"pid": True}, {"pid": 0}, {"pid": "102"}, {"starttime": None}, {"starttime": "9002"},
                {"model": ""}, {"model": None}, {"source": "banana"}, {"source": None}):
        w.judge_state(**bad)
        unknown("is not one this file recognises")
    w.judge_state_file().write_text("not json")
    unknown("judge state unreadable at")
    w.judge_state_file().write_text("[]")
    unknown("is not a JSON object")
    w.judge_state()
    (w.proc / "102" / "stat").unlink()
    unknown("the judge state cannot be tied to it")


def test_judge_role_an_explicit_choice_is_active_without_asking_the_chain(w, monkeypatch):
    _role(w, monkeypatch, tags={"models": [{"model": "chosen:7b"}]}, pinned=None)
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_OVERRIDE", "chosen:7b")
    w.judge_state("chosen:7b", "cli")
    role, why = _probe(w)
    assert role == "active" and "explicit choice" in why, why
    assert not [c for c in w.dendrad_calls() if c[:2] == ["query", "modelregistry"]]


def test_judge_role_a_chain_that_pins_nothing_is_not_a_chain_nobody_read(w, monkeypatch):
    # proto3 omits an empty string: an absent audit_judge_model is the empty pin. It is reported as such, and it is not
    # the answer of a query that failed -- which leaves standing the pin the worker read at its start.
    _role(w, monkeypatch)
    w.write("modelregistry.json", {"params": {}})
    r = w.check("C11")
    assert r["state"] == "unmeasured" and "pins no judge model" in r["measured"], r
    w.flag("modelregistry.fail")
    role, why = _probe(w)
    assert role == "active" and "as judge_worker.py read it at its start" in why and "pins no judge model" not in why


def test_judge_role_the_start_time_is_counted_after_the_LAST_parenthesis():
    assert MS.hb.proc_starttime(_stat_line(7, 123456)) == 123456
    assert MS.hb.proc_starttime(_stat_line(7, 55, comm="a) b (c) S 1")) == 55
    for bad in ("", "7 python3 S 0", _stat_line(7, 1).rsplit(" ", 3)[0], "7 (x) S 0", None):
        assert MS.hb.proc_starttime(bad) is None, bad


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs a Linux /proc")
def test_judge_role_the_real_worker_writes_the_state_the_self_test_reads(w, monkeypatch):
    """judge_worker.py started as a process (the write sits in its main(), which no import crosses) up to its
    judge state: the model the fake chain pins, its own pid and its own start time; then the self-test reads it
    back against the real /proc, while the worker runs and once it is gone."""
    import subprocess
    from modea import crypto
    keys = w.tmp / "jw-keys"
    keys.mkdir()
    crypto.save_sk(crypto.gen_keypair()[0], str(keys / f"{MID}.sk"))
    w.write("modelregistry.json", {"params": {"audit_judge_model": "qwen-judge"}})
    state = w.tmp / "jw-state.json"
    # The worker under test can be a mutated copy (DENDRA_JUDGE_WORKER_FILE): it then imports the tree's modules.
    subject = os.environ.get("DENDRA_JUDGE_WORKER_FILE") or os.path.join(MODEA, "judge_worker.py")
    env = dict(os.environ, DENDRA_JUDGE_STATE_FILE=str(state), HOME=str(w.tmp), PYTHONDONTWRITEBYTECODE="1",
               PYTHONUNBUFFERED="1", PYTHONPATH=MODEA)
    env.pop("DENDRA_PUBLIC", None)
    log = open(w.tmp / "jw.log", "w")
    p = subprocess.Popen([sys.executable, subject, "--id", MID, "--relay",
                          f"http://127.0.0.1:{w.relay.server_address[1]}", "--keydir", str(keys)],
                         env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        # until the state is there, the worker has gone past the line that writes it (it prints its verdict
        # endpoint next), or it has exited -- never a fixed sleep
        end = time.monotonic() + 60
        while (time.monotonic() < end and p.poll() is None and not state.exists()
               and "verdict endpoint" not in (w.tmp / "jw.log").read_text(errors="replace")):
            time.sleep(0.2)
        time.sleep(0.2)
        out = (w.tmp / "jw.log").read_text(errors="replace")
        assert state.exists(), f"the worker wrote no judge state (rc={p.poll()}): {out[-800:]}"
        doc = json.loads(state.read_text())
        assert (doc["model"], doc["source"], doc["pid"]) == ("qwen-judge", "chain", p.pid), doc
        with open(f"/proc/{p.pid}/stat") as f:
            assert doc["starttime"] == MS.hb.proc_starttime(f.read())
        monkeypatch.setenv("DENDRA_JUDGE_STATE_FILE", str(state))
        assert MS.judge_worker_model("/proc") == ("qwen-judge", "chain", None)
    finally:
        p.kill()
        p.wait()
        log.close()
    model, source, why = MS.judge_worker_model("/proc")
    assert model is None and "does not run" in why, why


# -- C12 judge memory, C7 behind the memory guard, and --judge-memory --------------------------------------
# The block under test is the one the daemon's memory watch writes into its heartbeat (key `judge_memory`, the
# fields of the interface the memory watch and this self-test share). The bench writes it field by field and does
# not import the module that writes it: a key renamed on one side turns these cases red.
def _jm(**over):
    now = int(time.time())
    jm = {"at": now - 3, "ceiling": "resident", "mode": "resident", "mode_since": now - 600, "mode_why": "bench",
          "gate": "open", "gate_why": "room for the model", "resident": True, "footprint_mb": 19600,
          "footprint_source": "engine-ps", "load_s": 41.5, "mem_total_mb": 64000, "mem_available_mb": 40000,
          "active_file_ref_mb": 3000, "swap_total_mb": 8000, "swap_used_mb": 0, "swap_out_pages_period": 0,
          "psi_some_avg10": 0.0, "psi_full_avg10": 0.0, "psi_some_avg300": 0.0, "unloads": 0, "oom_seen": 0,
          "pause_until": None}
    jm.update(over)
    return jm


def _beat(w, **over):
    """A fresh heartbeat that carries a judge_memory block with `over` applied."""
    now = int(time.time())
    w.heartbeat(written_at=now - 5, loop_at=now - 5, phase="loop", judge_memory=_jm(**over))


def _beat_without(w, **extra):
    """A fresh heartbeat with no judge_memory key at all (or the odd value `extra` gives it)."""
    now = int(time.time())
    w.heartbeat(written_at=now - 5, loop_at=now - 5, phase="loop", **extra)


def _on(monkeypatch):
    monkeypatch.setenv("DENDRA_MINER_JUDGE", "1")


def test_c12_is_skipped_when_the_judge_role_is_off(w):
    _beat(w)
    rc, out = w.run()
    doc = json.loads(out)
    assert {"id": "C12", "why": "the judge role is off (DENDRA_MINER_JUDGE)"} in doc["skipped"]
    assert "C12" not in [c["id"] for c in doc["checks"]] and rc == 0


def test_c12_a_moving_watch_that_took_nothing_out_of_memory_is_ok_and_says_the_mode(w, monkeypatch):
    _on(monkeypatch)
    _beat(w)
    r = w.check("C12")
    assert r["state"] == "ok", r
    for part in ("mode resident (ceiling resident)", "gate open (room for the model)", "model loaded: yes",
                 "footprint 19600 MB (engine-ps)", "memory available 40000 MB of 64000 MB", "swap used 0 MB of 8000 MB",
                 "pressure some/full avg10 0/0", "unloads 0, out-of-memory 0"):
        assert part in r["measured"], (part, r["measured"])


def test_c12_a_closed_gate_is_the_guard_working_not_a_fault(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", gate="closed", gate_why="available memory 9000 MB is under the 19600 MB the model needs",
          resident=False)
    r = w.check("C12")
    assert r["state"] == "ok" and "gate closed (available memory 9000 MB" in r["measured"], r
    assert "model loaded: no" in r["measured"] and "abstention for memory, not a fault" in r["reason"], r


def test_c12_an_emergency_unload_or_an_out_of_memory_error_is_ko_and_the_first_run_counts_from_the_daemon_start(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, unloads=2, oom_seen=1)
    r = w.check("C12")
    assert r["state"] == "ko", r
    assert r["measured"].startswith("2 emergency unload(s) and 1 out-of-memory error(s) since the daemon started"), r["measured"]
    assert "judge.sh status" in r["fix"] and "DENDRA_JUDGE_MODE_CHOICE=on-demand" in r["fix"], r["fix"]
    # the next full run counts from what that run read: the same counters are not a new emergency
    assert w.check("C12")["state"] == "ok"
    # ... and one more unload is one
    _beat(w, unloads=3, oom_seen=1)
    r = w.check("C12")
    assert r["state"] == "ko" and r["measured"].startswith("1 emergency unload(s) and 0 out-of-memory error(s) since the last full self-test"), r


def test_c12_an_out_of_memory_error_alone_is_ko(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, oom_seen=1)
    r = w.check("C12")
    assert r["state"] == "ko" and r["measured"].startswith("0 emergency unload(s) and 1 out-of-memory error(s)"), r


def test_c12_a_quick_run_reads_the_baseline_and_never_moves_it(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, unloads=1)
    assert w.check("C12", "--quick")["state"] == "ko"
    assert not os.path.exists(os.environ["DENDRA_SELFTEST_JUDGE_MEMORY_BASELINE"])
    assert w.check("C12", "--quick")["state"] == "ko"      # the quick run did not consume it
    assert w.check("C12")["state"] == "ko"                 # the full run still sees it ...
    assert w.check("C12")["state"] == "ok"                 # ... once


def test_c12_a_counter_below_its_baseline_is_a_restarted_daemon_and_all_of_it_is_new(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, unloads=5)
    assert w.check("C12")["state"] == "ko"
    _beat(w, unloads=0)
    assert w.check("C12")["state"] == "ok"
    _beat(w, unloads=2)
    r = w.check("C12")
    assert r["state"] == "ko" and r["measured"].startswith("2 emergency unload(s)"), r


def test_c12_a_missing_block_is_unmeasured_never_ok_while_the_role_is_requested(w, monkeypatch):
    _on(monkeypatch)
    _beat_without(w)
    r = w.check("C12")
    assert r["state"] == "unmeasured" and "carries no judge_memory" in r["measured"], r
    w.hb.unlink()
    r = w.check("C12")
    assert r["state"] == "unmeasured" and "heartbeat was not read" in r["measured"], r
    _beat_without(w, judge_memory="healthy")
    r = w.check("C12")
    assert r["state"] == "unmeasured" and "not an object" in r["measured"], r


def test_c12_a_block_without_a_readable_time_or_an_old_one_is_unmeasured(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, at=None)
    assert w.check("C12")["state"] == "unmeasured"
    bound = MS.hb.max_age_s()
    _beat(w, at=int(time.time()) - bound - 10)
    r = w.check("C12")
    assert r["state"] == "unmeasured" and "the memory watch is not moving" in r["measured"], r


def test_c12_counters_that_are_absent_are_not_zero(w, monkeypatch):
    _on(monkeypatch)
    for k in ("unloads", "oom_seen"):
        _beat(w, **{k: None})
        r = w.check("C12")
        assert r["state"] == "unmeasured" and "no readable unloads or oom_seen" in r["measured"], (k, r)


def test_c12_a_kernel_without_pressure_readings_is_said(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, psi_some_avg10=None, psi_full_avg10=None, psi_some_avg300=None)
    r = w.check("C12")
    assert r["state"] == "ok" and "pressure not read" in r["measured"], r
    assert "two of the unload triggers are off" in r["reason"], r


def test_c12_a_baseline_that_cannot_be_kept_is_said(w, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("DENDRA_SELFTEST_JUDGE_MEMORY_BASELINE", str(w.tmp / "no-such-dir" / "b.json"))
    _beat(w, unloads=1)
    r = w.check("C12")
    assert r["state"] == "ko" and "could not be kept" in r["reason"], r


def test_c12_runs_in_the_whole_report_when_the_role_is_on(w, monkeypatch):
    _role(w, monkeypatch)
    _beat(w)
    rc, out = w.run()
    doc = json.loads(out)
    assert [c["id"] for c in doc["checks"]][-2:] == ["C11", "C12"] and doc["rc"] == rc


def test_c12_the_slot_k_fix_names_its_own_compose_project(w, monkeypatch):
    _on(monkeypatch)
    _beat_without(w)
    slot1 = "docker compose -p dendra-miner-g1 --env-file gpu/1/.env -f docker-compose.yml"
    r = w.check("C12", "--compose", slot1)
    assert r["fix"].startswith(slot1 + " restart miner") and "-p dendra-miner restart" not in r["fix"], r["fix"]


# -- C12 and the judge that stays shut: a closed gate that cannot pass, one that lasts, a judge the chain stopped drawing
def _seat_block(missed=0, state="declared", age=30, **over):
    """The `judge_declaration` block of the daemon's heartbeat (miner.py::JudgeSeat._note), `over` applied."""
    blk = {"at": int(time.time()) - age, "state": state, "role": "on", "draining": False, "sent": None, "why": "",
           "read": {"declared": state == "declared", "declared_height": 100, "last_verdict_height": 900, "missed_draws": missed}}
    blk.update(over)
    return blk


def _beat_seat(w, seat=None, **over):
    now = int(time.time())
    doc = dict(written_at=now - 5, loop_at=now - 5, phase="loop", judge_memory=_jm(**over))
    if seat is not None:
        doc["judge_declaration"] = seat
    w.heartbeat(**doc)


def _params(w, **p):
    w.write("params.json", {"params": {"avail_epoch_blocks": "100", "avail_deadline_blocks": "0", **p}})


def test_c12_a_gate_closed_by_the_container_limit_cannot_open_by_itself_and_is_ko(w, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "17408")
    _beat(w, mode="on-demand", gate="closed", resident=False, footprint_mb=17697, mem_available_mb=24000,
          gate_why="the model (17697 MiB) exceeds the judge container's limit (17408 MiB)")
    r = w.check("C12")
    assert r["state"] == "ko", r
    assert "cannot open by itself" in r["measured"] and "17697 MB" in r["measured"] and "17408 MB" in r["measured"], r
    assert "deploy/join.sh" in r["fix"], r["fix"]


def test_c12_a_gate_held_closed_by_a_lower_bound_cannot_open_by_itself_and_is_ko(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", gate="closed", resident=False, footprint_mb=17697, footprint_source="file",
          mem_available_mb=60000, gate_why="the model's footprint is known only as a lower bound (its file, 17697 MiB)")
    r = w.check("C12")
    assert r["state"] == "ko", r
    assert "cannot open by itself" in r["measured"] and "lower bound" in r["measured"] and "17697 MB" in r["measured"], r
    assert "DENDRA_JUDGE_MODEL_ID" in r["fix"], r["fix"]
    # The same figure as a full estimate is a gate that waits for room: the guard working, not a fault.
    _beat(w, mode="on-demand", gate="closed", resident=False, footprint_mb=18081, footprint_source="file+kv",
          mem_available_mb=9000, gate_why="9000 MiB free")
    assert w.check("C12")["state"] == "ok"


def test_c12_says_when_the_footprint_is_the_declared_figure(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", gate="open", resident=False, footprint_mb=18081, footprint_source="declared")
    r = w.check("C12")
    assert r["state"] == "ok" and "18081 MB (declared)" in r["measured"], r
    assert "declared for the judge model" in r["reason"] and "no context-cache metadata" in r["reason"], r
    _beat(w, mode="on-demand", gate="open", resident=False, footprint_mb=18081, footprint_source="file+kv")
    assert "declared for the judge model" not in w.check("C12")["reason"]


def test_c12_a_gate_closed_for_lack_of_room_is_ok_whatever_the_limit(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", gate="closed", resident=False, footprint_mb=17697, mem_available_mb=9000, gate_why="9000 MiB free")
    assert w.check("C12")["state"] == "ok"                                   # no limit set
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "17697")
    assert w.check("C12")["state"] == "ok"                                   # the model fits the limit exactly
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "many")
    assert w.check("C12")["state"] == "ok"                                   # a limit that is not a number is no limit
    _beat(w, mode="on-demand", gate="closed", resident=False, footprint_mb=None, gate_why="footprint not known")
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "100")
    assert w.check("C12")["state"] == "ok"                                   # an unknown footprint is compared with nothing


def test_c12_a_gate_closed_for_longer_than_the_verdict_window_is_ko(w, monkeypatch):
    _on(monkeypatch)
    _params(w, judge_verdict_window_blocks="1000")
    _beat(w, gate="closed", resident=False, gate_why="9000 MiB free")
    w.climb = False
    w.height = 5000
    assert w.check("C12")["state"] == "ok"                                   # first seen closed at height 5000
    w.height = 6000
    assert w.check("C12")["state"] == "ok"                                   # 1000 blocks: the chain's window, not past it
    w.height = 6001
    r = w.check("C12")
    assert r["state"] == "ko" and "closed for 1001 blocks" in r["measured"] and "judge_verdict_window_blocks (1000 blocks" in r["measured"], r
    # the room came back: the closing instant is forgotten, and a later closing counts from its own start
    _beat(w)
    assert w.check("C12")["state"] == "ok"
    _beat(w, gate="closed", resident=False, gate_why="9000 MiB free")
    w.height = 9000
    assert w.check("C12")["state"] == "ok"


def test_c12_the_verdict_window_falls_back_to_the_juror_freshness_and_then_to_the_compiled_one(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, gate="closed", resident=False, gate_why="9000 MiB free")
    w.climb = False
    w.height = 10
    assert w.check("C12")["state"] == "ok"
    w.height = 10 + MS.JUROR_FRESHNESS_BLOCKS_COMPILED                       # params at zero: the compiled 259200
    assert w.check("C12")["state"] == "ok"
    w.height += 1
    assert w.check("C12")["state"] == "ko"
    _params(w, juror_freshness_blocks="500")                                 # a posted freshness is the window at 0
    w.height = 10 + 501
    assert w.check("C12")["state"] == "ko"
    _params(w, juror_freshness_blocks="500", judge_verdict_window_blocks="600")
    assert w.check("C12")["state"] == "ok"


def test_c12_a_quick_run_reads_the_closing_instant_and_never_moves_it(w, monkeypatch):
    _on(monkeypatch)
    _params(w, judge_verdict_window_blocks="1000")
    _beat(w, gate="closed", resident=False, gate_why="9000 MiB free")
    w.climb = False
    w.height = 5000
    assert w.check("C12", "--quick")["state"] == "ok"
    w.height = 9000
    assert w.check("C12", "--quick")["state"] == "ok"                        # nothing was kept by the quick run
    assert w.check("C12")["state"] == "ok"                                   # the full run keeps height 9000
    w.height = 9500
    assert w.check("C12")["state"] == "ok"
    w.height = 10001
    assert w.check("C12", "--quick")["state"] == "ko"                        # and a quick run judges against it


def test_c12_how_long_the_gate_was_closed_is_not_judged_when_the_params_or_the_height_cannot_be_read(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, gate="closed", resident=False, gate_why="9000 MiB free")
    w.climb = False
    w.height = 10
    assert w.check("C12")["state"] == "ok"
    w.height = 10 ** 7
    (w.state / "params.fail").write_text("1")
    r = w.check("C12")
    assert r["state"] == "ok" and "not judged: the chain's params were not read" in r["reason"], r
    (w.state / "params.fail").unlink()
    w.node.routes[("GET", "/status")] = lambda p, b: (500, b"")
    r = w.check("C12")
    assert r["state"] == "ok" and "how long the gate has been closed is not judged" in r["reason"], r


def test_c12_a_judge_seated_on_the_limit_of_missed_draws_is_ko_and_the_fix_declares_it_again(w, monkeypatch):
    _on(monkeypatch)
    _params(w, judge_missed_draws_max="3")
    _beat_seat(w, _seat_block(missed=2))
    r = w.check("C12")
    assert r["state"] == "ok" and "missed draws 2 (the chain stops drawing it at 3)" in r["measured"], r
    _beat_seat(w, _seat_block(missed=3))
    r = w.check("C12")
    assert r["state"] == "ko" and "no longer draws this judge" in r["measured"] and "3 concluded audit(s)" in r["measured"], r
    assert "judge.sh off" in r["fix"] and "judge.sh on" in r["fix"] and "clears the count" in r["fix"], r["fix"]
    assert "the miner declares it again by itself" in r["fix"], r["fix"]
    _params(w, judge_missed_draws_max="1")
    _beat_seat(w, _seat_block(missed=1))
    assert w.check("C12")["state"] == "ko"


def test_c12_the_missed_draws_limit_at_its_zero_is_the_compiled_one_never_no_limit(w, monkeypatch):
    _on(monkeypatch)
    _params(w)                                                               # judge_missed_draws_max omitted: proto3 zero
    _beat_seat(w, _seat_block(missed=2))
    assert w.check("C12")["state"] == "ok"
    _beat_seat(w, _seat_block(missed=MS.JUDGE_MISSED_DRAWS_MAX_COMPILED))
    assert w.check("C12")["state"] == "ko"


def test_c12_missed_draws_with_unread_params_are_unmeasured_and_none_needs_no_params(w, monkeypatch):
    _on(monkeypatch)
    (w.state / "params.fail").write_text("1")
    _beat_seat(w, _seat_block(missed=1))
    r = w.check("C12")
    assert r["state"] == "unmeasured" and "1 missed draw(s) exclude this judge is not known" in r["reason"], r
    _beat_seat(w, _seat_block(missed=0))
    assert w.check("C12")["state"] == "ok"                                   # no missed draw: never excluded, whatever the limit


def test_c12_the_seat_is_shown_as_read_or_as_not_read_never_as_a_clean_count(w, monkeypatch):
    _on(monkeypatch)
    _beat_seat(w, _seat_block(state="not_declared"))
    r = w.check("C12")
    assert r["state"] == "ok" and "the chain holds no judge declaration" in r["measured"], r
    for blk in (None, _seat_block(age=MS.hb.max_age_s() + 10), _seat_block(state="unknown", why="the chain could not be read"),
                _seat_block(at=None), _seat_block(read={"declared": True}), _seat_block(read=None), "declared"):
        _beat_seat(w, blk)
        r = w.check("C12")
        assert r["state"] == "ok" and "seat: not read" in r["measured"] and "missed draws" not in r["measured"], (blk, r)


def test_the_seat_the_daemon_writes_is_the_seat_this_check_reads(monkeypatch):
    # The interface is the heartbeat: the daemon's own declaration loop writes the block that judge_seat_block reads.
    import miner as md
    import types
    cli = types.SimpleNamespace(has=lambda *a, **k: True, predates=lambda *a, **k: None)
    fields = {"declared": True, "declared_height": 40, "last_verdict_height": 77, "missed_draws": 2}
    seat = md.JudgeSeat("dm1x", cli, reader=lambda m: (md.JUDGE_DECLARED, fields, ""))
    seat.turn(md.EPOCH_APPLIED, True, False, True, now=0.0)
    state, got, why = MS.judge_seat_block({"judge_declaration": md._STATUS["judge_declaration"]})
    assert (state, got["missed_draws"], got["last_verdict_height"], got["declared_height"]) == ("declared", 2, 77, 40), (state, got, why)
    seat = md.JudgeSeat("dm1x", cli, reader=lambda m: (md.JUDGE_NOT_DECLARED, {"declared": False, "declared_height": 0,
                                                                              "last_verdict_height": 0, "missed_draws": 0}, ""))
    seat.turn(md.EPOCH_APPLIED, False, False, True, now=0.0)
    assert MS.judge_seat_block({"judge_declaration": md._STATUS["judge_declaration"]})[0] == "not_declared"
    seat = md.JudgeSeat("dm1x", cli, reader=lambda m: (md.JUDGE_READ_UNKNOWN, {}, "not read"))
    seat.turn(md.EPOCH_APPLIED, True, False, True, now=0.0)
    assert MS.judge_seat_block({"judge_declaration": md._STATUS["judge_declaration"]})[0] == "unread"


def test_the_block_the_memory_watch_writes_is_the_block_c12_reads(w, monkeypatch):
    # The interface is the heartbeat; this case lets the shipped memory watch write the block and C12 read it, so that a
    # key renamed on one side turns it red. The limit of the judge container is the one the watch compares the model with.
    from modea import memwatch as mwm
    _on(monkeypatch)
    now = time.time()
    conf = {"period_s": 5.0, "psi_some_pct": 10.0, "promote_after_s": 3600.0, "headroom_mb": 0, "mem_limit_mb": 17408,
            "ceiling": "resident", "host_kind": "desktop", "resident_keepalive": "1h", "ondemand_keepalive": "600s"}
    r = {"at": 0, "mem_total_mb": 65536, "mem_available_mb": 30000, "mem_free_mb": 15000, "active_file_mb": 2048,
         "swap_total_mb": 8192, "swap_used_mb": 0, "pswpout": 0, "psi_some_avg10": 0.0, "psi_full_avg10": 0.0,
         "psi_some_avg300": 0.0}
    # The footprint of a model not yet loaded is its file and its context cache, when the watch is given the cache.
    import inspect
    kv = {"kv_mb": 384} if "kv_mb" in inspect.signature(mwm.MemWatch.observe).parameters else {}
    m = mwm.MemWatch(conf, now=now)
    m.observe(r, loaded=False, file_mb=17697, now=now, **kv)
    doc = m.doc()
    assert doc["gate"] == "closed" and doc["footprint_mb"] > 17408 and "limit" in doc["gate_why"], doc
    w.heartbeat(written_at=int(now) - 5, loop_at=int(now) - 5, phase="loop", judge_memory=doc)
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "17408")
    res = w.check("C12")
    assert res["state"] == "ko" and "cannot open by itself" in res["measured"], res
    # the same machine with a limit that holds the model: the gate is open and the check is ok
    conf_ok = dict(conf, mem_limit_mb=24000)
    m = mwm.MemWatch(conf_ok, now=now)
    m.observe(r, loaded=False, file_mb=17697, now=now, **kv)
    doc = m.doc()
    assert doc["gate"] == "open", doc
    w.heartbeat(written_at=int(now) - 5, loop_at=int(now) - 5, phase="loop", judge_memory=doc)
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "24000")
    assert w.check("C12")["state"] == "ok"


def test_c12_a_memory_setting_written_wrong_is_ko_because_no_model_is_loaded_until_it_is_fixed(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", gate="closed", resident=False, settings_error="DENDRA_JUDGE_KEEPALIVE: '1 hour' is not a duration",
          gate_why="the memory settings are written wrong: DENDRA_JUDGE_KEEPALIVE: '1 hour' is not a duration")
    r = w.check("C12")
    assert r["state"] == "ko" and "memory settings are written wrong (DENDRA_JUDGE_KEEPALIVE" in r["measured"], r
    assert "judge.sh" in r["fix"] and ".env" in r["fix"], r["fix"]
    for fine in (None, "", "   "):
        _beat(w, settings_error=fine)
        assert w.check("C12")["state"] == "ok", fine
    _beat(w)                                                       # an older writer: no such key at all
    assert "settings_error" not in json.loads(w.hb.read_text())["judge_memory"]
    assert w.check("C12")["state"] == "ok"


def test_c7_a_call_the_memory_guard_refused_is_unmeasured_and_any_other_failure_is_still_ko(w, monkeypatch):
    # The call to the judge's engine passes the check before a load (modea/memwatch.py::Held): a refusal means the engine
    # was not asked, so the model was not measured, and a model that was not measured is not a broken one.
    from modea import memwatch as mwm
    _on(monkeypatch)

    def refuse(self, *a, **k):
        raise mwm.Held("the judge model is not loaded, and 9000 MiB free beside the warm cache, the model needs 19600 MiB")
    monkeypatch.setattr(MS.inference.OllamaBackend, "generate", refuse)
    r = w.check("C7")
    assert r["state"] == "unmeasured" and "the memory guard held the call to the judge's engine" in r["measured"], r
    assert "9000 MiB free" in r["measured"] and "check C12" in r["reason"], r

    def boom(self, *a, **k):
        raise RuntimeError("engine down")
    monkeypatch.setattr(MS.inference.OllamaBackend, "generate", boom)
    r = w.check("C7")
    assert r["state"] == "ko" and "did not answer a generation: RuntimeError" in r["measured"], r


def test_a_presence_attempt_held_by_the_memory_guard_is_shown_as_the_daemon_wrote_it(w):
    w.write("txs.json", [])
    now = int(time.time())
    w.heartbeat(written_at=now - 5, loop_at=now - 5, phase="loop",
                presence={"at": now - 30, "result": "held by the memory guard", "why": "available memory 9000 MB"})
    r = w.check("C4")
    assert r["state"] == "ko", r
    assert "held by the memory guard: available memory 9000 MB" in r["reason"], r["reason"]


# C7 on a miner without a card: its own generations go to the judge's engine, and a probe loads the model there.
def _no_card(w, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", os.environ["OLLAMA_ENDPOINT"])
    _room(w, monkeypatch)


def test_c7_does_not_load_the_model_while_the_guard_holds_the_engine_closed(w, monkeypatch):
    _no_card(w, monkeypatch)
    _beat(w, gate="closed", gate_why="available memory 9000 MB is under the 19600 MB the model needs")
    r = w.check("C7")
    assert r["state"] == "unmeasured" and "memory guard holds the judge's engine closed" in r["measured"], r
    assert "available memory 9000 MB" in r["measured"], r
    assert w.oll.calls == [], w.oll.calls


def test_c7_runs_as_before_when_the_gate_is_open_or_the_block_is_absent_or_old(w, monkeypatch):
    _no_card(w, monkeypatch)
    _beat(w)
    assert w.check("C7")["state"] == "ok"
    _beat_without(w)
    assert w.check("C7")["state"] == "ok"
    _beat(w, gate="closed", at=int(time.time()) - MS.hb.max_age_s() - 10)
    assert w.check("C7")["state"] == "ok"
    _beat(w, gate=None)
    assert w.check("C7")["state"] == "ok"
    assert [c for c in w.oll.calls if c[1] == "/api/generate"], w.oll.calls


def test_c7_a_miner_with_a_card_is_not_held_by_the_judge_s_gate(w, monkeypatch):
    # The judge's engine is another instance than the one this miner calls: its closed gate holds nothing here.
    _on(monkeypatch)
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", "http://ollama-cpu:11434")
    _beat(w, gate="closed", gate_why="no room")
    assert w.check("C7")["state"] == "ok"
    assert [c for c in w.oll.calls if c[1] == "/api/generate"], w.oll.calls


def _memory_probe(w, *args):
    rc, out = w.run(*args, mode="--judge-memory", ref=False, cap=False)
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 4, out
    prefixes = ("DENDRA_JUDGE_MODE ", "DENDRA_JUDGE_LOADED ", "DENDRA_JUDGE_MEMORY_ABSTENTIONS ", "DENDRA_JUDGE_MEMORY_WHY ")
    assert all(ln.startswith(p) for ln, p in zip(lines, prefixes)), out
    return tuple(ln[len(p):] for ln, p in zip(lines, prefixes))


def _ledger(w, abstentions=None, **over):
    """The juror's state with the counters JudgeLedger.doc writes (`abstentions`: stage -> count)."""
    counters = {"since": int(time.time()) - 3600, "abstentions": abstentions, "verdicts_anchored": {}, "verdicts_refused": 0}
    counters.update(over)
    w.judge_state_file().write_text(json.dumps({"schema": 1, "model": "qwen-judge", "source": "chain", "counters": counters}))


def test_the_memory_probe_says_none_when_the_judge_role_is_off(w):
    _beat(w)
    mode, loaded, ab, why = _memory_probe(w)
    assert (mode, loaded, ab) == ("none", "unknown", "?") and "not requested" in why


def test_the_memory_probe_reads_the_mode_the_load_and_the_guard_s_abstentions(w, monkeypatch):
    _on(monkeypatch)
    _beat(w, mode="on-demand", resident=False)
    _ledger(w, {"memory-guard": 4, "judge-error": 9})
    assert _memory_probe(w)[:3] == ("on-demand", "no", "4")
    _beat(w, mode="resident", resident=True)
    assert _memory_probe(w)[:3] == ("resident", "yes", "4")
    _beat(w, resident=None)
    assert _memory_probe(w)[:2] == ("resident", "unknown")


def test_the_memory_probe_counts_zero_only_from_a_ledger_it_could_read(w, monkeypatch):
    _on(monkeypatch)
    _beat(w)
    _ledger(w, {})                                   # a ledger that counted no abstention: zero is a reading
    assert _memory_probe(w)[2] == "0"
    _ledger(w, {"judge-error": 2})                   # a ledger without that stage: the stage never happened
    assert _memory_probe(w)[2] == "0"
    _ledger(w, {"memory-guard": "many"})             # a count that is not a number is not a reading
    assert _memory_probe(w)[2] == "?"
    _ledger(w, None)                                 # a ledger without its abstentions
    assert _memory_probe(w)[2] == "?"
    w.judge_state_file().unlink()                    # no judge state at all
    assert _memory_probe(w)[2] == "?"


def test_the_memory_probe_never_calls_an_unread_mode_none_or_a_mode(w, monkeypatch):
    _on(monkeypatch)
    _beat_without(w)
    assert _memory_probe(w)[:2] == ("unknown", "unknown")
    w.hb.unlink()
    mode, loaded, ab, why = _memory_probe(w)
    assert (mode, loaded) == ("unknown", "unknown") and "heartbeat was not read" in why, why
    _beat(w, mode="whatever")
    assert _memory_probe(w)[0] == "unknown"
    _beat(w, at=int(time.time()) - MS.hb.max_age_s() - 10)
    mode, _l, _a, why = _memory_probe(w)
    assert mode == "unknown" and "not moving" in why, why


def test_the_memory_probe_survives_a_reading_that_raises(w, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(MS, "judge_memory_facts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    mode, loaded, ab, why = _memory_probe(w)
    assert (mode, loaded, ab) == ("unknown", "unknown", "?") and "RuntimeError: boom" in why


def test_c3_failed_vrf_command_is_unmeasured_even_with_public_key_output(w):
    binary = w.tmp / 'bin' / 'dendra-vrf'
    binary.write_text('#!/usr/bin/env python3\nprint(' + repr(PK) + ')\nraise SystemExit(1)\n')
    r = w.check('C3')
    assert r['state'] == 'unmeasured'
