"""The judge's memory (modea/memwatch.py): what the machine can give the judge model, and the mode it runs in.

What is pinned, each half with its counterpart:
  * The readers of /proc (meminfo, vmstat, pressure) under a fabricated root: a field the kernel does not write is
    absent, never 0; a kernel without pressure accounting reads None, never "no pressure".
  * The gate before a load: open only when every reading it needs was read and the room beside the warm cache
    holds the footprint plus the headroom; closed on a pause, on stalled tasks, on a footprint above the
    container's limit.
  * The unload signals: a swap-out with little room unloads, a swap-out with plenty of room (another container's
    limit) does not; pressure at the threshold unloads; an unread pressure decides nothing.
  * The mode: a desktop starts on demand under a resident ceiling and is promoted only after
    DENDRA_MEMWATCH_PROMOTE_AFTER_S of clean readings taken with the model unloaded, never above the ceiling; a
    server starts at its ceiling; an unload sends keep_alive 0 and goes on demand with a pause that doubles.
  * The daemon's thread (Watcher) against a fake engine: the unload request it sends, the juror's last load and
    out-of-memory loads it reads.
  * Every caller of the judge engine sends the same runner options, none: the judge call and a request served by the
    miner on the judge's engine (inference.OllamaBackend) carry no num_thread and the same keep_alive.
  * The settings: each value written wrong is named; durations are read the way the engine reads them.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))

from modea import heartbeat as hb  # noqa: E402
from modea import inference  # noqa: E402
from modea import judge as J  # noqa: E402
from modea import memwatch as mw  # noqa: E402

NL = chr(10)
GIB = 1024 ** 3
MODEL = "bench-judge:latest"
# The engine's metadata of the judge model (qwen3:30b-a3b: 48 layers, 4 KV heads of 128): the case
# deploy/hw_probe.sh's bench computes on the same input. Its context cache at 4096 tokens and one slot is
# 48 x 4 x (128 + 128) x 4096 x 1 x 2 bytes = 384 MiB, the cache the engine bench measured on 2026-10-25 next to a
# 17 697 MiB file (18 167 MiB loaded).
QWEN3_MOE_INFO = {"general.architecture": "qwen3moe", "qwen3moe.block_count": 48,
                  "qwen3moe.attention.head_count_kv": 4, "qwen3moe.attention.key_length": 128,
                  "qwen3moe.attention.value_length": 128}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for v in list(os.environ):
        if v.startswith(("DENDRA_JUDGE_", "DENDRA_MEMWATCH_")) or v in ("OLLAMA_TIMEOUT", "DENDRA_STATUS_FILE",
                                                                        "DENDRA_MINER_JUDGE", "OLLAMA_ENDPOINT"):
            monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "no-daemon.json"))
    monkeypatch.setenv("DENDRA_JUDGE_STATE_FILE", str(tmp_path / "no-juror.json"))
    monkeypatch.setattr(mw, "_LOCAL", None)


def proc(tmp_path, meminfo=None, vmstat=None, pressure=None):
    """A fabricated /proc under tmp_path/root; None leaves the file out."""
    root = tmp_path / "root"
    (root / "proc" / "pressure").mkdir(parents=True, exist_ok=True)
    for rel, rows in (("proc/meminfo", meminfo), ("proc/vmstat", vmstat), ("proc/pressure/memory", pressure)):
        p = root / rel
        if rows is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(rows)
    return str(root)


def meminfo(available, active_file=2048, total=65536, swap_total=8192, swap_free=8192, drop=()):
    rows = [("MemTotal", total), ("MemFree", available // 2), ("MemAvailable", available),
            ("Active(file)", active_file), ("SwapTotal", swap_total), ("SwapFree", swap_free)]
    return "".join(f"{k}: {v * 1024:>12} kB" + NL for k, v in rows if k not in drop)


def pressure(some10=0.0, full10=0.0, some300=0.0):
    return (f"some avg10={some10:.2f} avg60=0.00 avg300={some300:.2f} total=1" + NL
            + f"full avg10={full10:.2f} avg60=0.00 avg300=0.00 total=1" + NL)


def vmstat(pswpout, pswpin=0):
    return f"nr_free_pages 12" + NL + f"pswpin {pswpin}" + NL + f"pswpout {pswpout}" + NL


CONF = {"period_s": 5.0, "psi_some_pct": 10.0, "promote_after_s": 3600.0, "headroom_mb": 0, "mem_limit_mb": None,
        "ceiling": "resident", "host_kind": "desktop", "resident_keepalive": "1h", "ondemand_keepalive": "600s"}


def reading(available=48000, active_file=2048, pswpout=0, some10=0.0, full10=0.0):
    return {"at": 0, "mem_total_mb": 65536, "mem_available_mb": available, "mem_free_mb": available // 2,
            "active_file_mb": active_file, "swap_total_mb": 8192, "swap_used_mb": 0, "pswpout": pswpout,
            "psi_some_avg10": some10, "psi_full_avg10": full10, "psi_some_avg300": 0.0}


# == The readers ==============================================================================================
def test_the_readers_read_mib_and_say_what_they_did_not_read(tmp_path):
    root = proc(tmp_path, meminfo(30000, drop=("Active(file)",)), vmstat(77), pressure(1.5, 0.25, 0.5))
    r = mw.readings(root, now=10)
    assert r["mem_available_mb"] == 30000 and r["mem_total_mb"] == 65536 and r["swap_used_mb"] == 0
    assert r["active_file_mb"] is None, "a field the kernel does not write is absent, never 0"
    assert r["pswpout"] == 77 and r["psi_some_avg10"] == 1.5 and r["psi_full_avg10"] == 0.25
    assert r["psi_some_avg300"] == 0.5


def test_a_kernel_without_pressure_accounting_reads_none_never_no_pressure(tmp_path):
    root = proc(tmp_path, meminfo(30000), vmstat(0), None)
    r = mw.readings(root)
    assert r["psi_some_avg10"] is None and r["psi_full_avg10"] is None and r["psi_some_avg300"] is None
    assert mw.read_pressure(root) is None
    # the decisions go on with the other signals
    assert mw.unload_reasons(r, 0, 2048, 19456, 10.0) == []
    assert mw.gate(r, 19456, 0, None, 2048)[0] == "open"


def test_no_meminfo_reads_nothing(tmp_path):
    root = proc(tmp_path, None, None, None)
    r = mw.readings(root)
    assert all(r[k] is None for k in r if k != "at")
    assert mw.gate(r, 100, 0, None, 10) == ("closed", "MemAvailable not read")


# == The gate =================================================================================================
@pytest.mark.parametrize("kw,state,said", [
    ({}, "open", "45952 MiB free"),
    ({"available": 20000}, "closed", "17952 MiB free beside the warm cache"),
    ({"available": None}, "closed", "MemAvailable not read"),
    ({"w_ref": None}, "closed", "Active(file) without the judge model"),
    ({"fp": None}, "closed", "footprint is not known"),
    ({"full10": 0.5}, "closed", "stalled on memory"),
    ({"limit": 10000}, "closed", "exceeds the judge container's limit"),
    ({"headroom": 30000}, "closed", "needs 49456 MiB"),
    ({"paused": 1000.0}, "closed", "paused after a memory shortage"),
])
def test_the_gate(kw, state, said):
    r = reading(available=kw.get("available") or 48000, full10=kw.get("full10", 0.0))
    if "available" in kw and kw["available"] is None:
        r["mem_available_mb"] = None
    g, why = mw.gate(r, kw.get("fp", 19456), kw.get("headroom", 0), kw.get("limit"),
                     kw.get("w_ref", 2048), kw.get("paused"), now=100.0)
    assert g == state and said in why, why


# == The unload signals =======================================================================================
def test_a_swap_out_unloads_only_when_the_room_is_short():
    tight = reading(available=15000)
    assert mw.unload_reasons(tight, 40, 2048, 19456, 10.0), "pages left for swap while the judge does not fit"
    roomy = reading(available=48000)
    assert mw.unload_reasons(roomy, 40, 2048, 19456, 10.0) == [], "another container's limit, not us"
    assert mw.unload_reasons(tight, None, 2048, 19456, 10.0) == [], "no swap-out read: no swap-out signal"
    assert mw.unload_reasons(roomy, 40, None, 19456, 10.0), "the room never read without the model: it leaves"


def test_pressure_and_the_warm_cache_unload():
    assert mw.unload_reasons(reading(some10=10.0), 0, 2048, 19456, 10.0)
    assert mw.unload_reasons(reading(some10=9.9), 0, 2048, 19456, 10.0) == []
    assert mw.unload_reasons(reading(available=1000, active_file=2048), 0, 2048, 19456, 10.0)
    # A stall of every task alone unloads nothing (measured: another container's limit put it at 1 to 2); it
    # closes the gate before a load.
    assert mw.unload_reasons(reading(full10=2.0, some10=2.0), 0, 2048, 19456, 10.0) == []
    assert mw.gate(reading(full10=0.01), 19456, 0, None, 2048)[0] == "closed"


def test_the_reclaim_of_the_load_itself_unloads_nothing():
    m = mw.MemWatch(dict(CONF, host_kind="server", headroom_mb=16000), now=0)
    m.observe(reading(available=40000, pswpout=0), loaded=False, now=0)
    # the period the model appears in: its own load swapped some pages out
    assert m.observe(reading(available=18000, pswpout=2800), loaded=True, engine_size_mb=19456, now=5) == []
    # a period spent loaded from end to end: a swap-out there unloads
    assert m.observe(reading(available=18000, pswpout=4000), loaded=True, engine_size_mb=19456, now=10) == ["unload"]


# == The mode =================================================================================================
def test_a_desktop_starts_on_demand_and_a_server_at_its_ceiling():
    assert mw.MemWatch(dict(CONF, host_kind="desktop"), now=0).mode == "on-demand"
    assert mw.MemWatch(dict(CONF, host_kind="wsl"), now=0).mode == "on-demand"
    assert mw.MemWatch(dict(CONF, host_kind=None), now=0).mode == "on-demand"
    assert mw.MemWatch(dict(CONF, host_kind="server"), now=0).mode == "resident"
    assert mw.MemWatch(dict(CONF, host_kind="server", ceiling="on-demand"), now=0).mode == "on-demand"
    m = mw.MemWatch(dict(CONF, host_kind="server", ceiling=None), now=0)
    assert m.mode == "on-demand" and "no ceiling" in m.mode_why


def test_promotion_waits_for_clean_unloaded_readings_and_never_passes_the_ceiling():
    m = mw.MemWatch(CONF, now=0)
    t = 0.0
    m.file_mb, m.kv_mb = 19456, 384
    while t < 3600:
        assert m.observe(reading(), loaded=False, now=t) == []
        assert m.mode == "on-demand", t
        t += 5
    m.observe(reading(), loaded=False, now=t)
    assert m.mode == "resident" and "clean readings" in m.mode_why
    capped = mw.MemWatch(dict(CONF, ceiling="on-demand"), now=0)
    capped.file_mb, capped.kv_mb = 19456, 384
    for t in range(0, 7300, 5):
        capped.observe(reading(), loaded=False, now=t)
    assert capped.mode == "on-demand", "never above the ceiling"


def test_one_bad_reading_starts_the_clean_period_again():
    m = mw.MemWatch(CONF, now=0)
    m.file_mb, m.kv_mb = 19456, 384
    for t in range(0, 3000, 5):
        m.observe(reading(), loaded=False, now=t)
    m.observe(reading(available=15000), loaded=False, now=3000)    # no room beside the cache
    for t in range(3005, 6600, 5):
        m.observe(reading(), loaded=False, now=t)
    assert m.mode == "on-demand"
    m.observe(reading(), loaded=False, now=6605)
    assert m.mode == "resident"


def test_readings_with_the_model_loaded_add_no_room():
    m = mw.MemWatch(CONF, now=0)
    m.file_mb, m.kv_mb = 19456, 384
    m.observe(reading(), loaded=False, now=0)
    for t in range(5, 7300, 5):
        m.observe(reading(), loaded=True, engine_size_mb=19456, now=t)
    assert m.mode == "resident", "one unloaded clean reading opened the period, the loaded ones kept it clean"
    m2 = mw.MemWatch(CONF, now=0)
    for t in range(0, 7300, 5):
        m2.observe(reading(), loaded=True, engine_size_mb=19456, now=t)
    assert m2.mode == "on-demand", "no reading with the model unloaded: no room measured, no promotion"


def test_an_unread_engine_or_meminfo_never_counts_as_clean():
    m = mw.MemWatch(CONF, now=0)
    m.file_mb, m.kv_mb = 19456, 384
    for t in range(0, 7300, 5):
        m.observe(reading(), loaded=None, now=t)
    assert m.mode == "on-demand"


def test_an_unload_goes_on_demand_with_a_pause_that_doubles():
    m = mw.MemWatch(dict(CONF, host_kind="server", headroom_mb=14000), now=0)
    m.observe(reading(), loaded=False, now=0)
    assert m.observe(reading(available=15000, pswpout=0), loaded=True, engine_size_mb=19456, now=5) == []
    acts = m.observe(reading(available=15000, pswpout=50), loaded=True, engine_size_mb=19456, now=10)
    assert acts == ["unload"] and m.mode == "on-demand" and m.unloads == 1 and m.pause_until == 70
    assert m.gate_now(reading(), 20)[0] == "closed"
    m.observe(reading(available=15000, pswpout=50), loaded=True, engine_size_mb=19456, now=80)
    acts = m.observe(reading(available=15000, pswpout=90), loaded=True, engine_size_mb=19456, now=85)
    assert acts == ["unload"] and m.pause_until == 85 + 120
    d = m.doc()
    assert d["unloads"] == 2 and d["swap_out_pages_period"] == 40 and d["footprint_source"] == "engine-ps"


def test_the_document_has_every_field_and_null_for_unread():
    m = mw.MemWatch(CONF, now=0)
    d = m.doc()
    assert set(d) == {"at", "settings_error", "ceiling", "mode", "mode_since", "mode_why", "gate", "gate_why", "resident",
                      "footprint_mb", "footprint_source", "load_s", "mem_total_mb", "mem_available_mb",
                      "active_file_ref_mb", "swap_total_mb", "swap_used_mb", "swap_out_pages_period",
                      "psi_some_avg10", "psi_full_avg10", "psi_some_avg300", "unloads", "oom_seen", "pause_until"}
    assert d["gate"] is None and d["mem_available_mb"] is None and d["unloads"] == 0 and d["oom_seen"] == 0
    assert d["settings_error"] is None


def test_the_jurors_counters_give_the_footprint_and_the_out_of_memory_loads():
    m = mw.MemWatch(CONF, now=0)
    assert m.note_juror({"last_load": {"available_before_mb": 40000, "available_after_mb": 21000, "load_s": 12.5},
                         "oom_seen": 0}) is None
    assert m.footprint() == (19000, "load-delta") and m.load_s == 12.5
    assert m.note_juror({"oom_seen": 2}) == 2 and m.doc()["oom_seen"] == 2
    assert m.note_juror({"oom_seen": 1}) == 1, "a restarted juror counts from zero again"
    assert m.doc()["oom_seen"] == 3


# == The daemon's thread against a fake engine ================================================================
class _Engine:
    def __init__(self):
        self.ps, self.tags, self.posts = [], [], []
        self.info = dict(QWEN3_MOE_INFO)   # /api/show model_info; None answers 404
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _send(self, code, doc):
                out = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                if self.path == "/api/ps":
                    return self._send(200, {"models": owner.ps})
                if self.path == "/api/tags":
                    return self._send(200, {"models": owner.tags})
                return self._send(404, {"error": "route"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/show":
                    if owner.info is None:
                        return self._send(404, {"error": "model not found"})
                    return self._send(200, {"model_info": owner.info})
                owner.posts.append(body)
                if body.get("keep_alive") == 0:
                    owner.ps = []
                return self._send(200, {"response": "", "done": True, "load_duration": 2_500_000_000})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def engine():
    e = _Engine()
    yield e
    e.close()


def test_the_watcher_unloads_with_keep_alive_0_and_publishes(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", MODEL)
    monkeypatch.setenv("DENDRA_JUDGE_MODE", "resident")
    monkeypatch.setenv("DENDRA_JUDGE_HOST_KIND", "server")
    monkeypatch.setenv("DENDRA_JUDGE_NUM_THREAD", "12")
    engine.ps = [{"name": MODEL, "size": 19 * GIB}]
    published = []
    clock = [1000.0]
    w = mw.Watcher(publish=published.append, clock=lambda: clock[0], endpoint=engine.url,
                   root=proc(tmp_path, meminfo(15000), vmstat(10), pressure()))
    assert w.step() == []
    assert published[-1]["mode"] == "resident" and published[-1]["resident"] is True
    proc(tmp_path, meminfo(15000), vmstat(60), pressure())
    clock[0] += 5
    assert w.step() == ["unload"]
    assert engine.posts[-1]["keep_alive"] == 0 and engine.posts[-1]["prompt"] == ""
    assert engine.posts[-1]["options"] == {}, "the same runner options as every caller: none"
    d = published[-1]
    assert d["mode"] == "on-demand" and "swapped out" in d["mode_why"] and d["unloads"] == 1
    assert d["footprint_mb"] == 19456 and d["footprint_source"] == "engine-ps"


def test_the_watcher_reads_the_jurors_out_of_memory_loads_and_pauses(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", MODEL)
    state = tmp_path / "juror.json"
    monkeypatch.setenv("DENDRA_JUDGE_STATE_FILE", str(state))
    engine.tags = [{"name": MODEL, "size": 19 * GIB}]
    clock = [1000.0]
    w = mw.Watcher(clock=lambda: clock[0], endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    hb.write_judge_state(MODEL, "env", path=str(state), counters={"oom_seen": 0})
    w.step()
    assert w.doc()["gate"] == "open" and w.doc()["footprint_source"] == "file+kv"
    hb.write_judge_state(MODEL, "env", path=str(state), counters={"oom_seen": 1})
    clock[0] += 5
    w.step()
    d = w.doc()
    assert d["oom_seen"] == 1 and d["gate"] == "closed" and d["pause_until"] == 1005 + 60


def test_the_load_gate_reads_now_and_a_loaded_model_passes(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", MODEL)
    engine.tags = [{"name": MODEL, "size": 19 * GIB}]
    root = proc(tmp_path, meminfo(12000), vmstat(0))
    w = mw.Watcher(endpoint=engine.url, root=root)
    ok, why = w.load_gate()
    assert ok is False and "needs 19840 MiB" in why
    engine.ps = [{"name": MODEL, "size": 19 * GIB}]
    assert w.load_gate() == (True, "the judge model is loaded")
    engine.ps = []
    proc(tmp_path, meminfo(48000), vmstat(0))
    assert w.load_gate()[0] is True


def test_the_load_gate_closes_on_an_engine_that_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", MODEL)
    w = mw.Watcher(endpoint="http://127.0.0.1:9", root=proc(tmp_path, meminfo(48000), vmstat(0)))
    ok, why = w.load_gate()
    assert ok is False and "was not read" in why


def test_settings_written_wrong_close_the_gate_and_say_why(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODE", "always")
    w = mw.Watcher(endpoint="http://127.0.0.1:9", root=proc(tmp_path, meminfo(48000), vmstat(0)))
    assert w.mode() == "on-demand" and "DENDRA_JUDGE_MODE='always'" in w.mw.mode_why
    assert w.mw.gate_now(reading(), 0)[0] == "closed"


def test_two_threads_writing_lose_nothing(tmp_path, monkeypatch):
    """The daemon's lock: the loop and the memory thread write the heartbeat at once; every write lands and the
    file always reads."""
    import miner as md
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(md, "_STATUS", {})
    monkeypatch.setattr(md, "_STATUS_WARNED", False)
    fails = []
    real = md.hb.write_atomic

    def slow_write(path, doc):
        # json.dump walks the record; a pause between two keys lets the other thread run in the middle of it.
        for _k in doc:
            time.sleep(0.00005)
        real(path, doc)
    monkeypatch.setattr(md.hb, "write_atomic", slow_write)

    def writer(name, touch):
        for i in range(150):
            if not md._status_write(_touch=touch, **{name: {"i": i}}):
                fails.append((name, i))
            md._status_note(**{f"{name}_extra_{i}": i})
    ts = [threading.Thread(target=writer, args=("loop", True)), threading.Thread(target=writer, args=("mem", False))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert fails == [] and md._STATUS_WARNED is False
    doc = json.loads((tmp_path / "status.json").read_text())
    assert doc["loop"]["i"] == 149 and doc["mem"]["i"] == 149


def test_a_thread_write_never_moves_the_loops_timestamp(tmp_path, monkeypatch):
    import miner as md
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(md, "_STATUS", {})
    clock = [100.0]
    monkeypatch.setattr(md, "time", type("T", (), {"time": staticmethod(lambda: clock[0])}))
    md._status_write(phase="loop")
    clock[0] = 900.0
    md._status_write(_touch=False, judge_memory={"at": 900})
    doc = json.loads((tmp_path / "status.json").read_text())
    assert doc["written_at"] == 100 and doc["judge_memory"]["at"] == 900


# == The same options from every caller of the judge engine ===================================================
def test_a_request_served_on_the_judges_engine_sends_the_judge_calls_options(monkeypatch, tmp_path):
    """Without a carte, the miner's requests, the presence probe and the juror's references reach the judge's engine
    through inference.OllamaBackend: they send the runner options the judge call sends (none: the threads and the
    context are set on the engine) and its keep_alive, or the engine reloads the whole model at every turn of
    callers."""
    ep = "http://ollama-cpu:11434"
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", ep)
    monkeypatch.setenv("DENDRA_JUDGE_NUM_THREAD", "12")
    seen = []

    class R:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"response": "YES", "eval_count": 1, "prompt_eval_count": 1}
    monkeypatch.setattr(inference.requests, "post", lambda url, json=None, timeout=None: (seen.append(json), R())[1])
    monkeypatch.setattr(mw, "hold_call", lambda *a, **k: None)   # the check before a load is pinned below

    class U:
        def read(self):
            return b'{"response": "YES"}'
    monkeypatch.setattr(J.urllib.request, "urlopen", lambda req, timeout=None: (seen.append(
        __import__("json").loads(req.data)), U())[1])
    inference.OllamaBackend(model=MODEL, endpoint=ep).generate("a question")
    J.llm_judge("a", "a", model=MODEL, endpoint=ep)
    served, verdict = seen
    assert "num_thread" not in served["options"] and "num_thread" not in verdict["options"]
    assert "num_ctx" not in served["options"] and "num_ctx" not in verdict["options"]
    assert served["keep_alive"] == verdict["keep_alive"] == "600s"
    # A machine with a card: its miner's engine is not the judge's, and its requests stay as they were.
    seen.clear()
    inference.OllamaBackend(model="llama", endpoint="http://ollama:11434").generate("a question")
    assert "num_thread" not in seen[0]["options"] and "keep_alive" not in seen[0]


def test_an_engine_error_keeps_its_text_for_the_out_of_memory_reading(monkeypatch):
    class R:
        status_code = 500
        text = '{"error":"llama runner process has terminated: signal: killed"}'

        def raise_for_status(self):
            raise inference.requests.HTTPError("500 Server Error", response=self)
    monkeypatch.setattr(inference.requests, "post", lambda *a, **k: R())
    with pytest.raises(inference.requests.HTTPError) as e:
        inference.OllamaBackend(model="m", endpoint="http://x").generate("q")
    assert mw.out_of_memory(e.value) and e.value.response.status_code == 500


# == Settings and durations ===================================================================================
@pytest.mark.parametrize("text,secs", [("1h", 3600.0), ("10m", 600.0), ("600s", 600.0), ("600", 600.0),
                                       ("1h30m", 5400.0), ("-1", float("inf")), ("-5m", float("inf")),
                                       ("500ms", 0.5), ("", None), ("abc", None), ("1x", None), ("h", None)])
def test_durations_are_read_as_the_engine_reads_them(text, secs):
    assert mw.parse_duration_s(text) == secs


def test_the_defaults_and_their_sources(monkeypatch):
    s = mw.settings({})
    assert (s["period_s"], s["psi_some_pct"], s["promote_after_s"], s["headroom_mb"]) == (5.0, 10.0, 3600.0, 0)
    assert s["ondemand_keepalive"] == "600s" and s["resident_keepalive"] == "1h" and s["ceiling"] is None
    monkeypatch.setenv("OLLAMA_TIMEOUT", "240")   # read in one place, inference.ollama_timeout_s
    assert mw.settings({})["ondemand_keepalive"] == "240s"
    assert mw.settings({"DENDRA_JUDGE_KEEPALIVE": "2h"})["promote_after_s"] == 7200.0
    assert mw.settings({"DENDRA_JUDGE_KEEPALIVE": "-1"})["promote_after_s"] == 3600.0


def test_every_value_written_wrong_is_named():
    with pytest.raises(mw.SettingsError) as e:
        mw.settings({"DENDRA_MEMWATCH_PERIOD_S": "0", "DENDRA_JUDGE_HEADROOM_MB": "-1", "DENDRA_JUDGE_MODE": "x",
                     "DENDRA_JUDGE_HOST_KIND": "laptop", "DENDRA_JUDGE_ONDEMAND_KEEPALIVE": "0",
                     "DENDRA_MEMWATCH_PSI_SOME_PCT": "120", "DENDRA_JUDGE_KEEPALIVE": "soon"})
    for name in ("DENDRA_MEMWATCH_PERIOD_S", "DENDRA_JUDGE_HEADROOM_MB", "DENDRA_JUDGE_MODE", "DENDRA_JUDGE_HOST_KIND",
                 "DENDRA_JUDGE_ONDEMAND_KEEPALIVE", "DENDRA_MEMWATCH_PSI_SOME_PCT", "DENDRA_JUDGE_KEEPALIVE"):
        assert name in str(e.value)


def test_the_judge_engine_is_the_one_named_and_nothing_else(monkeypatch):
    assert mw.is_judge_engine("http://ollama-cpu:11434", {}) is False, "no judge engine named: none"
    env = {"DENDRA_JUDGE_ENDPOINT": "http://ollama-cpu:11434/"}
    assert mw.is_judge_engine("http://ollama-cpu:11434", env) is True
    assert mw.is_judge_engine("http://ollama:11434", env) is False


def test_the_oom_words_are_the_engines():
    assert mw.out_of_memory("llama runner process has terminated: signal: killed")
    assert mw.out_of_memory("model requires more system memory (19.5 GiB) than is available (8.1 GiB)")
    assert not mw.out_of_memory("model 'x' not found")


def test_a_call_during_a_pause_is_held_only_while_the_model_is_not_loaded(tmp_path, engine, monkeypatch):
    p = tmp_path / "daemon.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(p))
    monkeypatch.setenv("DENDRA_MEMWATCH_PROC_ROOT", proc(tmp_path, meminfo(48000), vmstat(0), pressure()))
    engine.tags = [{"name": MODEL, "size": 19 * GIB}]
    now = int(time.time())
    p.write_text(json.dumps({"judge_memory": {"at": now, "mode": "on-demand", "pause_until": now + 300}}))
    assert "paused after a memory shortage" in mw.hold_reason(engine.url, MODEL)
    engine.ps = [{"name": MODEL, "size": 19 * GIB}]
    assert mw.hold_reason(engine.url, MODEL) == "", "loaded: the call loads nothing"
    engine.ps = []
    p.write_text(json.dumps({"judge_memory": {"at": now, "mode": "on-demand", "pause_until": now - 1}}))
    assert mw.hold_reason(engine.url, MODEL) == "", "no pause: nothing held"
    p.write_text(json.dumps({"judge_memory": {"at": now, "mode": "on-demand", "pause_until": now + 300}}))
    with pytest.raises(mw.Held):
        J.llm_judge("a", "a", model=MODEL, endpoint=engine.url)


# == The footprint before a load: the file plus its context cache ==============================================
def test_the_context_cache_is_computed_from_the_engines_metadata():
    assert mw.kv_cache_mb(QWEN3_MOE_INFO, 4096, 1) == 384
    assert mw.kv_cache_mb(QWEN3_MOE_INFO, 4096, 2) == 768, "each parallel request holds its own cache"
    per_layer = dict(QWEN3_MOE_INFO, **{"qwen3moe.attention.head_count_kv": [4] * 24 + [8] * 24})
    assert mw.kv_cache_mb(per_layer, 4096, 1) == 576, "one count per layer is summed"
    for missing in ("general.architecture", "qwen3moe.block_count", "qwen3moe.attention.head_count_kv",
                    "qwen3moe.attention.key_length", "qwen3moe.attention.value_length"):
        info = {k: v for k, v in QWEN3_MOE_INFO.items() if k != missing}
        assert mw.kv_cache_mb(info, 4096, 1) is None, f"{missing} absent is not a zero"
    assert mw.kv_cache_mb(dict(per_layer, **{"qwen3moe.attention.head_count_kv": [4] * 47}), 4096, 1) is None
    assert mw.kv_cache_mb(QWEN3_MOE_INFO, None, 1) is None and mw.kv_cache_mb(QWEN3_MOE_INFO, 4096, None) is None
    # the measured case: 17 697 MiB of file and 384 MiB of cache, 18 167 MiB loaded (engine bench, 2026-10-25)
    assert 17697 + mw.kv_cache_mb(QWEN3_MOE_INFO, 4096, 1) == 18081


def _kit_compose():
    """The kit's compose file: under DENDRA_REPO when it names the tree (the battery runs pytest from a copy of
    prototype/, which carries no deploy/testnet-miner), else next to this directory. Not found is a failure: a
    measurement was asked for and cannot be made."""
    named = os.environ.get("DENDRA_REPO") or ""
    root = Path(named) if named else MODEA.parents[1]
    p = root / "deploy" / "testnet-miner" / "docker-compose.yml"
    assert p.is_file(), f"{p} is not a file (DENDRA_REPO={named or 'unset'})"
    return p.read_text(encoding="utf-8")


def test_the_engines_context_and_slots_are_read_and_the_kits_context_is_the_composes():
    assert mw.engine_context_tokens({}) == mw.JUDGE_CONTEXT_TOKENS == 4096
    assert mw.engine_context_tokens({"OLLAMA_CONTEXT_LENGTH": "8192"}) == 8192
    assert mw.engine_context_tokens({"OLLAMA_CONTEXT_LENGTH": "big"}) is None
    assert mw.engine_num_parallel({}) == 1 and mw.engine_num_parallel({"DENDRA_JUDGE_NUM_PARALLEL": "2"}) == 2
    assert mw.engine_num_parallel({"DENDRA_JUDGE_NUM_PARALLEL": "0"}) is None
    assert mw.engine_num_parallel({"OLLAMA_NUM_PARALLEL": "2"}) == 1, "the engine's own variable is not this container's"
    compose = _kit_compose()
    block = compose.split(NL + "  ollama-cpu:" + NL, 1)[1].split(NL + NL, 1)[0]
    assert f'OLLAMA_CONTEXT_LENGTH: "{mw.JUDGE_CONTEXT_TOKENS}"' in block, "the engine's context is the one counted"
    # The miner is given the slots the engine is given, from the same line of the kit's .env.
    assert 'OLLAMA_NUM_PARALLEL: "${DENDRA_CPU_NUM_PARALLEL:-}"' in block
    miner = compose.split(NL + "  miner:" + NL, 1)[1]
    assert 'DENDRA_JUDGE_NUM_PARALLEL: "${DENDRA_CPU_NUM_PARALLEL:-}"' in miner, "the watch counts the engine's slots"


def test_a_lower_bound_of_the_footprint_never_opens_the_gate():
    r = reading(available=48000)
    # The file alone fits the limit and the room, and still does not let a load through: the cache it leaves out
    # could be what does not fit under the container's limit.
    g, why = mw.gate(r, 17697, 0, 17800, 2048, source="file")
    assert g == "closed" and "lower bound" in why, why
    # The file plus its cache is the footprint: above that limit, closed; under a wider one, open.
    assert mw.gate(r, 18081, 0, 17800, 2048, source="file+kv")[0] == "closed"
    assert mw.gate(r, 18081, 0, 18500, 2048, source="file+kv")[0] == "open"
    # A lower bound above the limit or the room proves the refusal: said with "at least".
    g, why = mw.gate(r, 17697, 0, 16602, 2048, source="file")
    assert g == "closed" and "at least 17697 MiB" in why, why
    m = mw.MemWatch(CONF, now=0)
    m.file_mb = 19456
    for t in range(0, 7300, 5):
        m.observe(reading(), loaded=False, now=t)
    assert m.mode == "on-demand" and m.gate_state == "closed", "a lower bound starts no clean period"


def test_the_watcher_counts_the_file_and_its_cache_and_a_file_alone_stays_a_lower_bound(tmp_path, engine,
                                                                                         monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", MODEL)
    engine.tags = [{"name": MODEL, "size": 17697 * 1024 * 1024}]
    w = mw.Watcher(clock=lambda: 1000.0, endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    w.step()
    d = w.doc()
    assert (d["footprint_mb"], d["footprint_source"], d["gate"]) == (18081, "file+kv", "open"), d
    engine.info = None
    w2 = mw.Watcher(clock=lambda: 1000.0, endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    w2.step()
    d2 = w2.doc()
    assert (d2["footprint_source"], d2["gate"]) == ("file", "closed") and "lower bound" in d2["gate_why"], d2


# == No metadata for the kit's judge model: its declared figures ================================================
PINNED = "qwen3:30b-a3b-instruct-2507-q4_K_M"


def test_the_declared_figures_of_the_pinned_model_give_its_file_and_cache():
    # 17 697 MiB of file and the cache of 48 layers, 4 KV heads of 128, at 4096 tokens: 384 MiB a slot.
    assert mw.declared_footprint(PINNED, 4096, 1) == 18081 and mw.declared_footprint(PINNED, 4096, 2) == 18465
    assert mw.declared_footprint(PINNED, 4096, 1, file_mb=17697) == 18081
    assert mw.declared_footprint(PINNED, 4096, 1, file_mb=17000) is None, "a file of another size is another file"
    assert mw.declared_footprint(MODEL, 4096, 1) is None, "a model without a line has no declared figure"
    assert mw.declared_footprint(PINNED, None, 1) is None and mw.declared_footprint(PINNED, 4096, None) is None
    # The figures the engine bench read: the cache of the declared metadata is the one it measured (kv_mib).
    row = mw.DECLARED_JUDGE_MODELS[PINNED]
    assert row[:5] == (17697, 48, 4, 128, 128) and mw.kv_cache_mb(QWEN3_MOE_INFO, row[5], 1) == 384


def test_metadata_without_key_length_falls_back_to_the_declared_figures_which_can_open_the_gate(tmp_path, engine,
                                                                                                monkeypatch):
    monkeypatch.setenv("DENDRA_JUDGE_MODEL_ID", PINNED)
    engine.tags = [{"name": PINNED, "size": 17697 * 1024 * 1024}]
    engine.info = {k: v for k, v in QWEN3_MOE_INFO.items() if k != "qwen3moe.attention.key_length"}
    w = mw.Watcher(clock=lambda: 1000.0, endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    w.step()
    d = w.doc()
    assert (d["footprint_mb"], d["footprint_source"], d["gate"]) == (18081, "declared", "open"), d
    # Two slots on the engine: the declared cache of each.
    w2 = mw.Watcher(env={"DENDRA_JUDGE_MODEL_ID": PINNED, "DENDRA_JUDGE_NUM_PARALLEL": "2"}, clock=lambda: 1000.0,
                    endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    w2.step()
    assert (w2.doc()["footprint_mb"], w2.doc()["footprint_source"]) == (18465, "declared"), w2.doc()
    # /api/show failing for good: the same fallback, in the daemon and in a juror's own check.
    engine.info = None
    assert mw.footprint_estimate(engine.url, PINNED, {}) == (18081, "declared")
    w3 = mw.Watcher(clock=lambda: 1000.0, endpoint=engine.url, root=proc(tmp_path, meminfo(48000), vmstat(0)))
    w3.step()
    assert (w3.doc()["footprint_source"], w3.doc()["gate"]) == ("declared", "open"), w3.doc()
    # Still a footprint: with too little room the gate stays closed.
    w4 = mw.Watcher(clock=lambda: 1000.0, endpoint=engine.url, root=proc(tmp_path, meminfo(19000), vmstat(0)))
    w4.step()
    assert w4.doc()["gate"] == "closed" and "needs 18081 MiB" in w4.doc()["gate_why"], w4.doc()


# == A load that ran out of memory under the container's limit ==================================================
def test_an_out_of_memory_load_under_the_limit_learns_that_the_model_takes_more_than_the_limit():
    # The room at the quietest moment was 39 MiB above the estimate (file+kv), and a limit was written there: the
    # gate opens, the load is killed at the limit.
    m = mw.MemWatch(dict(CONF, mem_limit_mb=18120), now=0)
    m.observe(reading(available=48000), loaded=False, file_mb=17697, kv_mb=384, now=0)
    assert m.gate_state == "open" and m.footprint() == (18081, "file+kv")
    m.note_oom(10, "the juror's engine")
    assert m.footprint() == (18121, "limit-reached")
    # Past the pause, the gate does not open again on the estimate that let the load through.
    m.observe(reading(available=48000), loaded=False, file_mb=17697, kv_mb=384, now=10 + 3600)
    assert m.gate_state == "closed" and "exceeds the judge container's limit (18120 MiB)" in m.gate_why, m.gate_why
    assert "at least 18121 MiB" in m.gate_why and m.doc()["footprint_source"] == "limit-reached"
    # A measured footprint after a load that went through takes precedence again.
    m.observe(reading(available=30000), loaded=True, engine_size_mb=18100, now=10 + 3605)
    assert m.footprint() == (18100, "engine-ps")
    # Without a limit, an out-of-memory load is the whole machine's: a pause, no bound learned.
    n = mw.MemWatch(CONF, now=0)
    n.observe(reading(available=48000), loaded=False, file_mb=17697, kv_mb=384, now=0)
    n.note_oom(10)
    assert n.footprint() == (18081, "file+kv")
    n.observe(reading(available=48000), loaded=False, now=10 + 3600)
    assert n.gate_state == "open"


def test_a_jurors_own_check_keeps_the_daemons_bound_over_its_estimate(tmp_path, engine, monkeypatch):
    status = tmp_path / "daemon.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(status))
    monkeypatch.setenv("DENDRA_JUDGE_MEM_LIMIT_MB", "18120")
    _judge_env(monkeypatch, tmp_path, engine, 48000)
    engine.tags = [{"name": MODEL, "size": 17697 * 1024 * 1024}]
    now = int(time.time())
    status.write_text(json.dumps({"judge_memory": {"at": now, "mode": "on-demand", "footprint_mb": 18121,
                                                   "footprint_source": "limit-reached", "pause_until": None}}))
    stage, why, ctx = mw.check_room(engine.url, MODEL, now=now)
    assert stage == mw.STAGE and ctx["source"] == "limit-reached" and "exceeds the judge container's limit" in why, why


# == A swap-out with the model loaded: the model is not counted twice ==========================================
def test_a_swap_out_with_the_model_loaded_compares_the_room_left_with_the_headroom_only():
    # 15 000 MiB available with the model in: 12 952 MiB beside the warm cache, less than the 18 167 MiB the model
    # takes, but that room is what is left once it is in. Another container's swap-out does not unload it.
    m = mw.MemWatch(dict(CONF, host_kind="server"), now=0)
    m.observe(reading(available=40000, pswpout=0), loaded=False, now=0)
    m.observe(reading(available=15000, pswpout=0), loaded=True, engine_size_mb=18167, now=5)
    assert m.observe(reading(available=15000, pswpout=1), loaded=True, engine_size_mb=18167, now=10) == []
    assert m.mode == "resident" and m.unloads == 0 and m.pause_until is None
    # The operator's headroom is the margin the room left must keep: below it, the swap-out unloads.
    h = mw.MemWatch(dict(CONF, host_kind="server", headroom_mb=13000), now=0)
    h.observe(reading(available=40000, pswpout=0), loaded=False, now=0)
    h.observe(reading(available=15000, pswpout=0), loaded=True, engine_size_mb=18167, now=5)
    assert h.observe(reading(available=15000, pswpout=1), loaded=True, engine_size_mb=18167, now=10) == ["unload"]
    # Without the model, the room must still hold the whole footprint.
    assert mw.unload_reasons(reading(available=15000), 1, 2048, 18167, 10.0, loaded=False)


# == WSL: no promotion without the Windows side ================================================================
def test_under_wsl_a_resident_ceiling_never_promotes_at_run_time():
    m = mw.MemWatch(dict(CONF, host_kind="wsl"), now=0)
    m.file_mb, m.kv_mb = 19456, 384
    assert m.mode == "on-demand" and "Windows side" in m.mode_why
    for t in range(0, 7300, 5):
        m.observe(reading(), loaded=False, now=t)
    assert m.mode == "on-demand" and "Windows side" in m.mode_why
    d = mw.MemWatch(dict(CONF, host_kind="desktop"), now=0)
    d.file_mb, d.kv_mb = 19456, 384
    for t in range(0, 3610, 5):
        d.observe(reading(), loaded=False, now=t)
    assert d.mode == "resident", "a desktop is still promoted after its clean hour"


# == The period: bounded below, and a setting written wrong said aloud =========================================
def test_the_period_is_bounded_below_and_a_wrong_setting_is_said_in_the_log_and_the_heartbeat(tmp_path, capsys):
    for bad in ("0.000001", "1.9", "0"):
        with pytest.raises(mw.SettingsError) as e:
            mw.settings({"DENDRA_MEMWATCH_PERIOD_S": bad})
        assert "DENDRA_MEMWATCH_PERIOD_S" in str(e.value)
    assert mw.settings({"DENDRA_MEMWATCH_PERIOD_S": "2"})["period_s"] == 2.0
    w = mw.Watcher(env={"DENDRA_MEMWATCH_PERIOD_S": "0.000001"}, endpoint="http://127.0.0.1:9",
                   root=proc(tmp_path, meminfo(48000), vmstat(0)))
    out = capsys.readouterr().out
    assert "WARNING" in out and "DENDRA_MEMWATCH_PERIOD_S" in out and mw.STAGE_SETTINGS in out, out
    assert w.conf["period_s"] == mw.PERIOD_S_DEFAULT, "the thread runs at the default period, never spins"
    d = w.doc()
    assert "DENDRA_MEMWATCH_PERIOD_S" in (d["settings_error"] or "") and d["mode"] == "on-demand"


# == Every call that would load the judge model passes the check first ========================================
def _judge_env(monkeypatch, tmp_path, engine, available):
    monkeypatch.setenv("DENDRA_MEMWATCH_PROC_ROOT", proc(tmp_path, meminfo(available), vmstat(0), pressure()))
    monkeypatch.setenv("DENDRA_JUDGE_ENDPOINT", engine.url)
    engine.tags = [{"name": MODEL, "size": 19 * GIB}]


def test_outside_a_pause_a_call_that_would_load_the_model_without_room_is_held(tmp_path, engine, monkeypatch):
    """The juror in the middle of an audit (modea/judge.py::_generate), the self-test and the application's machine
    test (inference.OllamaBackend): no pause runs, no daemon decision is read, and the machine has no room."""
    _judge_env(monkeypatch, tmp_path, engine, 12000)
    with pytest.raises(mw.Held) as e:
        J.llm_judge("a", "a", model=MODEL, endpoint=engine.url)
    assert e.value.stage == mw.STAGE and "needs 19840 MiB" in str(e.value), str(e.value)
    with pytest.raises(mw.Held):
        inference.OllamaBackend(model=MODEL, endpoint=engine.url).generate("ok")
    assert engine.posts == [], "nothing reached the engine's /api/generate"
    # Loaded, a call loads nothing and goes; with room, it goes too.
    engine.ps = [{"name": MODEL, "size": 19 * GIB}]
    assert mw.hold_check(engine.url, MODEL) == ("", "")
    engine.ps = []
    _judge_env(monkeypatch, tmp_path, engine, 48000)
    assert mw.hold_check(engine.url, MODEL) == ("", "")


def test_a_held_call_names_its_cause(tmp_path, engine, monkeypatch):
    _judge_env(monkeypatch, tmp_path, engine, 48000)
    assert mw.hold_check("http://127.0.0.1:9", MODEL)[0] == mw.STAGE_ENGINE_UNREAD
    monkeypatch.setenv("DENDRA_JUDGE_KEEPALIVE", "1 hour")
    stage, why = mw.hold_check(engine.url, MODEL)
    assert stage == mw.STAGE_SETTINGS and "DENDRA_JUDGE_KEEPALIVE" in why, why
