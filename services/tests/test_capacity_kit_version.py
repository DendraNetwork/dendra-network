# -*- coding: utf-8 -*-
"""The kit version a capacity report declares, against the shipped registry.

What is pinned, each against `_clean`, `_public_row`, `aggregate` and the HTTP handler:
  * the field is kept, bounded, and read 0 when absent or not an integer: a kit older than the field, never
    a current one;
  * every published row carries it, and the `verified` block counts the kits of the proven live reports only,
    with "0" the count of signed reports that declare none;
  * a report that reaches the handler is stored with the number its body declared.
The subject can be pointed at a mutated copy (DENDRA_CAPACITY_SERVER_FILE), so a harness shows each case
turns red.
"""
import importlib.util
import json
import os
import sys
import threading
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

IDS = ["dm1kitcurrent00000000000", "dm1kitold000000000000000", "dm1kitnone00000000000000",
       "dm1kitunsigned0000000000"]


def _posted(node_id, miner_id, kit=None, proven=True, ts=None):
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": 12288, "ram_mb": 65536, "cpu_cores": 16, "tier": 3,
            "model": "mistral-nemo", "can_judge": True, "judge_backend": "cpu", "miner_ids": [miner_id],
            "judge_role": "active"}
    if kit is not None:
        body["kit_version"] = kit
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = miner_id if proven else ""
    if ts is not None:
        rep["ts"] = ts
    return rep


def _agg(monkeypatch, *reps):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    return cs.aggregate({cs.storage_key(r): r for r in reps})


def _row(a, node_id):
    rows = [r for r in a["nodes"] if r["node_id"] == node_id]
    assert len(rows) == 1, a["nodes"]
    return rows[0]


def test_absent_or_not_an_integer_reads_zero_never_current():
    assert cs._clean({"node_id": "x"})["kit_version"] == 0
    for bad in ("three", None, [3], {"v": 3}, -2, ""):
        assert cs._clean({"node_id": "x", "kit_version": bad})["kit_version"] == 0, bad
    assert cs._clean({"node_id": "x", "kit_version": 3})["kit_version"] == 3
    assert cs._clean({"node_id": "x", "kit_version": "3"})["kit_version"] == 3, "the digits of a text are read"
    assert cs._clean({"node_id": "x", "kit_version": 10 ** 9})["kit_version"] == 1_000_000, "bounded"


def test_a_record_stored_before_the_field_is_published_zero(monkeypatch):
    rep = _posted("m-stored", IDS[0], kit=3)
    del rep["kit_version"]
    a = _agg(monkeypatch, rep)
    assert _row(a, "m-stored")["kit_version"] == 0
    assert a["verified"]["kit_versions"] == {"0": 1}


def test_every_row_carries_it_and_verified_counts_the_proven_live_reports_only(monkeypatch):
    a = _agg(monkeypatch,
             _posted("m-current", IDS[0], kit=3),
             _posted("m-old", IDS[1], kit=2),
             _posted("m-none", IDS[2]),
             _posted("m-unsigned", IDS[3], kit=3, proven=False),
             _posted("m-stale", "dm1kitstale0000000000000", kit=3, ts=1))
    assert _row(a, "m-current")["kit_version"] == 3
    assert _row(a, "m-old")["kit_version"] == 2
    assert _row(a, "m-none")["kit_version"] == 0
    assert _row(a, "m-unsigned")["kit_version"] == 3, "declared on the row, not counted as proven"
    assert a["verified"]["kit_versions"] == {"3": 1, "2": 1, "0": 1}, a["verified"]


def test_the_handler_stores_the_declared_number(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_RATE", {})
    monkeypatch.setattr(cs, "_proven_miner_of", lambda headers, body: "")
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/capacity" % srv.server_address[1]
        body = json.dumps({"node_id": "m-http", "machine": "abcdef123456", "backend": "gpu", "gpu_count": 1,
                           "vram_mb": 8192, "ram_mb": 32768, "cpu_cores": 8, "tier": 2, "model": "m",
                           "miner_ids": [IDS[0]], "kit_version": 3}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        with urllib.request.urlopen(url, timeout=5) as r:
            doc = json.loads(r.read())
    finally:
        srv.shutdown()
        srv.server_close()
    assert _row(doc, "m-http")["kit_version"] == 3
    assert doc["verified"]["kit_versions"] == {}, "an unproven report is counted in no adoption figure"
