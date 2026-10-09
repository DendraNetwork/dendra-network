# -*- coding: utf-8 -*-
"""THE JUDGE ROLE A REPORT DECLARES, and what the registry may say about it.

The chain knows no judge role: it draws every present miner into a jury. Only a miner started with the role,
whose judge worker runs and whose engine holds the model it judges with, posts a verdict; the others are drawn
and say nothing -- mute seats. Until this field, the registry published `judge_capable` (a machine has the RAM
to judge) and nothing else, so nobody could tell how many judges ran before a jury formed.

What these cases hold, each against the SHIPPED `_clean`, `aggregate` and HTTP handler:
  * the four words only (active | mute | off | unknown); an ABSENT field -- every older kit -- and a word that
    is not one of the four are "unknown", never "off" and never "active";
  * `judges_declared` counts the ACTIVE roles of live reports, `judge_roles` every word, and neither is ever
    `judge_capable`;
  * in `verified`, only reports PROVEN to come from the on-chain operator count -- an unsigned "active" is a
    declaration, kept at the top level and kept out of the block public surfaces display;
  * `_provenance` says it is declared, names the only proof (a verdict on chain), and says unknown is not off.
The subject can be pointed at a MUTATED copy (DENDRA_CAPACITY_SERVER_FILE), so a harness can show each case
turns red.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("DENDRA_CAPACITY_SERVER_FILE"):
    _spec = importlib.util.spec_from_file_location("capacity_server", os.environ["DENDRA_CAPACITY_SERVER_FILE"])
    cs = importlib.util.module_from_spec(_spec)
    sys.modules["capacity_server"] = cs
    _spec.loader.exec_module(cs)
else:
    import capacity_server as cs  # noqa: E402

IDS = ["dm1judgeactive00000000000", "dm1judgemute000000000000", "dm1judgeoff0000000000000",
       "dm1judgeold0000000000000", "dm1judgeunsigned00000000"]


def _posted(node_id, miner_id, role=None, proven=True, can_judge=True, ts=None):
    """A report as POSTed and cleaned by the shipped `_clean`; `role=None` = a kit older than the field."""
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": 12288, "ram_mb": 65536, "cpu_cores": 16, "tier": 3,
            "model": "mistral-nemo", "can_judge": can_judge, "judge_backend": "cpu", "miner_ids": [miner_id]}
    if role is not None:
        body["judge_role"] = role
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = miner_id if proven else ""
    if ts is not None:
        rep["ts"] = ts
    return rep


def _store(*reps):
    return {cs.storage_key(r): r for r in reps}


def _agg(monkeypatch, *reps):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    return cs.aggregate(_store(*reps))


def _row(a, node_id):
    rows = [r for r in a["nodes"] if r["node_id"] == node_id]
    assert len(rows) == 1, a["nodes"]
    return rows[0]


# ── the four words, and the silence of an older kit ─────────────────────────────────────────────────────
def test_an_absent_judge_role_is_unknown_never_off(monkeypatch):
    rep = _posted("m-old", IDS[3], role=None)
    assert rep["judge_role"] == "unknown"
    a = _agg(monkeypatch, rep)
    assert _row(a, "m-old")["judge_role"] == "unknown"
    assert a["judge_roles"] == {"active": 0, "mute": 0, "off": 0, "unknown": 1}
    assert a["verified"]["judge_roles"] == {"active": 0, "mute": 0, "off": 0, "unknown": 1}
    assert a["judges_declared"] == 0 and a["verified"]["judges_declared"] == 0


def test_a_record_stored_before_the_field_existed_is_published_unknown(monkeypatch):
    # The registry keeps reports on disk for days: a record stored before the judge_role field existed has
    # no judge_role key at all, and it is read again at every GET.
    rep = _posted("m-stored", IDS[3], role="active")
    del rep["judge_role"]
    a = _agg(monkeypatch, rep)
    assert _row(a, "m-stored")["judge_role"] == "unknown" and a["judge_roles"]["unknown"] == 1
    assert a["judge_roles"]["off"] == 0 and a["judges_declared"] == 0


def test_only_the_four_words_pass_anything_else_is_unknown():
    for good in ("active", "mute", "off", "unknown"):
        assert cs._clean({"node_id": "x", "judge_role": good})["judge_role"] == good
    for bad in ("ACTIVE", "active ", " off", "", "on", "yes", "judge", 1, True, None, ["active"], {"r": "active"}):
        assert cs._clean({"node_id": "x", "judge_role": bad})["judge_role"] == "unknown", bad


# ── what is counted, and what it is not ─────────────────────────────────────────────────────────────────
def test_judges_declared_counts_active_roles_and_is_never_judge_capable(monkeypatch):
    # Two capable machines that judge nothing, one judge on a machine the probe calls not capable: the two
    # counts must come apart (2 and 1), or a count of one could not tell them from each other.
    a = _agg(monkeypatch,
             _posted("m-capable-off", IDS[2], role="off", can_judge=True),
             _posted("m-capable-unknown", IDS[3], role=None, can_judge=True),
             _posted("m-active-small", IDS[0], role="active", can_judge=False))
    assert a["judge_capable"] == 2 and a["judges_declared"] == 1
    assert a["verified"]["judge_capable"] == 2 and a["verified"]["judges_declared"] == 1
    # the two are different nodes: the capable one declares no judge, the judge declares no capability
    assert _row(a, "m-capable-off")["judge_role"] == "off" and _row(a, "m-capable-off")["can_judge"] is True
    assert _row(a, "m-active-small")["judge_role"] == "active" and _row(a, "m-active-small")["can_judge"] is False


def test_mute_seats_are_counted_apart_from_active_judges(monkeypatch):
    a = _agg(monkeypatch, _posted("m-a", IDS[0], role="active"), _posted("m-m", IDS[1], role="mute"),
             _posted("m-o", IDS[2], role="off"), _posted("m-u", IDS[3], role=None))
    assert a["judges_declared"] == 1
    assert a["judge_roles"] == {"active": 1, "mute": 1, "off": 1, "unknown": 1}
    assert a["verified"]["judge_roles"] == {"active": 1, "mute": 1, "off": 1, "unknown": 1}
    assert sum(a["judge_roles"].values()) == a["live_nodes"]


def test_verified_counts_only_reports_proven_by_the_onchain_operator(monkeypatch):
    a = _agg(monkeypatch, _posted("m-signed", IDS[0], role="active"),
             _posted("m-unsigned", IDS[4], role="active", proven=False),
             _posted("m-unsigned-mute", IDS[1], role="mute", proven=False))
    assert a["judges_declared"] == 2 and a["judge_roles"]["mute"] == 1
    assert a["verified"]["judges_declared"] == 1, "an unsigned declaration never enters the verified block"
    assert a["verified"]["judge_roles"] == {"active": 1, "mute": 0, "off": 0, "unknown": 0}
    assert _row(a, "m-unsigned")["registered_onchain"] is False and _row(a, "m-unsigned")["judge_role"] == "active"


def test_a_stale_report_declares_nothing_any_more(monkeypatch):
    old = int(time.time()) - cs.STALE_S - 60
    a = _agg(monkeypatch, _posted("m-gone", IDS[0], role="active", ts=old))
    assert a["judges_declared"] == 0 and a["verified"]["judges_declared"] == 0
    assert a["judge_roles"] == {"active": 0, "mute": 0, "off": 0, "unknown": 0}
    assert _row(a, "m-gone")["stale"] is True and _row(a, "m-gone")["judge_role"] == "active"


def test_the_provenance_says_declared_and_names_the_only_proof(monkeypatch):
    t = _agg(monkeypatch)["_provenance"]["judges_declared"]
    for must in ("DECLARED, not proven", "knows no judge role", "`mute`", "never counted as `off`",
                 "NOT `judge_capable`", "verdict it posted on chain", "signed by the on-chain operator",
                 "the one the chain pins"):
        assert must in t, must


# ── end to end: the shipped handler, over HTTP ──────────────────────────────────────────────────────────
def test_over_http_the_role_posted_is_the_role_served(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    monkeypatch.setattr(cs, "_RATE", {})
    # The signature is benched end to end with real keys elsewhere; here a header stands for the proof, so the
    # HTTP path (POST -> store -> GET) is what is held.
    monkeypatch.setattr(cs, "_proven_miner_of", lambda h, b: (h.get("X-Bench-Proof") or "").strip())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        def post(body, proof=""):
            req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Bench-Proof": proof})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
        common = {"backend": "gpu", "gpu": "RTX 4070", "gpu_count": 1, "vram_mb": 12288, "tier": 3,
                  "model": "m", "can_judge": True}
        post(dict(common, node_id="n-active", miner_ids=[IDS[0]], judge_role="active"), proof=IDS[0])
        post(dict(common, node_id="n-mute", miner_ids=[IDS[1]], judge_role="mute"), proof=IDS[1])
        post(dict(common, node_id="n-old", miner_ids=[IDS[3]]), proof=IDS[3])
        post(dict(common, node_id="n-unsigned", miner_ids=[IDS[4]], judge_role="active"))
        with urllib.request.urlopen(base + "/capacity", timeout=10) as r:
            a = json.loads(r.read())
        with urllib.request.urlopen(base + "/metrics", timeout=10) as r:
            metrics = r.read().decode().splitlines()
    finally:
        srv.shutdown()
        srv.server_close()
    assert {r["node_id"]: r["judge_role"] for r in a["nodes"]} == {
        "n-active": "active", "n-mute": "mute", "n-old": "unknown", "n-unsigned": "active"}
    assert a["judges_declared"] == 2 and a["judge_roles"] == {"active": 2, "mute": 1, "off": 0, "unknown": 1}
    assert a["verified"]["judges_declared"] == 1
    assert a["verified"]["judge_roles"] == {"active": 1, "mute": 1, "off": 0, "unknown": 1}
    assert a["judge_capable"] == 4 and a["verified"]["judge_capable"] == 3
    assert "dendra_capacity_judges_declared 2" in metrics
    assert 'dendra_capacity_judge_role_nodes{role="mute"} 1' in metrics
    assert 'dendra_capacity_judge_role_nodes{role="unknown"} 1' in metrics
