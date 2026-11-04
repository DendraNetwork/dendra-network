"""The /list cache of the relay, and the store version that invalidates it.

`relay._list_answer` serves a cached body while `relay._STORE_VERSION` is unchanged. The
danger is a mutation of the stored keys that does not bump the version: the relay then answers an old
queue, with a matching ETag, and a miner never sees its job. So this bench holds three things:

  * each mutation path of the shipped relay (a write, an age eviction, a boot reload) changes what the
    next /list says, and an unchanged store answers from the cache, a 304 included;
  * every function that mutates a STORE dict calls the bump (read from the shipped source by `ast`);
  * removing any one bump, in a scratch copy of the shipped file, makes a scenario below fail.

What the structural check does not see: a mutation through an alias other than `STORE[...]` or the
`d, t = STORE[kind], TS[kind]` form, or through a helper in another module. The scenarios are the
answer there, and the count of aliases it followed is printed.
"""
import ast
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# A mutation harness names its copy of the modules with DENDRA_MODEA_DIR (see conftest.py).
MODEA = Path(os.environ.get("DENDRA_MODEA_DIR") or Path(__file__).resolve().parent.parent)
SHIPPED = MODEA / "relay.py"
BUMP = "_bump_store_version_locked()"
_SEQ = [0]


def _load(path, monkeypatch, tmp_path):
    """Import the relay at `path` under a fresh module name, with a hermetic environment."""
    store = tmp_path / f"store-{_SEQ[0]}"
    _SEQ[0] += 1
    monkeypatch.setenv("DENDRA_RELAY_STORE", str(store))
    monkeypatch.setenv("DENDRA_RELAY_SIGN", "off")
    monkeypatch.setenv("DENDRA_RELAY_RATE", "100000")
    for name in ("DENDRA_RELAY_REGISTRY", "DENDRA_RELAY_TOKEN", "DENDRA_RELAY_TOKEN_PREV",
                 "DENDRA_RELAY_KIND_BUDGET_MIB", "DENDRA_RELAY_RETENTION", "DENDRA_KIT_MIN",
                 "DENDRA_KIT_DEADLINE", "DENDRA_PUBLIC"):
        monkeypatch.delenv(name, raising=False)
    saved = list(sys.path)
    sys.path.insert(0, str(MODEA))
    try:
        spec = importlib.util.spec_from_file_location(f"relay_under_test_{_SEQ[0]}", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    assert mod.BOOT_ERR == "", mod.BOOT_ERR
    return mod


def _req_keys(mod, suffix=None):
    body, _tag = mod._list_answer(suffix)
    return json.loads(body)["req"]


# -- Scenarios: each returns None when the relay is right, or a description of the stale answer -----
def scenario_write(mod):
    mod._put("req", "j1__mA", b"x")
    _req_keys(mod, "__mA")
    _req_keys(mod)
    mod._put("req", "j2__mA", b"x")
    if "j2__mA" not in _req_keys(mod, "__mA") or "j2__mA" not in _req_keys(mod):
        return "a new deposit is missing from /list"
    return None


def scenario_eviction(mod):
    # The second deposit of `keep` carries identical bytes on a write-once kind: it writes nothing and
    # returns before the write path, so only the age eviction it triggers can change the queue.
    mod._put("req", "old__mA", b"x")
    mod._put("req", "keep__mA", b"y")
    mod.TS["req"]["old__mA"] = time.time() - mod.RETENTION - 10
    if "old__mA" not in _req_keys(mod, "__mA"):
        return "setup: the old key should still be listed before an eviction"
    mod._put("req", "keep__mA", b"y")
    if "old__mA" in mod.STORE["req"]:
        return "setup: the eviction did not run"
    if "old__mA" in _req_keys(mod, "__mA"):
        return "an evicted key is still listed"
    return None


def scenario_boot(mod):
    mod._put("req", "a__mA", b"x")
    _req_keys(mod, "__mA")
    mod.DISK.write("req", "b__mA", b"x", ts=time.time())
    mod._boot_store()
    if "b__mA" not in _req_keys(mod, "__mA"):
        return "a key reloaded from disk is missing from /list"
    return None


SCENARIOS = {"write": scenario_write, "eviction": scenario_eviction, "boot": scenario_boot}


def _run(mod, tmp_path):
    out = {}
    for name, fn in SCENARIOS.items():
        mod.STORE_DIR = str(tmp_path / f"scenario-{name}-{_SEQ[0]}")
        _SEQ[0] += 1
        mod._boot_store()
        out[name] = fn(mod)
    return out


def test_shipped_relay_lists_every_mutation(monkeypatch, tmp_path):
    mod = _load(SHIPPED, monkeypatch, tmp_path)
    assert _run(mod, tmp_path) == {name: None for name in SCENARIOS}


def test_unchanged_store_is_served_from_the_cache_and_revalidates_with_304(monkeypatch, tmp_path):
    mod = _load(SHIPPED, monkeypatch, tmp_path)
    mod._put("req", "j1__mA", b"x")
    mod._put("req", "j2__mB", b"x")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, inm=None):
        req = urllib.request.Request(base + path, headers={"If-None-Match": inm} if inm else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("ETag")

    try:
        code, body, tag = get("/list?suffix=__mA")
        assert code == 200 and json.loads(body)["req"] == ["j1__mA"]
        builds = mod.LIST_CACHE_STATS["builds"]
        code, body, tag2 = get("/list?suffix=__mA", inm=tag)
        assert (code, body, tag2) == (304, b"", tag)
        code, body, _ = get("/list?suffix=__mA")
        assert code == 200 and json.loads(body)["req"] == ["j1__mA"]
        assert mod.LIST_CACHE_STATS["builds"] == builds, "an unchanged store was scanned again"
        mod._put("req", "j3__mA", b"x")
        code, body, tag3 = get("/list?suffix=__mA", inm=tag)
        assert code == 200 and json.loads(body)["req"] == ["j1__mA", "j3__mA"] and tag3 != tag
        # Every kind is still named, even empty: the zero of a read queue is never {}.
        assert sorted(json.loads(body)) == sorted(mod.STORE)
    finally:
        srv.shutdown()
        srv.server_close()


def test_cache_is_bounded_in_entries_and_bytes(monkeypatch, tmp_path):
    mod = _load(SHIPPED, monkeypatch, tmp_path)
    for i in range(20):
        mod._put("req", f"j{i}__m{i}", b"x")
    mod.LIST_CACHE_MAX_ENTRIES = 5
    for i in range(20):
        mod._list_answer(f"__m{i}")
    assert len(mod._LIST_CACHE) == 5
    assert mod._LIST_CACHE_BYTES == sum(len(v[1]) for v in mod._LIST_CACHE.values())
    one = len(mod._list_answer("__m19")[0])
    mod.LIST_CACHE_MAX_BYTES = 2 * one
    for i in range(20):
        mod._list_answer(f"__m{i}")
    assert mod._LIST_CACHE_BYTES <= mod.LIST_CACHE_MAX_BYTES
    assert mod._LIST_CACHE_BYTES == sum(len(v[1]) for v in mod._LIST_CACHE.values())
    # A body above the byte bound is served and not kept.
    mod.LIST_CACHE_MAX_BYTES = 10
    body, _ = mod._list_answer()
    assert "j0__m0" in json.loads(body)["req"] and "" not in mod._LIST_CACHE


def test_concurrent_writes_and_reads_end_on_the_store(monkeypatch, tmp_path):
    mod = _load(SHIPPED, monkeypatch, tmp_path)
    stop = threading.Event()

    def writer(w):
        for i in range(40):
            mod._put("req", f"w{w}j{i}__mA", b"x")

    def reader():
        while not stop.is_set():
            mod._list_answer("__mA")
            mod._list_answer()

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(w,)) for w in range(3)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert json.loads(mod._list_answer("__mA")[0]) == mod._snapshot_keys("__mA")
    assert json.loads(mod._list_answer()[0]) == mod._snapshot_keys()
    assert len(mod._snapshot_keys("__mA")["req"]) == 120


# -- Structure: every function that mutates a STORE dict calls the bump ------------------------------
_MUTATORS = {"clear", "pop", "popitem", "move_to_end", "update", "setdefault", "__setitem__",
             "__delitem__"}


def _store_mutating_functions(tree):
    """{function name: (mutates STORE, calls the bump)}, and the number of aliases followed."""
    out, aliases = {}, 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        names = set()
        for node in ast.walk(fn):
            # `d, t = STORE[kind], TS[kind]` and `d = STORE[kind]`
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt, val = node.targets[0], node.value
                pairs = (zip(tgt.elts, val.elts) if isinstance(tgt, ast.Tuple) and isinstance(val, ast.Tuple)
                         else [(tgt, val)])
                for t, v in pairs:
                    if (isinstance(t, ast.Name) and isinstance(v, ast.Subscript)
                            and isinstance(v.value, ast.Name) and v.value.id == "STORE"):
                        names.add(t.id)
        aliases += len(names)

        def is_store(expr):
            if isinstance(expr, ast.Name):
                return expr.id in names
            return (isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name)
                    and expr.value.id == "STORE")

        mutates = bumps = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _MUTATORS and is_store(node.func.value):
                    mutates = True
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and is_store(t.value):
                        mutates = True
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_bump_store_version_locked"):
                bumps = True
        if mutates:
            out[fn.name] = bumps
    return out, aliases


def test_every_store_mutation_calls_the_bump():
    found, aliases = _store_mutating_functions(ast.parse(SHIPPED.read_text(encoding="utf-8")))
    print(f"store-mutating functions: {sorted(found)}; STORE aliases followed: {aliases}")
    assert aliases > 0, "the alias form `d = STORE[kind]` was not seen: the check reads nothing"
    assert set(found) == {"_boot_store", "_put"}, found
    assert all(found.values()), found


# -- Mutation: removing any one bump makes a scenario fail --------------------------------------------
def _bump_lines(source):
    return [i for i, line in enumerate(source.splitlines()) if line.strip() == BUMP]


def test_removing_each_bump_turns_a_scenario_red(monkeypatch, tmp_path):
    source = SHIPPED.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    sites = _bump_lines(source)
    tree = ast.parse(source)
    where = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for i in sites:
                if fn.lineno <= i + 1 <= fn.end_lineno:
                    where[i] = fn.name
    print("bump sites:", [(where.get(i), i + 1) for i in sites])
    assert sorted(where.values()) == ["_boot_store", "_put", "_put"], where

    def expected(i):
        if where[i] == "_boot_store":
            return "boot"
        code = [ln.strip() for ln in lines[:i] if ln.strip() and not ln.strip().startswith("#")]
        return "eviction" if code[-1] == "if evicted:" else "write"

    assert sorted(expected(i) for i in sites) == sorted(SCENARIOS)
    for i in sites:
        mutant = list(lines)
        line = mutant[i]
        indent = line[: len(line) - len(line.lstrip())]
        mutant[i] = indent + "pass" + line[len(line.rstrip()):]     # keeps the line ending as shipped
        path = tmp_path / f"relay_mutant_line{i + 1}.py"
        path.write_text("".join(mutant), encoding="utf-8")
        mod = _load(path, monkeypatch, tmp_path)
        result = _run(mod, tmp_path)
        red = sorted(name for name, why in result.items() if why)
        assert red == [expected(i)], (f"bump at line {i + 1} ({where[i]}) removed: red scenarios {red}, "
                                      f"expected [{expected(i)!r}] ({result})")


# -- /stats names the cache counters, and the exporter still reads it --------------------------------
# The keys /stats served before the cache existed. The exporter and the operators' dashboards read them.
STATS_KEYS_BEFORE = {
    "counters", "occupation", "legacy_entry_cap", "entries_max_per_kind", "budget_bytes_per_kind",
    "entry_overhead_bytes", "bytes", "fill", "fill_max", "alarm_threshold", "alarm", "retention_s",
    "prev_token_uses", "signature", "kit_floor", "rate_max", "rate_window_s", "tracked_ips", "uptime_s"}


def test_stats_names_the_list_cache_and_keeps_every_key(monkeypatch, tmp_path):
    mod = _load(SHIPPED, monkeypatch, tmp_path)
    mod._put("req", "j1__mA", b"x")
    mod._list_answer("__mA")
    mod._list_answer("__mA")
    mod._list_answer()
    st = json.loads(json.dumps(mod._stats()))
    assert STATS_KEYS_BEFORE <= set(st), sorted(STATS_KEYS_BEFORE - set(st))
    assert set(st) - STATS_KEYS_BEFORE == {"list_cache"}, sorted(set(st) - STATS_KEYS_BEFORE)
    assert st["list_cache"] == {"hits": 1, "builds": 2, "entries": 2}, st["list_cache"]


def test_the_exporter_reads_the_shipped_stats(monkeypatch, tmp_path):
    """`exporter.py::refresh_relay` against the shipped relay's /stats, over HTTP on loopback.
    prometheus_client is replaced by a recording gauge so the module can be imported more than once."""
    import importlib
    import types

    mod = _load(SHIPPED, monkeypatch, tmp_path)
    mod._put("req", "j1__mA", b"x")
    mod._list_answer()

    class _Gauge(object):
        def __init__(self, *a, **k):
            self.v, self.children = None, {}

        def set(self, v):
            self.v = v

        def labels(self, **kw):
            return self.children.setdefault(tuple(sorted(kw.items())), _Gauge())

        def clear(self):
            self.children.clear()

    fake = types.ModuleType("prometheus_client")
    fake.Gauge, fake.start_http_server = _Gauge, (lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "prometheus_client", fake)
    monkeypatch.delitem(sys.modules, "exporter", raising=False)
    monkeypatch.syspath_prepend(str(MODEA))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ex = importlib.import_module("exporter")
        monkeypatch.setattr(ex, "RELAY_BASE", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setattr(ex, "RELAY_TOKEN", "")
        ex.refresh_relay()
        assert ex.g_relay_up.v == 1
        assert ex.g_relay_occ.children[(("kind", "req"),)].v == 1
        assert ex.g_relay_cap.v == mod._stats()["legacy_entry_cap"]
        assert ex.g_relay_prev.v == 0 and ex.g_relay_ips.v is not None
    finally:
        sys.modules.pop("exporter", None)
        srv.shutdown()
        srv.server_close()
