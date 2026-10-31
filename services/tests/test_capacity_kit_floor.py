# -*- coding: utf-8 -*-
"""The declared list of GET /capacity leaves out the reports of a kit below DENDRA_KIT_MIN, and counts them apart.

What is pinned, each against `kit_filter_state`, `aggregate` and the HTTP handler:
  * a report whose kit is under the floor leaves `nodes` and every top-level total, and `below_kit_min` counts it,
    stale or not; a report that declares no kit reads 0 and is under any floor; a report at the floor stays;
  * `verified` is not filtered: the signed reports of every kit stay in it, `kit_versions` included;
  * a floor that is set and cannot be read (a typo, 0, the reader missing) filters nothing and says so: `kit_filter`
    reads "unreadable" with its reason and `below_kit_min` is null, never 0;
  * no floor (unset, empty, blank) filters nothing: "dormant", `below_kit_min` null.
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

IDS = ["dm1floorcurrent000000000", "dm1flooratfloor00000000", "dm1floorold0000000000000",
       "dm1floorzero000000000000", "dm1floornone000000000000", "dm1floorstale00000000000"]
STORE_FLOOR = "3"


def _posted(node_id, miner_id, kit=None, proven=True, ts=None, drop_kit=False):
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": 12288, "ram_mb": 65536, "cpu_cores": 16, "tier": 3,
            "model": "mistral-nemo", "can_judge": True, "judge_backend": "cpu", "miner_ids": [miner_id],
            "judge_role": "active"}
    if kit is not None:
        body["kit_version"] = kit
    rep = cs._clean(json.loads(json.dumps(body)))
    if drop_kit:
        del rep["kit_version"]            # a record stored before the field existed
    rep["_proven"] = miner_id if proven else ""
    if ts is not None:
        rep["ts"] = ts
    return rep


def _mixed():
    """One report of each kind: above the floor, at it, under it, declaring 0, declaring nothing, unsigned and
    under, and a stale one under."""
    return (_posted("m-above", IDS[0], kit=4),
            _posted("m-at", IDS[1], kit=int(STORE_FLOOR)),
            _posted("m-old", IDS[2], kit=2),
            _posted("m-zero", IDS[3], kit=0),
            _posted("m-none", IDS[4], drop_kit=True),
            _posted("m-unsigned-old", "dm1floorunsigned0000000", kit=1, proven=False),
            _posted("m-stale-old", IDS[5], kit=2, ts=1))


def _agg(monkeypatch, *reps):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    return cs.aggregate({cs.storage_key(r): r for r in reps}, {"measured": False, "why": "bench"})


def _ids(a):
    return sorted(r["node_id"] for r in a["nodes"])


def test_reports_under_the_floor_leave_the_list_and_are_counted(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    a = _agg(monkeypatch, *_mixed())
    assert _ids(a) == ["m-above", "m-at"], a["nodes"]
    assert a["below_kit_min"] == 5, "kit 2, kit 0, no kit, unsigned kit 1 and the stale kit 2"
    assert a["kit_filter"] == {"state": "applied", "kit_min": 3, "why": ""}, a["kit_filter"]
    assert a["known_nodes"] == 2


def test_a_report_with_no_kit_field_reads_zero_and_is_under_the_floor(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", "1")
    a = _agg(monkeypatch, _posted("m-none", IDS[4], drop_kit=True), _posted("m-one", IDS[0], kit=1))
    assert _ids(a) == ["m-one"], a["nodes"]
    assert a["below_kit_min"] == 1


def test_a_report_at_the_floor_stays(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    a = _agg(monkeypatch, _posted("m-at", IDS[1], kit=int(STORE_FLOOR)))
    assert _ids(a) == ["m-at"]
    assert a["below_kit_min"] == 0, "applied and nothing under it: a known zero, not null"


def test_the_declared_totals_leave_with_the_list(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    all_reps = _mixed()
    a = _agg(monkeypatch, *all_reps)
    kept = _agg(monkeypatch, all_reps[0], all_reps[1])
    for k in ("live_nodes", "machines", "gpu_nodes", "gpus", "vram_total_mb", "ram_total_mb", "cpu_cores_total",
              "judge_capable", "distinct_models", "judges_declared", "judge_roles", "models", "tiers"):
        assert a[k] == kept[k], (k, a[k], kept[k])
    assert a["live_nodes"] == 2 and a["models"] == {"mistral-nemo": 2}


def test_the_stale_report_under_the_floor_is_counted_and_not_listed(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    a = _agg(monkeypatch, _posted("m-stale-old", IDS[5], kit=2, ts=1))
    assert a["nodes"] == [] and a["below_kit_min"] == 1 and a["live_nodes"] == 0


def test_verified_is_not_filtered(monkeypatch):
    all_reps = _mixed()
    monkeypatch.delenv("DENDRA_KIT_MIN", raising=False)
    before = _agg(monkeypatch, *all_reps)["verified"]
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    after = _agg(monkeypatch, *all_reps)["verified"]
    assert after == before
    assert after["live_nodes"] == 5 and after["kit_versions"] == {"4": 1, "3": 1, "2": 1, "0": 2}, after


def test_a_floor_that_cannot_be_read_filters_nothing_and_says_so(monkeypatch):
    all_reps = _mixed()
    monkeypatch.delenv("DENDRA_KIT_MIN", raising=False)
    everything = _ids(_agg(monkeypatch, *all_reps))
    assert len(everything) == len(all_reps)
    for bad in ("three", "0", "03", "-1", "3.5", "1e3", "1234567890", "3 4", "0x3"):
        monkeypatch.setenv("DENDRA_KIT_MIN", bad)
        a = _agg(monkeypatch, *all_reps)
        assert _ids(a) == everything, bad
        assert a["below_kit_min"] is None, (bad, "an unreadable floor is not a count of 0")
        assert a["kit_filter"]["state"] == "unreadable" and a["kit_filter"]["kit_min"] is None, bad
        assert a["kit_filter"]["why"], bad


def test_a_missing_floor_reader_filters_nothing_and_says_so(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    monkeypatch.setitem(sys.modules, "modea", None)          # `from modea import ...` raises ImportError
    a = _agg(monkeypatch, *_mixed())
    assert len(a["nodes"]) == 7
    assert a["below_kit_min"] is None
    assert a["kit_filter"]["state"] == "unreadable" and "kit_floor" in a["kit_filter"]["why"], a["kit_filter"]


def test_no_floor_filters_nothing_and_is_dormant(monkeypatch):
    all_reps = _mixed()
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv("DENDRA_KIT_MIN", raising=False)
        else:
            monkeypatch.setenv("DENDRA_KIT_MIN", value)
        a = _agg(monkeypatch, *all_reps)
        assert len(a["nodes"]) == 7, repr(value)
        assert a["below_kit_min"] is None, repr(value)
        assert a["kit_filter"]["state"] == "dormant" and a["kit_filter"]["kit_min"] is None, repr(value)


def test_the_reason_is_published_in_the_provenance(monkeypatch):
    monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
    a = _agg(monkeypatch, *_mixed())
    assert "below_kit_min" in a["_provenance"]["kit_floor"] and "unreadable" in a["_provenance"]["kit_floor"]


def _serve(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_RATE", {})
    monkeypatch.setattr(cs, "_proven_miner_of", lambda headers, body: "")
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set())
    monkeypatch.setattr(cs, "onchain_block", lambda: {"measured": False, "why": "bench"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(base, node_id, kit):
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu_count": 1, "vram_mb": 8192,
            "ram_mb": 32768, "cpu_cores": 8, "tier": 2, "model": "m"}
    if kit is not None:
        body["kit_version"] = kit
    req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200


def test_the_handler_serves_the_filter_and_the_metrics(tmp_path, monkeypatch):
    srv = _serve(monkeypatch, tmp_path)
    try:
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        _post(base, "m-new", 3)
        _post(base, "m-old", 1)
        _post(base, "m-nokit", None)
        monkeypatch.setenv("DENDRA_KIT_MIN", STORE_FLOOR)
        with urllib.request.urlopen(base + "/capacity", timeout=5) as r:
            doc = json.loads(r.read())
        with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
            metrics = r.read().decode()
        monkeypatch.setenv("DENDRA_KIT_MIN", "three")
        with urllib.request.urlopen(base + "/capacity", timeout=5) as r:
            bad = json.loads(r.read())
        with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
            bad_metrics = r.read().decode()
    finally:
        srv.shutdown()
        srv.server_close()
    assert [r["node_id"] for r in doc["nodes"]] == ["m-new"], doc["nodes"]
    assert doc["below_kit_min"] == 2 and doc["kit_filter"]["state"] == "applied"
    assert "dendra_capacity_below_kit_min 2" in metrics and "dendra_capacity_kit_filter_applied 1" in metrics
    assert len(bad["nodes"]) == 3 and bad["below_kit_min"] is None and bad["kit_filter"]["state"] == "unreadable"
    assert "dendra_capacity_kit_filter_applied 0" in bad_metrics
    assert "dendra_capacity_below_kit_min" not in bad_metrics, "an unreadable floor emits no count, not a 0"
