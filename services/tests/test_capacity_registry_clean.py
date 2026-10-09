# -*- coding: utf-8 -*-
"""ONE MACHINE, ONE ROW -- and the chain's own counts beside the declared inventory.

MEASURED on the public registry (GET /capacity, 2026-10-08): seven entries for four machines. Each machine whose
miner is registered was listed TWICE -- once by the UNSIGNED report deploy/join.sh posts before the miner exists
on chain (namespace `decl::`), once by the SIGNED report publish-capacity.sh posts afterwards (`signe::`) -- and
both were summed in `live_nodes`, `machines` and every total: an over-count, the direction this registry refuses
everywhere else.

What these cases hold, against the SHIPPED `aggregate`, `declared_folded_keys`, `onchain_block` and HTTP handler:
  * a declared report is folded into the signed report of the same identity ON THE SAME MACHINE (same `machine`
    and same `node_id`, or same `machine` and one of its `miner_ids` proven by a signed report): one row, totals
    counted once, the top-level block included;
  * a declared report with no signed twin stays listed and counted; two machines are never merged -- not even
    when a signed report copies the `node_id` every row publishes, nor when one signed report bears a name many
    machines share -- and two cards of ONE machine (same `machine`, two `node_id`) are two identities, never
    merged either; an empty `machine` folds nothing;
  * `verified` is unchanged by the folding;
  * the `onchain` block is READ FROM THE CHAIN through the shipped reader and a real `dendrad` invocation (a fake
    executable on PATH, a fake RPC on a local port): registered miners, and those with a presence proof the
    chain accepted in the current or previous availability window; every reading that fails is
    `measured: false` with a reason and NO count -- a mute chain, an unreadable registry, an index that does
    not reach back, a disabled index, a node catching up, windows disarmed, a search past its page cap
    (refused on its FIRST page), a search that outlasts the reading's budget, a page shorter than the index's
    count, a page that repeats another, an index that moves while it is read;
  * the proof search gives the same answer as the season's reader on the same proofs;
  * one reading serves every GET for ONCHAIN_CACHE_S, then expires;
  * /metrics emits the chain's counts only when they were read.
The subject can be pointed at a MUTATED copy (DENDRA_CAPACITY_SERVER_FILE), so a harness can show each case
turns red.
"""
import importlib.util
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MODEA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODEA))

if os.environ.get("DENDRA_CAPACITY_SERVER_FILE"):
    _spec = importlib.util.spec_from_file_location("capacity_server", os.environ["DENDRA_CAPACITY_SERVER_FILE"])
    cs = importlib.util.module_from_spec(_spec)
    sys.modules["capacity_server"] = cs
    _spec.loader.exec_module(cs)
else:
    import capacity_server as cs  # noqa: E402

NL = chr(10)
# SYNTHETIC identities, in the shapes the chain and the probe serve (`dm1` + 22, `m-` + 10). Never real ones:
# pairing a real node name with a real miner id in a published file would state a link nobody measured.
IDS = ["dm1benchminera0000000000a", "dm1benchminerb0000000000b", "dm1benchminerc0000000000c",
       "dm1benchminerd0000000000d", "dm1benchminere0000000000e", "dm1benchminerf0000000000f",
       "dm1benchminerg0000000000g", "dm1benchminerh0000000000h"]
NOT_READ = {"measured": False, "why": "the chain is not read in this case"}
TOTALS = ("live_nodes", "known_nodes", "machines", "gpu_nodes", "gpus", "vram_total_mb", "ram_total_mb",
          "cpu_cores_total", "judge_capable", "judges_declared", "judge_roles", "distinct_models", "models", "tiers")


def _posted(node_id, miner_ids, proven="", machine="5f2c0e7a91d3", vram=16384, ram=32768, cores=32, tier=4,
            model="qwen3:14b", role="active", share=None, ts=None):
    """A report as POSTed and cleaned by the shipped `_clean`; `proven` is what do_POST sets after the signature."""
    body = {"node_id": node_id, "machine": machine, "backend": "gpu", "gpu": "NVIDIA GeForce RTX 5060 Ti",
            "gpu_count": 1, "vram_mb": vram, "ram_mb": ram, "cpu_cores": cores, "tier": tier, "model": model,
            "can_judge": True, "judge_backend": "cpu", "judge_role": role, "miner_ids": list(miner_ids)}
    if share is not None:
        body["host_share"] = share
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = proven
    if ts is not None:
        rep["ts"] = ts
    return rep


def _store(*reps):
    return {cs.storage_key(r): r for r in reps}


def _agg(monkeypatch, *reps):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    return cs.aggregate(_store(*reps), NOT_READ)


def _rows(a, node_id):
    return [r for r in a["nodes"] if r["node_id"] == node_id]


# ── 1. one machine, one row ─────────────────────────────────────────────────────────────────────────────
def test_a_signed_machine_and_its_declared_twin_make_one_row_and_one_count(monkeypatch):
    # The shape measured on the registry: join.sh's report names the kit's `m-` name, the signed one proves `dm1`.
    signed = _posted("m-a1a1a1a1a1", [IDS[0]], proven=IDS[0])
    declared = _posted("m-a1a1a1a1a1", ["m-a1a1a1a1a1"])
    both = _agg(monkeypatch, signed, declared)
    alone = _agg(monkeypatch, signed)
    rows = _rows(both, "m-a1a1a1a1a1")
    assert len(rows) == 1 and len(both["nodes"]) == 1, both["nodes"]
    assert rows[0]["registered_onchain"] is True, "the row kept is the SIGNED one"
    for k in TOTALS:
        assert both[k] == alone[k], (k, both[k], alone[k])
    # ...and the totals are the ones of ONE machine, so the case cannot pass by both being wrong alike.
    assert (both["live_nodes"], both["machines"], both["gpu_nodes"], both["judge_capable"],
            both["judges_declared"]) == (1, 1, 1, 1, 1)
    assert both["vram_total_mb"] == cs._round_mb(16384) and both["ram_total_mb"] == cs._round_mb(32768)
    assert both["models"] == {"qwen3:14b": 1} and both["tiers"] == {"4": 1}
    assert both["verified"] == alone["verified"], "the verified block does not move"
    assert both["declared_folded"] == 1 and alone["declared_folded"] == 0


def test_the_registry_measured_on_2026_10_08_reads_four_machines(monkeypatch):
    reps = []
    for i, nid in enumerate(("m-a1a1a1a1a1", "m-b2b2b2b2b2", "m-c3c3c3c3c3")):
        reps += [_posted(nid, [IDS[i]], proven=IDS[i], machine="box%d" % i), _posted(nid, [nid], machine="box%d" % i)]
    reps.append(_posted("m-d4d4d4d4d4", ["m-d4d4d4d4d4"], machine="box3", vram=12288, tier=3, model="mistral-nemo"))
    a = _agg(monkeypatch, *reps)
    assert len(reps) == 7
    assert (len(a["nodes"]), a["known_nodes"], a["live_nodes"], a["machines"], a["gpu_nodes"]) == (4, 4, 4, 4, 4)
    assert a["declared_folded"] == 3
    assert sum(1 for r in a["nodes"] if r["registered_onchain"]) == 3
    assert a["verified"]["live_nodes"] == 3 and a["verified"]["machines"] == 3
    assert a["vram_total_mb"] == 3 * cs._round_mb(16384) + cs._round_mb(12288)


def test_a_declared_report_naming_a_signed_miner_id_is_folded(monkeypatch):
    a = _agg(monkeypatch, _posted("m-signed", [IDS[0]], proven=IDS[0]), _posted("m-other-name", [IDS[0]]))
    assert [r["node_id"] for r in a["nodes"]] == ["m-signed"]
    assert a["live_nodes"] == 1 and a["declared_folded"] == 1


def test_a_declared_report_without_a_signed_twin_stays_listed_and_counted(monkeypatch):
    a = _agg(monkeypatch, _posted("m-old-kit", ["m-old-kit"]))
    assert len(a["nodes"]) == 1 and a["nodes"][0]["registered_onchain"] is False
    assert a["live_nodes"] == 1 and a["vram_total_mb"] == cs._round_mb(16384) and a["declared_folded"] == 0


def test_two_machines_are_never_merged(monkeypatch):
    s = _posted("m-alpha", [IDS[0]], proven=IDS[0], machine="aaaaaaaaaaaa")
    d = _posted("m-beta", ["m-beta"], machine="bbbbbbbbbbbb", vram=12288)
    a = _agg(monkeypatch, s, d)
    assert sorted(r["node_id"] for r in a["nodes"]) == ["m-alpha", "m-beta"]
    assert a["live_nodes"] == 2 and a["machines"] == 2 and a["declared_folded"] == 0
    assert a["vram_total_mb"] == cs._round_mb(16384) + cs._round_mb(12288)


def test_two_cards_of_one_machine_are_two_identities_never_merged(monkeypatch):
    # deploy/join.sh --gpus: every card is its own identity, all with the SAME `machine`. The second card's
    # signature has not landed yet: its declared report must stay, not vanish behind its neighbour's.
    s = _posted("m-rig", [IDS[0]], proven=IDS[0], machine="rigrigrigrig")
    d = _posted("m-rig-g1", ["m-rig-g1"], machine="rigrigrigrig", share="secondary", vram=8192)
    a = _agg(monkeypatch, s, d)
    assert sorted(r["node_id"] for r in a["nodes"]) == ["m-rig", "m-rig-g1"], a["nodes"]
    assert a["gpu_nodes"] == 2 and a["declared_folded"] == 0
    assert a["vram_total_mb"] == cs._round_mb(16384) + cs._round_mb(8192)


def test_a_signed_report_copying_a_published_node_id_folds_no_other_machine(monkeypatch):
    # Review of 2026-10-08, case A2: a registered miner signs a report with the `node_id` of a victim, which every
    # row publishes. The victim's machine key is never published, so the signer cannot copy it.
    victim = _posted("m-victim", ["m-victim"], machine="0a0a0a0a0a0a", vram=12288)
    forged = _posted("m-victim", [IDS[0]], proven=IDS[0], machine="fefefefefefe")
    a = _agg(monkeypatch, victim, forged)
    assert len(a["nodes"]) == 2 and a["declared_folded"] == 0, a["nodes"]
    assert a["live_nodes"] == 2 and a["vram_total_mb"] == cs._round_mb(16384) + cs._round_mb(12288)
    assert sorted(r["registered_onchain"] for r in a["nodes"]) == [False, True]


def test_one_signed_report_never_folds_several_machines_sharing_a_name(monkeypatch):
    # Review of 2026-10-08, case A3: `node` is deploy/join.sh's fallback when it has no miner id, and two operators
    # may pick the same --id. Only the twin on the SAME machine is folded.
    signed = _posted("node", [IDS[0]], proven=IDS[0], machine="aaaaaaaaaaaa")
    twin = _posted("node", ["node"], machine="aaaaaaaaaaaa")
    others = [_posted("node", ["node"], machine=m) for m in ("bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd")]
    a = _agg(monkeypatch, signed, twin, *others)
    assert len(a["nodes"]) == 4 and a["live_nodes"] == 4 and a["declared_folded"] == 1, a["nodes"]
    assert cs.declared_folded_keys(_store(signed, twin, *others)) == {cs.storage_key(twin)}


def test_a_declared_report_naming_a_signed_miner_id_on_another_machine_is_not_folded(monkeypatch):
    s = _posted("m-signed", [IDS[0]], proven=IDS[0], machine="aaaaaaaaaaaa")
    d = _posted("m-elsewhere", [IDS[0]], machine="bbbbbbbbbbbb")
    a = _agg(monkeypatch, s, d)
    assert sorted(r["node_id"] for r in a["nodes"]) == ["m-elsewhere", "m-signed"]
    assert a["live_nodes"] == 2 and a["declared_folded"] == 0


def test_an_empty_machine_folds_nothing(monkeypatch):
    # An empty `machine` names no machine: equal emptiness is not the same host.
    s = _posted("m-blank", [IDS[0]], proven=IDS[0], machine="")
    d = _posted("m-blank", ["m-blank"], machine="")
    a = _agg(monkeypatch, s, d)
    assert len(a["nodes"]) == 2 and a["declared_folded"] == 0, a["nodes"]


def test_a_stale_signed_report_keeps_its_row_against_a_fresh_declared_twin(monkeypatch):
    # An unsigned deposit, which anyone can make, must not change how a signed identity reads.
    old = int(time.time()) - cs.STALE_S - 60
    a = _agg(monkeypatch, _posted("m-x", [IDS[0]], proven=IDS[0], ts=old), _posted("m-x", ["m-x"]))
    assert len(a["nodes"]) == 1
    assert a["nodes"][0]["registered_onchain"] is True and a["nodes"][0]["stale"] is True
    assert a["live_nodes"] == 0 and a["declared_folded"] == 1


def test_a_signed_report_is_never_folded(monkeypatch):
    s0 = _posted("m-shared-name", [IDS[0]], proven=IDS[0])
    s1 = _posted("m-shared-name", [IDS[1]], proven=IDS[1])
    forged = _posted("m-shared-name", [IDS[0], IDS[1]])
    a = _agg(monkeypatch, s0, s1, forged)
    assert len(a["nodes"]) == 2 and all(r["registered_onchain"] for r in a["nodes"])
    assert a["verified"]["live_nodes"] == 2 and a["declared_folded"] == 1
    assert set(cs.declared_folded_keys(_store(s0, s1, forged))) == {cs.storage_key(forged)}


def test_the_provenance_says_how_the_folding_is_done(monkeypatch):
    t = _agg(monkeypatch)["_provenance"]["folding"]
    for must in ("`node_id`", "same machine", "never published", "folds nothing", "never by machine alone",
                 "signed report is the one kept", "stays listed", "`verified`", "`declared_folded`"):
        assert must in t, must


# ── 2. the same, over HTTP, as a joiner reaches it ──────────────────────────────────────────────────────
def _serve(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_RATE", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode()


def test_over_http_the_declared_twin_is_folded_and_metrics_follow(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    monkeypatch.setattr(cs, "onchain_block", lambda: dict(NOT_READ))
    monkeypatch.setattr(cs, "_proven_miner_of", lambda h, b: (h.get("X-Bench-Proof") or "").strip())
    srv, base = _serve(monkeypatch, tmp_path)
    try:
        def post(body, proof=""):
            req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Bench-Proof": proof})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
        common = {"node_id": "m-joiner", "machine": "abcabcabcabc", "backend": "gpu", "gpu": "RTX 5060 Ti",
                  "gpu_count": 1, "vram_mb": 16384, "ram_mb": 32768, "cpu_cores": 32, "tier": 4,
                  "model": "qwen3:14b", "can_judge": True}
        post(dict(common, miner_ids=["m-joiner"]))                       # join.sh, before the registration
        post(dict(common, miner_ids=[IDS[2]]), proof=IDS[2])             # publish-capacity.sh, signed
        a = json.loads(_get(base + "/capacity"))
        metrics = _get(base + "/metrics").splitlines()
    finally:
        srv.shutdown()
        srv.server_close()
    assert [r["node_id"] for r in a["nodes"]] == ["m-joiner"] and a["nodes"][0]["registered_onchain"] is True
    assert a["live_nodes"] == 1 and a["declared_folded"] == 1 and a["verified"]["live_nodes"] == 1
    assert "dendra_capacity_live_nodes 1" in metrics
    assert "dendra_capacity_declared_folded 1" in metrics
    assert "dendra_capacity_onchain_measured 0" in metrics
    assert not any(m.startswith("dendra_capacity_onchain_present_miners") for m in metrics), (
        "a count that was not read must not be emitted: an absent series is unknown, a 0 is a measurement")


# ── 3. the chain's counts: the shipped reader, a real `dendrad` invocation ─────────────────────────────────
AVAIL = "/dendra.jobs.v1.MsgProveAvailability"
HEIGHT, EB = 32720, 288
FIRST = (HEIGHT // EB - 1) * EB          # 32256: the start of the previous window, computed the chain's way

_FAKE_DENDRAD = """
import json, os, sys, time
scenario = json.load(open(os.environ["CAPACITY_FAKE_CHAIN"]))
args = sys.argv[1:]
with open(scenario["log"], "a") as f:
    print(" ".join(args), file=f)
def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)
if args[:3] == ["query", "jobs", "list-miner"]:
    if scenario.get("miners") is None:
        fail("post failed: connection refused")
    print(json.dumps({"miner": scenario["miners"], "pagination": {"next_key": None, "total": "0"}}))
elif args[:3] == ["query", "jobs", "params"]:
    if scenario.get("params") is None:
        fail("post failed: connection refused")
    print(json.dumps({"params": scenario["params"]}))
elif args[:2] == ["query", "txs"]:
    if scenario.get("txs") is None:
        fail("transaction indexing is disabled")
    time.sleep(float(scenario.get("txs_sleep") or 0))
    page = int(args[args.index("--page") + 1])
    per = min(int(args[args.index("--limit") + 1]), 100)
    chunk = scenario["txs"][(page - 1) * per: page * per]
    chunk = (scenario.get("pages") or {}).get(str(page), chunk)          # a page served out of order
    answer = {"total_count": str(len(scenario["txs"])), "count": str(len(chunk)),
              "page_number": str(page), "limit": str(per), "txs": chunk}
    totals = scenario.get("totals")                                      # the count each page claims
    if totals is not None:
        t = totals[min(page, len(totals)) - 1]
        if t is None:
            del answer["total_count"]                                    # proto3 omits a zero
        else:
            answer["total_count"] = str(t)
    print(json.dumps(answer))
else:
    fail("unknown command: " + " ".join(args))
"""


def _proof(mid, height, code=None, typ=AVAIL, extra=()):
    msgs = [{"@type": typ, "creator": "dendra1op" + mid[-6:], "miner_id": mid, "challenge": "c", "vrf_proof": "00"}]
    msgs += list(extra)
    t = {"height": str(height), "txhash": "H%d%s" % (height, mid[-4:]), "tx": {"body": {"messages": msgs}}}
    if code is not None:
        t["code"] = code
    return t


def _proofs():
    """The proofs the fake node holds -- the fake does NOT filter on the query, so the reader must."""
    other = {"@type": "/dendra.jobs.v1.MsgUpdateMiner", "creator": "dendra1x", "miner_id": IDS[6]}
    return [
        _proof(IDS[0], FIRST + 1, code=0), _proof(IDS[0], FIRST + EB + 2, code=0),   # twice: counted once
        _proof(IDS[1], FIRST + 2),                     # `code` absent: proto3 omits 0, an accepted proof
        _proof(IDS[2], FIRST, code=0),                 # the window's first height counts
        _proof(IDS[3], FIRST - 1, code=0),             # one block before the window: does not count
        _proof(IDS[4], FIRST + 300, code=5),           # refused by the chain: proves nothing
        _proof(IDS[5], FIRST + 400, code=0, extra=[other]),   # IDS[5] counts; IDS[6], named by another message, not
        _proof("dm1notregistered000000000", FIRST + 410, code=0),   # proven, but not in the registry
        _proof(IDS[7], FIRST + 420, code=0, typ="/dendra.jobs.v2.MsgProveAvailability"),   # another message type
    ]


class _Rpc(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        st = self.server.status
        if self.path.split("?")[0] != "/status" or st is None:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"jsonrpc": "2.0", "id": -1, "result": {"node_info": {"network": "bench"},
                                                                   "sync_info": st}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeChain:
    def __init__(self, tmp_path, monkeypatch, rpc=True):
        self.dir = tmp_path / "chain"
        (self.dir / "bin").mkdir(parents=True)
        exe = self.dir / "bin" / "dendrad"
        exe.write_text("#!" + sys.executable + NL + _FAKE_DENDRAD.lstrip(), encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.log = self.dir / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.scenario_path = self.dir / "scenario.json"
        self.srv = None
        if rpc:
            self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Rpc)
            threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            port = self.srv.server_address[1]
        else:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()                                      # nothing listens there: a mute node
        self.node = "tcp://127.0.0.1:%d" % port
        monkeypatch.setenv("DENDRA_NODE", self.node)
        monkeypatch.setenv("CAPACITY_FAKE_CHAIN", str(self.scenario_path))
        monkeypatch.setenv("PATH", str(self.dir / "bin") + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setitem(cs._MINERS, "t", 0.0)
        monkeypatch.setitem(cs._MINERS, "ids", set())
        monkeypatch.setitem(cs._MINERS, "ops", {})
        monkeypatch.setitem(cs._ONCHAIN, "t", 0.0)
        monkeypatch.setitem(cs._ONCHAIN, "block", None)
        self.set()

    def set(self, **kw):
        sc = {"log": str(self.log),
              "miners": [{"miner_id": m, "operator": "dendra1op" + m[-6:]} for m in IDS],
              "params": {"min_stake": "1000000", "avail_epoch_blocks": str(EB)},
              "txs": _proofs(),
              "status": {"latest_block_height": str(HEIGHT), "earliest_block_height": "1", "catching_up": False}}
        sc.update(kw)
        self.scenario_path.write_text(json.dumps(sc), encoding="utf-8")
        if self.srv is not None:
            self.srv.status = sc["status"]

    def calls(self, prefix):
        return [c for c in self.log.read_text(encoding="utf-8").splitlines() if c.startswith(prefix)]

    def close(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()


posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake dendrad is an executable script (POSIX)")


@pytest.fixture
def chain(tmp_path, monkeypatch):
    c = FakeChain(tmp_path, monkeypatch)
    yield c
    c.close()


@pytest.fixture
def mute_chain(tmp_path, monkeypatch):
    c = FakeChain(tmp_path, monkeypatch, rpc=False)
    yield c
    c.close()


def _unmeasured(block):
    """`measured: false`, a reason, and NOT ONE count -- never a zero standing for a failure."""
    return (block.get("measured") is False and isinstance(block.get("why"), str) and block["why"]
            and set(block) == {"measured", "why"})


@posix_only
def test_onchain_counts_the_registered_and_the_present_miners(chain):
    b = cs.onchain_block()
    assert b == {"measured": True, "registered": 8, "present": 4, "window_start": FIRST, "height": HEIGHT,
                 "avail_epoch_blocks": EB}, b


@posix_only
def test_onchain_invokes_the_cli_as_shipped(chain):
    cs.onchain_block()
    params = chain.calls("query jobs params")
    assert params and params[0].endswith("--node %s --output json" % chain.node), params
    txs = chain.calls("query txs")
    want = "message.action='%s' AND tx.height>=%d AND tx.height<=%d" % (AVAIL, FIRST, HEIGHT)
    assert txs and want in txs[0], txs
    assert chain.calls("query jobs list-miner"), "the registry is read through the same dendrad"


@posix_only
def test_a_mute_chain_is_not_measured_never_zero(mute_chain):
    mute_chain.set(miners=None, params=None, txs=None)
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_a_node_whose_rpc_does_not_answer_is_not_measured(mute_chain):
    # The CLI answers (registry, parameters, index) and the node's RPC does not: the height is not known, so
    # neither is the window -- and the proofs already readable are not counted against a guessed one.
    b = cs.onchain_block()
    assert _unmeasured(b) and "status" in b["why"], b
    assert not mute_chain.calls("query txs"), "no search against a window computed from no height"


@posix_only
def test_an_unreadable_registry_is_not_measured(chain):
    chain.set(miners=None)
    b = cs.onchain_block()
    assert _unmeasured(b) and "registry" in b["why"], b


@posix_only
def test_an_index_that_does_not_reach_back_is_not_measured(chain):
    chain.set(status={"latest_block_height": str(HEIGHT), "earliest_block_height": str(FIRST + 44),
                      "catching_up": False})
    b = cs.onchain_block()
    assert _unmeasured(b) and "index" in b["why"], b


@posix_only
def test_a_disabled_index_is_not_measured(chain):
    chain.set(txs=None)
    b = cs.onchain_block()
    assert _unmeasured(b) and "index" in b["why"], b


@posix_only
def test_a_node_catching_up_is_not_measured(chain):
    chain.set(status={"latest_block_height": str(HEIGHT), "earliest_block_height": "1", "catching_up": True})
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_a_node_that_does_not_say_whether_it_catches_up_is_not_measured(chain):
    chain.set(status={"latest_block_height": str(HEIGHT), "earliest_block_height": "1"})
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_disarmed_windows_are_not_counted_as_nobody_present(chain):
    chain.set(params={"min_stake": "1000000"})           # proto3: absent IS 0, and 0 refuses every proof
    b = cs.onchain_block()
    assert _unmeasured(b) and "avail_epoch_blocks" in b["why"], b


@posix_only
def test_no_node_configured_is_not_measured(chain, monkeypatch):
    monkeypatch.delenv("DENDRA_NODE")
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_the_presence_search_is_refused_past_its_page_cap_never_cut(chain, monkeypatch):
    monkeypatch.setattr(cs, "ONCHAIN_MAX_PAGES", 1)
    chain.set(txs=[_proof(IDS[i % 8], FIRST + 1 + i, code=0) for i in range(150)])
    assert _unmeasured(cs.onchain_block())


def _many(n, mid=None):
    """n accepted proofs at distinct heights inside the window: every one has its own transaction hash."""
    return [_proof(mid or IDS[i % 8], FIRST + 1 + i, code=0) for i in range(n)]


@posix_only
def test_the_presence_search_past_its_cap_is_refused_on_its_first_page(chain, monkeypatch):
    # Review of 2026-10-08: the count is on page 1, so a search the cap cannot hold is refused there -- not after
    # every page the cap allows, which each minute launched as many `dendrad` processes for a refusal.
    monkeypatch.setattr(cs, "ONCHAIN_MAX_PAGES", 2)
    chain.set(txs=_many(250))
    assert _unmeasured(cs.onchain_block())
    assert len(chain.calls("query txs")) == 1, chain.calls("query txs")


@posix_only
def test_a_search_that_outlasts_the_budget_is_not_measured(chain, monkeypatch):
    # Every page took the CLI's own sixty seconds, fifty pages long (review of 2026-10-08). A page call now takes
    # what is left of the reading's budget as its timeout: a node slower than the budget is not measured, quickly.
    monkeypatch.setattr(cs, "ONCHAIN_BUDGET_S", 3.0)
    chain.set(txs_sleep=8)
    t0 = time.monotonic()
    b = cs.onchain_block()
    took = time.monotonic() - t0
    assert _unmeasured(b), b
    assert took < 6.5, "the reading outlived its budget: %.1f s for a 3 s budget" % took


@posix_only
def test_a_spent_budget_starts_no_further_step(chain, monkeypatch):
    monkeypatch.setattr(cs, "ONCHAIN_BUDGET_S", 0.0)
    b = cs.onchain_block()
    assert _unmeasured(b) and "budget" in b["why"], b
    assert not chain.calls("query jobs params") and not chain.calls("query txs"), (
        "a step that finds the budget spent is not started")


@posix_only
def test_a_page_shorter_than_the_count_is_refused_never_cut(chain):
    # Review of 2026-10-08, the season reader's cut: the index counts 250, the pages hold 150, and a reader that
    # stops on the first empty page returns the 150 as if they were the whole.
    chain.set(txs=_many(150), totals=[250])
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_a_page_repeating_another_is_refused_never_cut(chain):
    # 150 proofs, the last 50 from the only proof of IDS[1]; page 2 repeats half of page 1. A raw tally reaches the
    # count, and IDS[1] is silently missing: distinct transactions are what must reach it.
    txs = _many(100, mid=IDS[0]) + [_proof(IDS[1], FIRST + 200 + i, code=0) for i in range(50)]
    chain.set(txs=txs, pages={"2": txs[:50]})
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_an_index_that_moves_while_read_is_refused(chain):
    chain.set(txs=_many(150), totals=[150, 151])
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_an_absent_count_over_no_proof_is_a_measured_zero(chain):
    # Rule of zero: the search answered, with no transaction and no count -- proto3 omits a zero. Nobody proved
    # presence in the window, and that is known, not unknown.
    chain.set(txs=[], totals=[None])
    b = cs.onchain_block()
    assert b.get("measured") is True and b.get("present") == 0 and b.get("registered") == 8, b


@posix_only
def test_an_absent_count_over_proofs_is_refused(chain):
    # ...and an absent count cannot end a search that holds proofs: the distinct transactions read must equal it.
    chain.set(totals=[None])
    assert _unmeasured(cs.onchain_block())


@posix_only
def test_the_presence_reader_agrees_with_the_seasons(chain):
    # The search is this file's (bounded, never cut) and the rules for which proof counts are the season's: the
    # two must give the same miners on the same proofs, on one page and on several.
    import final_season_chain as fsc
    want = {IDS[0], IDS[1], IDS[2], IDS[5], "dm1notregistered000000000"}
    season = set(fsc.presence_proofs(chain.node, FIRST, HEIGHT, EB)[0])
    ours = cs._present_ids(fsc, chain.node, FIRST, HEIGHT, time.monotonic() + 30)
    assert ours == season == want, (ours, season)
    chain.set(txs=_many(250))
    season = set(fsc.presence_proofs(chain.node, FIRST, HEIGHT, EB)[0])
    ours = cs._present_ids(fsc, chain.node, FIRST, HEIGHT, time.monotonic() + 30)
    assert ours == season == set(IDS), (ours, season)


@posix_only
def test_one_reading_serves_every_get_until_it_expires(chain, monkeypatch):
    a = cs.onchain_block()
    b = cs.onchain_block()
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    c = cs.aggregate({}).get("onchain")
    assert a == b == c and a["measured"] is True
    assert len(chain.calls("query txs")) == 1, "the chain is read once per ONCHAIN_CACHE_S, not once per GET"
    cs._ONCHAIN["t"] -= cs.ONCHAIN_CACHE_S + 1
    cs.onchain_block()
    assert len(chain.calls("query txs")) == 2, "an expired reading is read again"


@posix_only
def test_over_http_the_onchain_block_and_its_metrics(chain, monkeypatch, tmp_path):
    srv, base = _serve(monkeypatch, tmp_path)
    try:
        a = json.loads(_get(base + "/capacity"))
        metrics = _get(base + "/metrics").splitlines()
    finally:
        srv.shutdown()
        srv.server_close()
    assert a["onchain"]["measured"] is True and (a["onchain"]["registered"], a["onchain"]["present"]) == (8, 4)
    for line in ("dendra_capacity_onchain_measured 1", "dendra_capacity_onchain_registered_miners 8",
                 "dendra_capacity_onchain_present_miners 4", "dendra_capacity_onchain_window_start %d" % FIRST):
        assert line in metrics, (line, metrics)


def test_the_onchain_provenance_says_read_not_declared(monkeypatch):
    t = _agg(monkeypatch)["_provenance"]["onchain"]
    for must in ("not declared", "`registered`", "`present`", "ACCEPTED", "`window_start`", "`avail_epoch_blocks`",
                 "`measured: false`", "not a zero"):
        assert must in t, must
