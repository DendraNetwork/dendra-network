"""Bench of miner_selftest.py, the check that runs inside the miner container.

What is REAL: the node's RPC, the network's RPC, the relay, the capacity registry and Ollama are local HTTP
servers that answer what each case sets; the self-test reaches them through its own code (the redirect-
refusing opener of final_season_chain, relay_client, modea.inference). What is REPLACED: `dendrad` and
`dendra-vrf`, by scripts in a HERMETIC PATH (that directory and nothing else), which answer from the case's
state -- the fake node answers a transaction search WITH the bounds it was asked, so a search that asks the
wrong window gets the wrong answer --, /proc, by a directory of cmdline files, and the relay signature, by a
recorder: signing is relay_client's and benched elsewhere; what is pinned here is that the self-test signs,
with height 0, the exact bytes it sends.

Every check is driven to its three states. The rule of zero is pinned document by document: an absent
`vrf_pubkey` is ko (proto3: the empty key), an absent `avail_epoch_blocks` is presence disarmed, an absent
transaction `code` is accepted -- while CometBFT's `catching_up` and the registry's `registered_onchain`,
which are not proto3, are unmeasured when absent, never false.

The subject can be pointed at a mutated copy with DENDRA_SELFTEST_FILE (the mutations of
dendra/onchain-staging/dendra_mineur_sante_test.sh).
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
        (self.keys / "identite-resolue").write_text(MID + "\n")
        (self.keys / f"{MID}.vrf").write_text(SK)
        self.proc = tmp / "proc"
        self.set_procs("miner.py", "reveal_worker.py")
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
        self.pub_stored = json.dumps({"pub": ENC}).encode()
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
               "DENDRA_JUDGE_STATE_FILE": str(tmp / "judge-state.json")}
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


# ── the whole run ──────────────────────────────────────────────────────────────────────────────────
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


# ── the aggregate ──────────────────────────────────────────────────────────────────────────────────
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


# ── C1 the node RPC ────────────────────────────────────────────────────────────────────────────────
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


# ── C2 caught up ───────────────────────────────────────────────────────────────────────────────────
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
    # No bound is published (avail_epoch_blocks 0), so the lag is not judged -- but the node's OWN flag is:
    # a node that says it is behind is behind, whatever the chain publishes.
    w.write("params.json", {"params": {}})
    w.catching_up = True
    r = w.check("C2")
    assert r["state"] == "ko" and r["measured"].startswith("catching up") and "the node itself says it is behind" in r["reason"]


# ── C3 registration and keys ───────────────────────────────────────────────────────────────────────
def test_c3_registered_with_the_key_held_here_is_ok(w):
    r = w.check("C3")
    assert r["state"] == "ok" and ADDR in r["measured"]


def test_c3_the_vrf_secret_never_goes_through_argv(w):
    w.check("C3")
    calls = (w.state / "vrf.log").read_text()
    assert SK not in calls and json.loads(calls.splitlines()[0]) == ["pubkey"]


def test_c3_an_ABSENT_vrf_pubkey_is_ko_not_unmeasured(w):
    # proto3 omits the empty string: an absent key IS no key, and that is a reading.
    w.write("miner.json", {"miner": {"miner_id": MID, "operator": ADDR, "stake": "5000000", "enc_pubkey": ENC}})
    r = w.check("C3")
    assert r["state"] == "ko" and "NO VRF key anchored" in r["measured"]


def test_c3_notfound_is_not_registered_and_a_dead_node_is_unmeasured(w):
    # Two opposite facts that a lazy reader renders alike: the chain SAYS the miner is absent, versus
    # nothing answered at all.
    w.write("miner_mode.json", "notfound")
    r = w.check("C3")
    assert r["state"] == "ko" and "NOT registered" in r["measured"]
    (w.state / "miner_mode.json").unlink()
    w.write("miner_mode.json", "transport")
    MS_r = w.check("C3")
    assert MS_r["state"] == "unmeasured" and "connection refused" in MS_r["reason"]


def test_c3_a_key_mismatch_is_ko_and_names_the_rotation(w):
    w.write("vrf_pairs.json", {SK: "11" * 32})
    r = w.check("C3")
    assert r["state"] == "ko" and "MISMATCH" in r["measured"] and r["fix"].endswith("--new-vrf-pubkey " + "11" * 32)


def test_c3_no_vrf_secret_on_this_node_is_ko(w):
    (w.keys / f"{MID}.vrf").unlink()
    assert w.check("C3")["state"] == "ko"


def test_c3_another_operator_is_ko(w):
    w.write("address.json", "dendra1someoneelse")
    r = w.check("C3")
    assert r["state"] == "ko" and "dendra1someoneelse" in r["measured"]


def test_c3_on_a_slot_k_never_offers_slot_0_s_new_identity(w):
    # `deploy/join.sh --id` names SLOT 0's identity: printed in a slot k's report it would act on another
    # identity. A slot k's remedy is its own key, restored into its own keyring.
    w.write("address.json", "dendra1someoneelse")
    r = w.check("C3", "--compose", "bash /kit/slots.sh run 1")
    assert r["state"] == "ko" and "--id <new-id>" not in r["fix"]
    assert "keyring of this slot" in r["fix"] and "names slot 0 only" in r["fix"]
    # slot 0 keeps the remedy it always had
    assert "deploy/join.sh --id <new-id>" in w.check("C3")["fix"]


# ── C4 presence ────────────────────────────────────────────────────────────────────────────────────
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
    assert r["state"] == "ko" and "ABSENT" in r["measured"] and "invalid VRF proof" in r["reason"]


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


# ── C5 the work queue ──────────────────────────────────────────────────────────────────────────────
def test_c5_a_readable_queue_is_ok(w):
    r = w.check("C5")
    assert r["state"] == "ok" and "1 request(s) for this miner" in r["measured"]


def test_c5_a_refused_queue_is_ko(w):
    w.relay.routes[("GET", "/list")] = lambda p, b: (401, b'{"error":"unauthorized"}')
    assert w.check("C5")["state"] == "ko"


def test_c5_an_unreachable_relay_is_ko(w, monkeypatch):
    monkeypatch.setenv("DENDRA_RELAY", "http://127.0.0.1:1")
    assert w.check("C5")["state"] == "ko"


# ── C6 the relay key copy and the signed write ─────────────────────────────────────────────────────
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
        assert r["state"] == "ko" and "DIFFERENT key" in r["measured"]
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
    assert [p[2] for p in w.posts()] == [json.dumps({"pub": ENC}).encode()]


def test_c6_a_deposit_that_cannot_be_signed_is_NEVER_sent(w):
    # An unsigned write is attributable to nobody: a relay that still accepts one would take it, and this
    # check would have caused it. Nothing goes out, and the verdict says so.
    w.sign_ok = False
    for stored in (w.pub_stored, None):
        w.pub_stored = stored
        r = w.check("C6", "--write")
        assert r["state"] == "ko" and "could NOT sign" in r["measured"] and "nothing was written" in r["measured"]
    assert w.posts() == []


def test_c6_a_relay_that_does_not_answer_is_unmeasured(w, monkeypatch):
    monkeypatch.setenv("DENDRA_RELAY", "http://127.0.0.1:1")
    assert w.check("C6")["state"] == "unmeasured"


# ── C7 the model ───────────────────────────────────────────────────────────────────────────────────
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


# C7's fixes name the service the miner CALLS. deploy/join.sh points the miner at `ollama-cpu` on a machine
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
    # The judge ROLE is not the engine: a card's miner with --judge serves its model from `ollama`, and a fix
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
    # ONE line: a line naming a slot's project is judged on its own, and must carry --env-file and -f together.
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


# ── C8 the capacity registry ───────────────────────────────────────────────────────────────────────
def test_c8_a_fresh_attributed_line_is_ok(w):
    assert w.check("C8")["state"] == "ok"


def test_c8_no_line_stale_and_unattributed_are_ko(w):
    w.rows = []
    assert w.check("C8")["state"] == "ko"
    w.rows = [{"node_id": "m-bench", "stale": True, "registered_onchain": True}]
    assert "STALE" in w.check("C8")["measured"]
    w.rows = [{"node_id": "m-bench", "stale": False, "registered_onchain": False}]
    assert "NOT attributed" in w.check("C8")["measured"]


def test_c8_an_ABSENT_registered_onchain_is_unmeasured(w):
    # The registry is not proto3: it writes both flags on every row.
    w.rows = [{"node_id": "m-bench", "stale": False}]
    assert w.check("C8")["state"] == "unmeasured"


def test_c8_no_address_or_a_registry_down_is_unmeasured(w):
    assert w.check("C8", cap=False)["state"] == "unmeasured"
    w.reg.routes[("GET", "/capacity")] = lambda p, b: (500, b"{}")
    assert w.check("C8")["state"] == "unmeasured"


# ── C9 processes and heartbeat ─────────────────────────────────────────────────────────────────────
def test_c9_the_daemon_and_its_reveal_worker_with_a_fresh_heartbeat_are_ok(w):
    assert w.check("C9")["state"] == "ok"


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


# ── C10 judge engine (one CPU judge per machine, reached by every identity of a multi-GPU rig) ───────────
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
    # a READ list with no model in it is a reading: the engine holds nothing, ko
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
    # The bound is DERIVED (heartbeat.max_age_s), so the ages are too: a heartbeat a hundred thousand
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


# ── --heartbeat, the image's healthcheck ───────────────────────────────────────────────────────────
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


# ── the deadline and the lock, both INSIDE the container ───────────────────────────────────────────
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


# ── the readers, document by document ──────────────────────────────────────────────────────────────
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


# ── the keyring (modea/keyring.py), read by C3 ─────────────────────────────────────────────────────────
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
    assert r["state"] == "ko" and "CANNOT SIGN" in r["measured"] and "passphrase is missing or wrong" in r["reason"]


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


# ── advice: apart from the checks, and never in the exit code ─────────────────────────────────────────
def test_advice_never_changes_the_exit_code_and_is_said_apart(w, monkeypatch):
    # Keys in clear (the bench's keyring is the test one), the phrase on the volume, no payout declared:
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
    # one key file still in clear under an encrypted keyring: advice, with the restart that seals it
    (w.keys / f"{MID}.sk").write_bytes(b"0" * 32)
    doc = json.loads(w.run("--only", "C3")[1])
    assert [a["id"] for a in doc["advice"]] == ["A1"] and "restart" in doc["advice"][0]["fix"]
    # a declaration recorded for ANOTHER identity is not this one's
    (w.keys / "payout-declared.json").write_text(json.dumps({"miner_id": "dm1other", "address": "dendra1cold"}))
    assert "A3" in [a["id"] for a in json.loads(w.run("--only", "C3")[1])["advice"]]


# ── C11 the judge role this identity DECLARES, and --judge-role, which publish-capacity.sh reads ──────────
# The chain knows no judge role: it draws every present miner into a jury. The role this identity declares in its
# signed capacity report is read here, in its container: active (the worker runs, its engine holds the model THAT
# worker resolved at its start -- its judge state -- and that model is the chain's pin), mute (the worker or that
# model MEASURED absent), off (not requested), unknown (not readable).
def _role(w, monkeypatch, worker=True, **kw):
    """The judge role on (see _judge), and judge_worker.py running beside the daemon unless `worker` is False --
    with the judge state it wrote at its start: the model the chain pins (`pinned`), read from the chain."""
    _judge(w, monkeypatch, **kw)
    w.set_procs("miner.py", "reveal_worker.py", *(("judge_worker.py",) if worker else ()))
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
    rc, out = w.run()
    doc = json.loads(out)
    assert rc == 0 and [c["id"] for c in doc["checks"]] == [f"C{i}" for i in range(1, 12)]


def test_judge_role_a_dead_worker_is_a_mute_seat_with_its_restart(w, monkeypatch):
    _role(w, monkeypatch, worker=False)
    r = w.check("C11")
    assert r["state"] == "ko" and r["measured"].startswith("MUTE:") and "judge_worker.py is NOT running" in r["measured"]
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
    # unreadable processes AND an engine that holds nothing: the seat is KNOWN to be silent
    _role(w, monkeypatch, tags={"models": []})
    assert _probe(w, "--proc-root", str(w.tmp / "no-proc"))[0] == "mute"
    # a dead worker AND an unreadable engine: mute too
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
    # The engine takes longer than the bound: the alarm must NOT be read as "the engine does not answer" (a
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
    # the machine's ONE judge engine is slot 0's, whichever identity reports it
    _role(w, monkeypatch, tags={"models": []})
    rc, out = w.run("--only", "C11", "--compose", "bash /kit/slots.sh run 2")
    assert json.loads(out)["checks"][0]["fix"] == "bash deploy/testnet-miner/slots.sh run 0 exec ollama-cpu ollama pull qwen-judge"


# ── the model the RUNNING worker judges with, and the chain's pin ───────────────────────────────────────────
# judge_worker.py resolves its model ONCE, at its start, and writes it in its judge state. A role declared from a
# model resolved again later -- a fallback the chain did not confirm, a pin that moved since -- would call active a
# seat that judges with another model. Each case below said "active" before the state existed.
def _unflag(w, name):
    p = w.state / name
    if p.exists():
        p.unlink()


def test_judge_role_a_fallback_the_chain_did_not_confirm_is_unknown_never_active(w, monkeypatch):
    # The worker started while the chain did not answer: it judges with the kit's model (env), which the engine
    # holds, and the pin still cannot be read. Whether that model is the pinned one is NOT known.
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
    # the chain answers and pins that very model: the fallback IS the pin, and the role is active
    _unflag(w, "modelregistry.fail")
    w.write("modelregistry.json", {"params": {"audit_judge_model": "kit-model"}})
    w.judge_state("kit-model", "env")
    assert _probe(w)[0] == "active"
    # the chain pins ANOTHER model: unknown, and the remedy restarts the worker so that it reads the pin
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
    # the pin cannot be read now: a model the worker took FROM THE CHAIN at its start is the pin it read
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
    w.judge_state(starttime=1)              # this pid's judge_worker.py started AFTER the state was written
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
    # proto3 omits an empty string: an absent audit_judge_model IS the empty pin. It is said as such, and it is not
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
    """judge_worker.py STARTED as a process -- the write sits in its main(), which no import crosses -- up to its
    judge state: the model the fake chain pins, its own pid and its own start time; then the self-test reads it
    back against the REAL /proc, while the worker runs and once it is gone."""
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
