# -*- coding: utf-8 -*-
"""The judge role a report declares, and what the registry may say about it.

The chain draws declared judges into its juries, and this registry does not read that declaration. Only a miner
started with the role, whose judge worker runs and whose engine holds the model it judges with, posts a verdict;
a declared judge without one is drawn, silent, until its missed draws reach judge_missed_draws_max or neither its last verdict nor its declaration is within judge_verdict_window_blocks -- a mute seat. Until this field, the registry published `judge_capable` (a machine has the RAM
to judge) and nothing else, so nobody could tell how many judges ran before a jury formed.

What these cases hold, each against the shipped `_clean`, `aggregate` and HTTP handler:
  * the four words only (active | mute | off | unknown); an absent field (every older kit) and a word that
    is not one of the four are "unknown", never "off" and never "active";
  * `judges_declared` counts the active roles of live reports, `judge_roles` every word, and neither is ever
    `judge_capable`;
  * in `verified`, only reports proven to come from the on-chain operator count -- an unsigned "active" is a
    declaration, kept at the top level and kept out of the block public surfaces display;
  * `_provenance` says it is declared, names the only proof (a verdict on chain), and says unknown is not off.
The subject can be pointed at a mutated copy (DENDRA_CAPACITY_SERVER_FILE), so a harness can show each case
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


# -- the four words, and the silence of an older kit -----------------------------------------------------
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


# -- what is counted, and what it is not -----------------------------------------------------------------
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
    for must in ("Declared, not proven", "draws declared judges only", "this registry does not read it",
                 "declares no judge and its daemon withdraws one the chain holds", "`mute`", "never counted as `off`",
                 "not `judge_capable`", "verdict it posted on chain", "signed by the on-chain operator",
                 "the one the chain pins"):
        assert must in t, must


# -- end to end: the shipped handler, over HTTP ----------------------------------------------------------
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
            # A proven report carries the signed height and sequence (capacity_server._signed_order).
            signed = {"X-Dendra-Height": "0", "X-Dendra-Seq": "1"} if proof else {}
            req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Bench-Proof": proof,
                                                  **signed})
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


# -- the judge's memory mode and the memory-guard abstentions, declared beside the role -------------------------
# The judge is on by default and adapts to the machine: resident or on demand. Both facts are read in the miner
# container and carried in the signed report; the registry keeps them (`_clean`) and publishes only counts of nodes
# (`aggregate`): the modes, and the abstentions in bands. No row carries either (`_public_row`). Absent is `unknown`
# and `unread`, never `none` and never 0.
def _posted_mode(node_id, miner_id, mode=None, abstentions=None, proven=True, ts=None, **extra):
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": 12288, "ram_mb": 65536, "cpu_cores": 16, "tier": 3, "model": "mistral-nemo",
            "can_judge": True, "judge_backend": "cpu", "miner_ids": [miner_id], "judge_role": "active"}
    if mode is not None:
        body["judge_mode"] = mode
    if abstentions is not None:
        body["judge_memory_abstentions"] = abstentions
    body.update(extra)
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = miner_id if proven else ""
    if ts is not None:
        rep["ts"] = ts
    return rep


def test_only_the_four_modes_pass_anything_else_is_unknown():
    for good in ("resident", "on-demand", "none", "unknown"):
        assert cs._clean({"node_id": "x", "judge_mode": good})["judge_mode"] == good
    for bad in ("RESIDENT", "resident ", " none", "", "on_demand", "ondemand", "always", 1, True, None, ["resident"], {"m": "resident"}):
        assert cs._clean({"node_id": "x", "judge_mode": bad})["judge_mode"] == "unknown", bad
    assert cs._clean({"node_id": "x"})["judge_mode"] == "unknown", "an absent mode (every older kit) is unknown, never none"


def test_the_abstention_count_is_a_whole_number_or_nothing_never_zero():
    for good in (0, 1, 7, cs.ABSTENTIONS_MAX):
        assert cs._clean({"node_id": "x", "judge_memory_abstentions": good})["judge_memory_abstentions"] == good
    for bad in (-1, cs.ABSTENTIONS_MAX + 1, 1.5, "4", "", True, False, None, [4], {"n": 4}, float("nan")):
        assert cs._clean({"node_id": "x", "judge_memory_abstentions": bad})["judge_memory_abstentions"] is None, bad
    assert cs._clean({"node_id": "x"})["judge_memory_abstentions"] is None, "an absent count is not 0"


def test_no_row_publishes_the_mode_or_the_count_and_a_stored_record_without_them_is_unknown_and_unread(monkeypatch):
    # A node's count moves only when the chain seats its judge on an audit, an event the chain publishes with the
    # miner's identity: an exact count (or a mode) on a row would tie the pseudonymous node to that identity.
    stored = _posted_mode("m-stored", IDS[3], mode="resident", abstentions=2)
    del stored["judge_mode"], stored["judge_memory_abstentions"]       # a record stored before the fields existed
    a = _agg(monkeypatch, _posted_mode("m-res", IDS[0], "resident", 0), _posted_mode("m-dem", IDS[1], "on-demand", 4242),
             _posted_mode("m-none", IDS[2], "none"), stored)
    assert len(a["nodes"]) == 4
    for r in a["nodes"]:
        assert not [k for k in r if "judge_mode" in k or "abstention" in k], r
    assert "4242" not in json.dumps(a), "the exact count is published nowhere"
    assert a["judge_modes"] == {"resident": 1, "on-demand": 1, "none": 1, "unknown": 1}


def test_the_abstentions_are_counted_in_bands_of_nodes_and_an_unread_count_is_not_zero(monkeypatch):
    old = int(time.time()) - cs.STALE_S - 60
    stored = _posted_mode("m-stored", IDS[3], mode="resident")
    a = _agg(monkeypatch, _posted_mode("m-0", IDS[0], "resident", 0), _posted_mode("m-1", IDS[1], "resident", 1),
             _posted_mode("m-9", IDS[2], "resident", 9), _posted_mode("m-10", IDS[4], "resident", 10), stored,
             _posted_mode("m-unsigned", "dm1nobody1", "on-demand", 50, proven=False),
             _posted_mode("m-gone", "dm1nobody2", "resident", 3, ts=old, proven=False))
    assert a["judge_memory_abstention_bands"] == {"0": 1, "1-9": 2, "10+": 2, "unread": 1}, a["judge_memory_abstention_bands"]
    assert a["verified"]["judge_memory_abstention_bands"] == {"0": 1, "1-9": 2, "10+": 1, "unread": 1}, \
        "an unsigned declaration never enters the verified block"
    assert sum(a["judge_memory_abstention_bands"].values()) == a["live_nodes"], "every live report is counted in one band"
    assert cs._abstention_band({}) == "unread" and cs._abstention_band({"judge_memory_abstentions": 0}) == "0"


def test_the_modes_are_counted_apart_live_reports_only_and_the_verified_block_counts_the_proven_ones(monkeypatch):
    old = int(time.time()) - cs.STALE_S - 60
    a = _agg(monkeypatch, _posted_mode("m-r", IDS[0], "resident"), _posted_mode("m-d", IDS[1], "on-demand"),
             _posted_mode("m-n", IDS[2], "none"), _posted_mode("m-u", IDS[3]),
             _posted_mode("m-unsigned", IDS[4], "on-demand", proven=False), _posted_mode("m-gone", IDS[0], "resident", ts=old, proven=False))
    assert a["judge_modes"] == {"resident": 1, "on-demand": 2, "none": 1, "unknown": 1}
    assert a["verified"]["judge_modes"] == {"resident": 1, "on-demand": 1, "none": 1, "unknown": 1}, \
        "an unsigned declaration never enters the verified block"
    assert sum(a["judge_modes"].values()) == a["live_nodes"], "every live report is counted under one word"


def test_a_mode_nobody_read_is_not_a_judge_that_is_off(monkeypatch):
    a = _agg(monkeypatch, _posted_mode("m-u", IDS[3]), _posted_mode("m-n", IDS[2], "none"))
    assert a["judge_modes"]["unknown"] == 1 and a["judge_modes"]["none"] == 1


def test_the_provenance_says_the_modes_are_declared_and_that_unknown_is_not_none(monkeypatch):
    t = _agg(monkeypatch)["_provenance"]["judge_modes"]
    for must in ("Declared, not proven", "`resident`", "`on-demand`", "`none`", "never counted as `none`",
                 "`judge_memory_abstention_bands`", "`unread`", "No row carries the mode or the count itself",
                 "signed by the on-chain operator"):
        assert must in t, must


def test_over_http_the_mode_and_the_count_posted_are_the_ones_served(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    monkeypatch.setattr(cs, "_RATE", {})
    monkeypatch.setattr(cs, "_proven_miner_of", lambda h, b: (h.get("X-Bench-Proof") or "").strip())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        def post(body, proof=""):
            # A proven report carries the signed height and sequence (capacity_server._signed_order).
            signed = {"X-Dendra-Height": "0", "X-Dendra-Seq": "1"} if proof else {}
            req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Bench-Proof": proof,
                                                  **signed})
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
        common = {"backend": "gpu", "gpu": "RTX 4070", "gpu_count": 1, "vram_mb": 12288, "tier": 3, "model": "m",
                  "can_judge": True, "judge_role": "active"}
        post(dict(common, node_id="n-res", miner_ids=[IDS[0]], judge_mode="resident", judge_memory_abstentions=0), proof=IDS[0])
        post(dict(common, node_id="n-dem", miner_ids=[IDS[1]], judge_mode="on-demand", judge_memory_abstentions=5), proof=IDS[1])
        post(dict(common, node_id="n-old", miner_ids=[IDS[3]]), proof=IDS[3])
        post(dict(common, node_id="n-bad", miner_ids=[IDS[4]], judge_mode="always", judge_memory_abstentions="lots"))
        with urllib.request.urlopen(base + "/capacity", timeout=10) as r:
            a = json.loads(r.read())
        with urllib.request.urlopen(base + "/metrics", timeout=10) as r:
            metrics = r.read().decode().splitlines()
    finally:
        srv.shutdown()
        srv.server_close()
    assert sorted(r["node_id"] for r in a["nodes"]) == ["n-bad", "n-dem", "n-old", "n-res"]
    assert not [k for r in a["nodes"] for k in r if "judge_mode" in k or "abstention" in k], a["nodes"]
    assert a["judge_memory_abstention_bands"] == {"0": 1, "1-9": 1, "10+": 0, "unread": 2}
    assert a["judge_modes"] == {"resident": 1, "on-demand": 1, "none": 0, "unknown": 2}
    assert a["verified"]["judge_modes"] == {"resident": 1, "on-demand": 1, "none": 0, "unknown": 1}
    assert 'dendra_capacity_judge_mode_nodes{mode="on-demand"} 1' in metrics
    assert 'dendra_capacity_judge_mode_nodes{mode="unknown"} 2' in metrics
    assert 'dendra_capacity_judge_mode_nodes{mode="none"} 0' in metrics


def test_the_miner_route_still_publishes_no_mode_and_its_classes_do_not_split_on_it(monkeypatch):
    # GET /capacity/miner/<id> publishes the kit, the role and the age of the signed report. The mode is not among
    # them: the anonymity floor counts rows that share those three values, and a fourth value on the route would
    # shrink every class.
    rep = _posted_mode("m-r", IDS[0], "resident", 3)
    other = _posted_mode("m-d", IDS[1], "on-demand", 9)
    assert cs._report_class(rep, time.time()) == cs._report_class(other, time.time())
    assert len(cs._report_class(rep, time.time())) == 3, "the class is still kit, role and stale: three values"
