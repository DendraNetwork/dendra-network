"""The judge's memory: what the machine can give the judge model now, and the mode it runs in.

The judge model is about 19 GB of weights that the CPU engine (`ollama-cpu`) loads into the machine's RAM. The
engine checks no free memory before such a load on the CPU, so a load on a machine that has no room for it
pushes the desktop, the node or the engine itself into swap until the machine freezes. This module decides,
from readings of the kernel and of the engine, whether the model may be loaded, whether it may stay loaded,
and which `keep_alive` and runner options every caller of that engine sends.

Two modes, under a ceiling join.sh writes from its survey (`DENDRA_JUDGE_MODE`):
  resident   the model stays loaded between audits (`DENDRA_JUDGE_KEEPALIVE`, one hour unset);
  on-demand  it is loaded for an audit and leaves after `DENDRA_JUDGE_ONDEMAND_KEEPALIVE` idle seconds.
The miner daemon is the one authority on the mode (`Watcher`, a thread of miner.py). It reads the
machine every `DENDRA_MEMWATCH_PERIOD_S` seconds, unloads the model at the first sign of a shortage, and only
returns to resident after `DENDRA_MEMWATCH_PROMOTE_AFTER_S` seconds of clean readings: two thresholds, so the
mode does not flap. It publishes its decision in its heartbeat under `judge_memory` (`MemWatch.doc`). The
juror reads that decision (`daemon_decision`) and, right before it loads the model, checks the room itself
(`gate`); a decision too old to trust is replaced by the juror's own readings, never by a guess.

Units: MiB everywhere, read from /proc/meminfo (kB). A value that was not read is None, never 0: an absent
reading is not a reading of zero. The /proc read is the machine's (or the WSL virtual machine's) even inside a
container; `DENDRA_MEMWATCH_PROC_ROOT` points the readers at a fabricated tree for the benches.

Every caller of the judge engine sends the same runner options (`engine_options`): none. The engine compares them
with those of the loaded model and reloads all of it when they differ (ollama 0.34.4, server/sched.go::needsReload),
so one caller that sent a thread count the others did not would reload 19 GB at every turn of callers. The threads
and the context are therefore set on the engine itself (LLAMA_ARG_THREADS in the override deploy/join.sh writes,
OLLAMA_CONTEXT_LENGTH in the compose file), where a caller that sends nothing, inside this kit or not, shares the
loaded model (dendra/onchain-staging/dendra_judge_engine_bench.sh, steps reload, envthreads and context).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

from modea import heartbeat as hb
from modea import inference

# -- Parameters ---------------------------------------------------------------------------------------------
# The period of the readings: half the 10 s window of the kernel's `avg10` pressure averages, so every
# average is seen at least once.
PERIOD_S_DEFAULT = 5.0
# The shortest period accepted: the kernel recomputes its pressure averages every 2 s (kernel/sched/psi.c,
# PSI_FREQ), so a shorter period reads the same averages again and only spins the thread.
PERIOD_S_MIN = 2.0
# The pressure (`some avg10`, percent of time some task waited on memory) that unloads the model. A chosen
# value, not a measure: the one setting of this module without a measurement behind it, to be calibrated by
# the engine bench (dendra/onchain-staging/dendra_judge_engine_bench.sh).
PSI_SOME_PCT_DEFAULT = 10.0
# Resident: the hour the kit already keeps a model loaded for (the `ollama` service of the compose), so a juror
# idle between two audits does not reload 19 GB for the next one.
RESIDENT_KEEPALIVE_DEFAULT = "1h"
# A decision older than this many periods is not the daemon's current one.
STALE_PERIODS = 3
MODES = ("resident", "on-demand")
HOST_KINDS = ("desktop", "server", "wsl")
# Where the footprint comes from. Measured: `engine-ps`, the size the engine gives the loaded model, and `load-delta`,
# the drop of MemAvailable around a load. Estimated before any load: `file+kv`, the model file plus its context cache
# (`kv_cache_mb`), and `declared`, the same sum made from the figures DECLARED_JUDGE_MODELS holds for the judge model,
# when the engine gives no metadata for it. Lower bounds, which can refuse a load but never let one through (`gate`):
# `file` alone, and `limit-reached`, learned when a load ran out of memory under the judge container's limit
# (`MemWatch.note_oom`).
FOOTPRINT_SOURCES = ("engine-ps", "load-delta", "file+kv", "declared", "file", "limit-reached")
LOWER_BOUND_SOURCES = ("file", "limit-reached")
MEASURED_SOURCES = ("engine-ps", "load-delta")
# The judge model the kit pins, as the engine bench measured it on ollama 0.34.4 (dendra/onchain-staging/
# dendra_judge_engine_bench.sh, steps model and context, on the owner's PC, 2026-10-25): the size of its file as the
# engine lists it (MiB); the four figures of its metadata that size its context cache (block_count, head_count_kv,
# key_length, value_length), whose cache at the context below and one slot is the one the bench read (kv_mib); that
# context; and the size the engine gave the loaded model (/api/ps, MiB) with one slot, then with two. One line per
# model, read here and by deploy/hw_probe.sh (the survey of a first installation runs before the model is pulled), so
# that both count the same figures. A model without a line has no declared figure.
DECLARED_JUDGE_MODELS = {
    "qwen3:30b-a3b-instruct-2507-q4_K_M": (17697, 48, 4, 128, 128, 4096, 18167, 18644),
}
# The context every model of the judge engine opens with: OLLAMA_CONTEXT_LENGTH of the ollama-cpu service in
# deploy/testnet-miner/docker-compose.yml, set there and never by a caller. tests/test_memwatch.py holds the two equal.
JUDGE_CONTEXT_TOKENS = 4096
# Bytes per element of the context cache: the engine keeps it in f16 unless told otherwise, and the kit does not.
KV_BYTES_PER_ELEMENT = 2
# The result of a presence proof held back because the model could not be loaded (miner.py).
PRESENCE_HELD = "held by the memory guard"
# The abstention stages of a juror that could not load the judge model (judge_worker.py): no room for it, settings
# written wrong, and an engine whose loaded models could not be read. Three causes, three stages: only the first is a
# lack of memory.
STAGE = "memory-guard"
STAGE_SETTINGS = "memory-settings"
STAGE_ENGINE_UNREAD = "judge-engine-unread"

# The engine's own words for a load that ran out of memory: its allocator, the kernel killing its runner, and its
# own refusal. Matched in the engine's error text, like inference.py::context_refusal.
_OOM_HINTS = ("out of memory", "out-of-memory", "cannot allocate memory", "failed to allocate",
              "unable to allocate", "requires more system memory", "signal: killed")


class SettingsError(ValueError):
    """A memory setting written wrong: the operator is told, nothing is guessed in its place."""


def parse_duration_s(text):
    """A keep_alive as the engine reads it -> seconds, or None when it is not one. A plain number is seconds;
    otherwise a sequence of number and unit (h, m, s, ms), as "1h", "10m", "1h30m" or "600s". A negative value
    keeps the model without limit and reads as infinity."""
    t = str(text if text is not None else "").strip().lower()
    if not t:
        return None
    sign = 1.0
    if t[0] in "+-":
        sign = -1.0 if t[0] == "-" else 1.0
        t = t[1:]
    try:
        v = float(t)
    except ValueError:
        v = None
    if v is not None:
        if not math.isfinite(v):
            return None
        total = v
    else:
        units = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
        total, i, n = 0.0, 0, len(t)
        if n == 0:
            return None
        while i < n:
            j = i
            while j < n and (t[j].isdigit() or t[j] == "."):
                j += 1
            if j == i:
                return None
            k = j
            while k < n and t[k].isalpha():
                k += 1
            unit = t[j:k]
            if unit not in units:
                return None
            try:
                total += float(t[i:j]) * units[unit]
            except ValueError:
                return None
            i = k
    total *= sign
    return float("inf") if total < 0 else total


def _env(env):
    return os.environ if env is None else env


def _num(env, name, kind, lo=None, hi=None, lo_open=False):
    """(value or None when unset or empty, error or "")."""
    raw = (_env(env).get(name) or "").strip()
    if not raw:
        return None, ""
    try:
        v = kind(raw)
    except ValueError:
        return None, f"{name}={raw!r} is not a number"
    if isinstance(v, float) and not math.isfinite(v):
        return None, f"{name}={raw!r} is not a finite number"
    if lo is not None and (v <= lo if lo_open else v < lo):
        return None, f"{name}={raw!r} is below {'or at ' if lo_open else ''}{lo}"
    if hi is not None and v > hi:
        return None, f"{name}={raw!r} is above {hi}"
    return v, ""


def settings(env=None) -> dict:
    """Every memory setting of the judge, read once. Raises SettingsError naming each value written wrong.

    Unset values take the defaults named next to them above, each with its reason; three have none, by design:
    no ceiling written (`DENDRA_JUDGE_MODE`) leaves the judge on demand, since only the survey may let it stay
    loaded; no host kind is read as a desktop (start on demand); no headroom adds nothing, the zero value of the
    type."""
    e = _env(env)
    errors = []
    period, err = _num(e, "DENDRA_MEMWATCH_PERIOD_S", float, lo=PERIOD_S_MIN)
    errors += [err] if err else []
    psi, err = _num(e, "DENDRA_MEMWATCH_PSI_SOME_PCT", float, lo=0, hi=100, lo_open=True)
    errors += [err] if err else []
    promote, err = _num(e, "DENDRA_MEMWATCH_PROMOTE_AFTER_S", float, lo=0, lo_open=True)
    errors += [err] if err else []
    headroom, err = _num(e, "DENDRA_JUDGE_HEADROOM_MB", int, lo=0)
    errors += [err] if err else []
    limit, err = _num(e, "DENDRA_JUDGE_MEM_LIMIT_MB", int, lo=0, lo_open=True)
    errors += [err] if err else []
    resident_ka = (e.get("DENDRA_JUDGE_KEEPALIVE") or "").strip() or RESIDENT_KEEPALIVE_DEFAULT
    if parse_duration_s(resident_ka) is None:
        errors.append(f"DENDRA_JUDGE_KEEPALIVE={resident_ka!r} is not a duration")
    ondemand_ka = (e.get("DENDRA_JUDGE_ONDEMAND_KEEPALIVE") or "").strip()
    if not ondemand_ka:
        # The longest gap between two calls of one audit is a reference generation, bounded by this value.
        ondemand_ka = f"{inference.ollama_timeout_s()}s"
    od = parse_duration_s(ondemand_ka)
    if od is None or od <= 0 or od == float("inf"):
        errors.append(f"DENDRA_JUDGE_ONDEMAND_KEEPALIVE={ondemand_ka!r} is not a positive, finite duration")
    ceiling = (e.get("DENDRA_JUDGE_MODE") or "").strip() or None
    if ceiling is not None and ceiling not in MODES:
        errors.append(f"DENDRA_JUDGE_MODE={ceiling!r} is neither {' nor '.join(MODES)}")
    host = (e.get("DENDRA_JUDGE_HOST_KIND") or "").strip() or None
    if host is not None and host not in HOST_KINDS:
        errors.append(f"DENDRA_JUDGE_HOST_KIND={host!r} is not one of {', '.join(HOST_KINDS)}")
    if errors:
        raise SettingsError("; ".join(errors))
    if promote is None:
        # The resident keep_alive: the horizon the kit already keeps a model loaded for.
        promote = parse_duration_s(resident_ka)
        if not (promote and math.isfinite(promote)):
            promote = parse_duration_s(RESIDENT_KEEPALIVE_DEFAULT)
    return {"period_s": period if period is not None else PERIOD_S_DEFAULT,
            "psi_some_pct": psi if psi is not None else PSI_SOME_PCT_DEFAULT,
            "promote_after_s": promote, "headroom_mb": headroom if headroom is not None else 0,
            "mem_limit_mb": limit,
            "resident_keepalive": resident_ka, "ondemand_keepalive": ondemand_ka,
            "ceiling": ceiling, "host_kind": host}


def engine_options(env=None) -> dict:
    """The runner options every caller of the judge engine sends, decided here and nowhere else: none. The thread
    count and the context are set on the engine (see the module's text); a caller that sent either would make the
    engine reload the model it shares with the callers that send nothing. `env` is accepted for the callers' sake."""
    return {}


def _norm(url):
    return str(url or "").strip().rstrip("/")


def judge_engine_endpoint(env=None) -> str:
    """The engine named as the judge's (`DENDRA_JUDGE_ENDPOINT`), or "" when none is named."""
    return _norm(_env(env).get("DENDRA_JUDGE_ENDPOINT"))


def is_judge_engine(endpoint, env=None) -> bool:
    """Does `endpoint` reach the engine named as the judge's? The predicate docker/entrypoint-services.sh uses to
    tell a machine whose miner serves the judge model on the CPU (OLLAMA_ENDPOINT equal to DENDRA_JUDGE_ENDPOINT)."""
    j = judge_engine_endpoint(env)
    return bool(j) and _norm(endpoint) == j


def out_of_memory(text) -> bool:
    """Does an engine's error text say the load ran out of memory?"""
    t = str(text or "").lower()
    return any(h in t for h in _OOM_HINTS)


def retry_delay(attempt, base=60.0, cap=1800.0):
    """Pause before trying again what could not be done: `base` seconds, doubling at each attempt, capped at `cap`.
    The exponent is bounded before the power. The juror's audits (judge_worker.py) and the memory pauses share it."""
    exp = min(20, max(0, int(attempt) - 1))
    return min(cap, base * (2 ** exp))


# -- Readers of the kernel ------------------------------------------------------------------------------------
def proc_root(env=None) -> str:
    return (_env(env).get("DENDRA_MEMWATCH_PROC_ROOT") or "").strip() or "/"


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_meminfo(root="/"):
    """/proc/meminfo -> {field: MiB}, or None when the file was not read. A field the kernel does not write is
    absent from the result, never 0."""
    text = _read(root, "proc/meminfo")
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        parts = rest.split()
        if not sep or not parts:
            continue
        try:
            v = int(parts[0])
        except ValueError:
            continue
        out[key.strip()] = v // 1024 if parts[1:2] == ["kB"] else v // (1024 * 1024)
    return out


def read_vmstat(root="/"):
    """/proc/vmstat -> {"pswpout": pages, "pswpin": pages} as far as written, or None when not read."""
    text = _read(root, "proc/vmstat")
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("pswpout", "pswpin"):
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def read_pressure(root="/"):
    """/proc/pressure/memory -> {"some_avg10", "some_avg300", "full_avg10", "full_avg300"}, or None when the
    kernel has no pressure accounting (no file) or it was not read: no pressure read is not "no pressure"."""
    text = _read(root, "proc/pressure/memory")
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        for p in parts[1:]:
            k, _s, v = p.partition("=")
            if k in ("avg10", "avg300"):
                try:
                    out[f"{parts[0]}_{k}"] = float(v)
                except ValueError:
                    pass
    return out or None


def readings(root=None, now=None) -> dict:
    """One reading of the machine. Every field is present; None = not read."""
    root = proc_root() if root is None else root
    mi = read_meminfo(root) or {}
    vm = read_vmstat(root) or {}
    ps = read_pressure(root) or {}
    swap_total, swap_free = mi.get("SwapTotal"), mi.get("SwapFree")
    return {"at": int(time.time() if now is None else now),
            "mem_total_mb": mi.get("MemTotal"), "mem_available_mb": mi.get("MemAvailable"),
            "mem_free_mb": mi.get("MemFree"), "active_file_mb": mi.get("Active(file)"),
            "swap_total_mb": swap_total,
            "swap_used_mb": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
            "pswpout": vm.get("pswpout"),
            "psi_some_avg10": ps.get("some_avg10"), "psi_full_avg10": ps.get("full_avg10"),
            "psi_some_avg300": ps.get("some_avg300")}


# -- The engine -------------------------------------------------------------------------------------------------
def model_ref(name) -> str:
    """A model reference with its tag, as the engine lists it (`name` without a tag is `name:latest`)."""
    n = str(name or "").strip()
    if not n:
        return ""
    return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"


def _get(endpoint, path, timeout=5.0):
    """(document, "") or (None, why)."""
    try:
        with urllib.request.urlopen(_norm(endpoint) + path, timeout=timeout) as r:
            doc = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 -- not read: said, never guessed
        return None, f"{path} not read ({type(e).__name__})"
    return (doc, "") if isinstance(doc, dict) else (None, f"{path} is not a JSON object")


def _listed(doc, model):
    want = model_ref(model)
    for m in (doc.get("models") or []) if isinstance(doc, dict) else []:
        if isinstance(m, dict) and want and want in (model_ref(m.get("name")), model_ref(m.get("model"))):
            return m
    return None


def _mib(v):
    return int(v) // (1024 * 1024) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def model_loaded(endpoint, model, timeout=5.0):
    """(loaded True/False/None, engine size MiB or None, why). None = the engine's /api/ps was not read, or no
    model is named."""
    if not model:
        return None, None, "no judge model named"
    doc, why = _get(endpoint, "/api/ps", timeout)
    if doc is None:
        return None, None, why
    m = _listed(doc, model)
    if m is None:
        return False, None, ""
    return True, _mib(m.get("size")), ""


def model_file_mb(endpoint, model, timeout=5.0):
    """The size of the model's file as the engine lists it (/api/tags), MiB, or None. A lower bound of what the
    loaded model takes: the context cache and the runner's buffers come on top."""
    if not model:
        return None
    doc, _why = _get(endpoint, "/api/tags", timeout)
    m = _listed(doc, model) if doc is not None else None
    return _mib(m.get("size")) if m is not None else None


def model_info(endpoint, model, timeout=5.0):
    """The engine's metadata of the model (`model_info` of /api/show), or None when not read."""
    if not model:
        return None
    try:
        doc = _post_to(endpoint, "/api/show", {"model": model}, timeout)
    except Exception:  # noqa: BLE001 -- not read: the context cache is then not known
        return None
    info = doc.get("model_info") if isinstance(doc, dict) else None
    return info if isinstance(info, dict) else None


def _whole(v):
    return v if type(v) is int and v > 0 else None


def engine_context_tokens(env=None):
    """The context the judge engine opens every model with: OLLAMA_CONTEXT_LENGTH when this process is given it, else
    the value the kit sets on that engine (JUDGE_CONTEXT_TOKENS). None when it is written but is no positive count."""
    raw = (_env(env).get("OLLAMA_CONTEXT_LENGTH") or "").strip()
    if not raw:
        return JUDGE_CONTEXT_TOKENS
    try:
        return _whole(int(raw))
    except ValueError:
        return None


def engine_num_parallel(env=None):
    """The requests the judge engine serves at once, each with its own context cache: DENDRA_JUDGE_NUM_PARALLEL, which
    the kit gives this container with the figure it gives the engine as OLLAMA_NUM_PARALLEL (the miner service of
    deploy/testnet-miner/docker-compose.yml, and the override deploy/join.sh writes for the judge role on the CPU).
    Unset, the engine's own single slot (1). None when it is written but is no positive count."""
    raw = (_env(env).get("DENDRA_JUDGE_NUM_PARALLEL") or "").strip()
    if not raw:
        return 1
    try:
        return _whole(int(raw))
    except ValueError:
        return None


def kv_cache_mb(info, context_tokens, num_parallel):
    """The context cache of a model, MiB rounded up, from the engine's metadata: block_count x head_count_kv x
    (key_length + value_length) x context x parallel requests x KV_BYTES_PER_ELEMENT. head_count_kv may be one count
    per layer. None when one of them is not read: a missing field is never taken as zero.

    The formula deploy/hw_probe.sh applies to the same metadata; the benches of both compare it on the same input."""
    if not isinstance(info, dict) or _whole(context_tokens) is None or _whole(num_parallel) is None:
        return None
    arch = info.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None
    blocks = _whole(info.get(f"{arch}.block_count"))
    k = _whole(info.get(f"{arch}.attention.key_length"))
    v = _whole(info.get(f"{arch}.attention.value_length"))
    heads = info.get(f"{arch}.attention.head_count_kv")
    if blocks is None or k is None or v is None:
        return None
    if isinstance(heads, list):
        per_layer = [_whole(h) for h in heads]
        if len(per_layer) != blocks or None in per_layer:
            return None
        head_sum = sum(per_layer)
    elif _whole(heads) is not None:
        head_sum = blocks * heads
    else:
        return None
    nbytes = head_sum * (k + v) * context_tokens * num_parallel * KV_BYTES_PER_ELEMENT
    return -(-nbytes // (1024 * 1024))


def declared_footprint(model, context_tokens, num_parallel, file_mb=None):
    """The judge model's footprint from its declared figures (DECLARED_JUDGE_MODELS) -> MiB or None: the declared file
    plus the context cache the declared metadata gives at this context and these slots (`kv_cache_mb`). None for a
    model without a line, a context or slot count that is not known, or a file the engine lists at another size than
    the declared one (the line then describes another file)."""
    row = DECLARED_JUDGE_MODELS.get(model_ref(model)) if model else None
    if row is None:
        return None
    dfile, blocks, heads, k, v = row[:5]
    if file_mb is not None and file_mb != dfile:
        return None
    info = {"general.architecture": "declared", "declared.block_count": blocks,
            "declared.attention.head_count_kv": heads, "declared.attention.key_length": k,
            "declared.attention.value_length": v}
    kv = kv_cache_mb(info, context_tokens, num_parallel)
    return dfile + kv if kv is not None else None


def footprint_estimate(endpoint, model, env=None, timeout=5.0):
    """The judge model's footprint before it is loaded -> (MiB, source) or (None, None): the file plus its context
    cache (`file+kv`); when the engine gives no metadata for the cache, the declared figures of that model (`declared`);
    else the file alone (`file`, a lower bound)."""
    f = model_file_mb(endpoint, model, timeout)
    if f is None:
        return None, None
    ctx, par = engine_context_tokens(env), engine_num_parallel(env)
    kv = kv_cache_mb(model_info(endpoint, model, timeout), ctx, par)
    if kv is not None:
        return f + kv, "file+kv"
    d = declared_footprint(model, ctx, par, file_mb=f)
    return (d, "declared") if d is not None else (f, "file")


def engine_lists(endpoint, model, timeout=5.0):
    """Does the engine hold `model` (/api/tags)? True/False, or raises when the engine did not answer: the start
    probe of a process whose engine is the judge's, which must not load 19 GB to learn that the engine answers."""
    with urllib.request.urlopen(_norm(endpoint) + "/api/tags", timeout=timeout) as r:
        doc = json.loads(r.read())
    if not model:
        return True
    return _listed(doc, model) is not None


def _post_to(endpoint, path, payload, timeout):
    req = urllib.request.Request(_norm(endpoint) + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(endpoint, payload, timeout):
    return _post_to(endpoint, "/api/generate", payload, timeout)


def load_model(endpoint, model, keep_alive, options=None, timeout=None) -> dict:
    """Loads `model` (an empty prompt) and returns {"ok", "load_s", "why", "oom"}. `load_s` is the engine's
    `load_duration` when it gives one, otherwise the time the call took. Never raises."""
    timeout = inference.ollama_timeout_s() if timeout is None else timeout
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive,
               "options": dict(options or {})}
    t0 = time.monotonic()
    try:
        doc = _post(endpoint, payload, timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 -- the body never masks the error
            pass
        text = f"HTTP {e.code}: {' '.join(body.split())[:200]}"
        return {"ok": False, "load_s": None, "why": text, "oom": out_of_memory(body)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "load_s": None, "why": f"{type(e).__name__}: {e}"[:240], "oom": out_of_memory(e)}
    took = time.monotonic() - t0
    ld = doc.get("load_duration") if isinstance(doc, dict) else None
    if isinstance(doc, dict) and doc.get("error"):
        return {"ok": False, "load_s": None, "why": str(doc.get("error"))[:240], "oom": out_of_memory(doc["error"])}
    load_s = (ld / 1e9) if isinstance(ld, (int, float)) and not isinstance(ld, bool) and ld > 0 else round(took, 3)
    return {"ok": True, "load_s": round(load_s, 3), "why": "", "oom": False}


def unload_model(endpoint, model, options=None, timeout=30.0):
    """Asks the engine to drop `model` (an empty prompt, keep_alive 0): it does so once the call in progress, if
    any, is over. -> "" when the engine took it, else why. Never raises."""
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0, "options": dict(options or {})}
    try:
        _post(endpoint, payload, timeout)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:200]
    return ""


# -- The gate before a load -------------------------------------------------------------------------------------
def gate(r, footprint_mb, headroom_mb, mem_limit_mb, w_ref_mb, paused_until=None, now=None, source=None):
    """May the judge model be loaded now? -> ("open" | "closed", why). Every reading it needs must have been read:
    an unread reading closes the gate, it is never a pass.

    Open when MemAvailable minus the warm file cache (`w_ref_mb`, Active(file) read while the model was not
    loaded) holds the model's footprint plus the operator's headroom, the footprint fits the container's limit,
    no pause is running, and no task is stalled on memory (`full avg10` = 0, when the kernel reports it).

    A footprint whose `source` is a lower bound (the file alone, or more than a limit a load ran out of memory under)
    can close the gate, since the model takes at least that much, but never opens it: what it leaves out could be what
    does not fit."""
    now = time.time() if now is None else now
    if paused_until is not None and now < paused_until:
        return "closed", f"paused after a memory shortage until {int(paused_until)} ({int(paused_until - now)} s)"
    a = r.get("mem_available_mb")
    if a is None:
        return "closed", "MemAvailable not read"
    if w_ref_mb is None:
        return "closed", "the warm file cache (Active(file) without the judge model) not read yet"
    if footprint_mb is None:
        return "closed", "the judge model's footprint is not known (the engine lists no size for it)"
    full = r.get("psi_full_avg10")
    if full is not None and full > 0:
        return "closed", f"tasks are stalled on memory (full avg10 {full:g})"
    lower = source in LOWER_BOUND_SOURCES
    at_least = "at least " if lower else ""
    if mem_limit_mb is not None and footprint_mb > mem_limit_mb:
        return "closed", (f"the model ({at_least}{footprint_mb} MiB) exceeds the judge container's limit "
                          f"({mem_limit_mb} MiB)")
    room = a - w_ref_mb
    need = footprint_mb + (headroom_mb or 0)
    if room < need:
        return "closed", (f"{room} MiB free beside the warm cache ({a} available - {w_ref_mb} cache), the model "
                          f"needs {at_least}{need} MiB")
    if lower:
        return "closed", (f"the model's footprint is known only as a lower bound (its file, {footprint_mb} MiB): the "
                          f"engine gave no context-cache size for it (/api/show model_info), no figure is declared for "
                          f"it, and a load is never let through on a lower bound")
    return "open", f"{room} MiB free beside the warm cache, the model needs {need} MiB"


def unload_reasons(r, swap_out_pages, w_ref_mb, footprint_mb, psi_some_pct, loaded=False, headroom_mb=0):
    """What says the model must leave now, read on the readings of one period. Each signal counts only when it
    was read; the result names every one that fired.

    A swap-out weighs the room beside the warm cache. With the model loaded, MemAvailable already leaves the model
    out, so that room is compared with the operator's headroom alone; comparing it with the footprint would count the
    model twice and unload a judge that fits for another container's swap-out. Without the model, the room must hold
    the footprint.

    A stall of every task (`full avg10` above 0) is not one of them. /proc/pressure/memory counts the stalls of a
    container held at its own memory limit with those of the whole machine: on the owner's PC (WSL, 32 GiB), the
    judge model loaded in another container under a memory limit put `full avg10` at 1 to 2 for about a minute,
    equal to `some avg10` there, while the machine kept 12 GiB available (5 s readings, 2026-10-25). Unloading on
    it would unload the judge for another container's limit. It closes the gate before a load (`gate`), and the
    pressure threshold covers a lasting stall, since `full` never exceeds `some`."""
    out = []
    a = r.get("mem_available_mb")
    some = r.get("psi_some_avg10")
    room = a - w_ref_mb if a is not None and w_ref_mb is not None else None
    if swap_out_pages and room is not None and loaded:
        if room < (headroom_mb or 0):
            out.append(f"{swap_out_pages} pages swapped out while {room} MiB are free beside the warm cache with the "
                       f"model loaded (headroom {headroom_mb or 0} MiB)")
    elif swap_out_pages and room is not None and footprint_mb is not None:
        if room < footprint_mb:
            out.append(f"{swap_out_pages} pages swapped out while {room} MiB are free beside the warm cache")
    elif swap_out_pages:
        # The room was never read without the model (a daemon started while it was loaded): a swap-out cannot be
        # told from another container's, and the model leaves, the side that keeps the machine responsive.
        out.append(f"{swap_out_pages} pages swapped out, and the room beside the warm cache is not known")
    if some is not None and some >= psi_some_pct:
        out.append(f"memory pressure (some avg10 {some:g} >= {psi_some_pct:g})")
    if a is not None and w_ref_mb is not None and a < w_ref_mb:
        out.append(f"the warm file cache is being taken ({a} MiB available < {w_ref_mb} MiB cache)")
    return out


# -- The state machine of the mode ------------------------------------------------------------------------------
class MemWatch:
    """The mode of the judge, decided on readings (`observe`), with no input or output of its own: the
    Watcher feeds it and runs what it asks for. Starts on demand except on a server whose ceiling is resident
    (a desktop's next game is not in thirty seconds of survey; an hour of readings sees more).

    Under WSL it never leaves on demand: these readings are the virtual machine's, and the Windows side, whose next
    program can push the model's pages out to the page file, is read only by the install survey (deploy/hw_probe.sh,
    windows_available_mb). A container cannot read it at run time, so a resident ceiling stays a ceiling there."""

    def __init__(self, conf, now=None, settings_error=""):
        now = time.time() if now is None else now
        self.conf = conf or {}
        self.settings_error = settings_error
        self.ceiling = self.conf.get("ceiling")
        if settings_error:
            self.mode, why = "on-demand", f"the memory settings are written wrong: {settings_error}"
        elif self.ceiling == "resident" and self.conf.get("host_kind") == "server":
            self.mode, why = "resident", "a server whose survey allows a resident judge"
        elif self.ceiling == "resident" and self.conf.get("host_kind") == "wsl":
            self.mode, why = "on-demand", ("WSL: the Windows side is read only by the install survey, never at run "
                                           "time from a container, so the judge stays on demand")
        elif self.ceiling == "resident":
            self.mode, why = "on-demand", (f"a {self.conf.get('host_kind') or 'machine of unknown kind'} starts on "
                                           f"demand; resident after {int(self.conf['promote_after_s'])} s of clean "
                                           f"readings")
        elif self.ceiling == "on-demand":
            self.mode, why = "on-demand", "the survey's ceiling is on demand"
        else:
            self.mode, why = "on-demand", "no ceiling written by join.sh (DENDRA_JUDGE_MODE): on demand"
        self.mode_since, self.mode_why = int(now), why
        self.gate_state, self.gate_why = None, ""
        self.resident = None
        self.w_ref = None
        self.prev_pswpout = None
        self.prev_loaded = None
        self.swap_out_period = None
        self.r = {}
        self.engine_ps_mb = None
        self.load_delta_mb = None
        self.file_mb = None
        self.kv_mb = None
        self.declared_mb = None
        self.limit_floor_mb = None
        self.load_s = None
        self.unloads = 0
        self.oom_own = 0
        self.oom_juror = 0
        self._juror_oom_last = None
        self.strikes = 0
        self.pause_until = None
        self.clean_since = None
        self.clean_unloaded = False
        self.at = None
        self.last_reasons = []

    # The footprint, by preference: the engine's own size of the loaded model, the drop of MemAvailable around
    # a load the juror made, the model file plus its context cache, the declared figures of the model, then the file
    # alone (a lower bound). A lower bound learned from a load that ran out of memory under the container's limit
    # (`note_oom`) replaces any estimate below it: the estimate is what let that load through.
    def footprint(self):
        if self.engine_ps_mb is not None:
            return self.engine_ps_mb, "engine-ps"
        if self.load_delta_mb is not None:
            return self.load_delta_mb, "load-delta"
        est, src = None, None
        if self.file_mb is not None and self.kv_mb is not None:
            est, src = self.file_mb + self.kv_mb, "file+kv"
        elif self.file_mb is not None and self.declared_mb is not None:
            est, src = self.declared_mb, "declared"
        elif self.file_mb is not None:
            est, src = self.file_mb, "file"
        if self.limit_floor_mb is not None and (est is None or est < self.limit_floor_mb):
            return self.limit_floor_mb, "limit-reached"
        return est, src

    def _set_mode(self, mode, why, now):
        if mode != self.mode:
            self.mode, self.mode_since = mode, int(now)
        self.mode_why = why

    def note_juror(self, counters):
        """The juror's counters (its judge state): its last load, and the loads it saw run out of memory."""
        if not isinstance(counters, dict):
            return None
        ll = counters.get("last_load")
        if isinstance(ll, dict):
            b, a = ll.get("available_before_mb"), ll.get("available_after_mb")
            if isinstance(b, int) and isinstance(a, int) and b > a:
                self.load_delta_mb = b - a
            if isinstance(ll.get("load_s"), (int, float)) and not isinstance(ll.get("load_s"), bool):
                self.load_s = ll["load_s"]
        n = counters.get("oom_seen")
        if not isinstance(n, int) or isinstance(n, bool):
            return None
        last, self._juror_oom_last = self._juror_oom_last, n
        if last is None:
            self.oom_juror = n
            return None
        new = n - last if n >= last else n   # a restarted juror counts from zero again
        self.oom_juror += new
        return new

    def note_oom(self, now, who="the engine"):
        """A load ran out of memory: on demand, and a pause that doubles at each strike.

        Under a memory limit on the judge container, with a footprint that was only estimated and fit that limit, the
        load is read as stopped by that limit: the model takes more than the limit, and that lower bound replaces the
        estimate (`footprint`), so the gate does not open again on the figure that let this load through. It stays
        closed, said as a model above the container's limit, until an installation writes another limit and the daemon
        starts again. The gate asked for the room before the load, so a shortage of the whole machine is not the
        likely cause; that it is the limit is an inference (another program may have taken the room during the load)."""
        lim = self.conf.get("mem_limit_mb")
        fp, src = self.footprint()
        if lim is not None and src not in MEASURED_SOURCES and (fp is None or fp <= lim):
            self.limit_floor_mb = lim + 1
        self.strikes += 1
        self.pause_until = now + retry_delay(self.strikes)
        self.clean_since, self.clean_unloaded = None, False
        self._set_mode("on-demand", f"{who} ran out of memory loading the judge model", now)

    def observe(self, r, loaded, engine_size_mb=None, file_mb=None, now=None, kv_mb=None, declared_mb=None):
        """One period. `loaded` is what the engine's /api/ps said (None = not read). -> list of actions: "unload"
        when the model must leave now."""
        now = time.time() if now is None else now
        self.at = int(now)
        self.r = dict(r)
        self.resident = loaded
        if file_mb is not None:
            self.file_mb = file_mb
        if kv_mb is not None:
            self.kv_mb = kv_mb
        if declared_mb is not None:
            self.declared_mb = declared_mb
        if loaded and engine_size_mb is not None:
            self.engine_ps_mb = engine_size_mb
        if loaded is False and r.get("active_file_mb") is not None:
            self.w_ref = r["active_file_mb"]
        p = r.get("pswpout")
        self.swap_out_period = ((p - self.prev_pswpout)
                                if p is not None and self.prev_pswpout is not None and p >= self.prev_pswpout
                                else None)
        self.prev_pswpout = p
        fp, src = self.footprint()
        psi_pct = self.conf.get("psi_some_pct", PSI_SOME_PCT_DEFAULT)
        headroom = self.conf.get("headroom_mb") or 0
        # A swap-out unloads only over a period the model spent loaded from end to end: the reclaim its own load may
        # cause falls in the period it appears in (an inference: no load with room was measured here).
        swap_while_loaded = self.swap_out_period if (loaded and self.prev_loaded) else None
        reasons = unload_reasons(r, swap_while_loaded, self.w_ref, fp, psi_pct, loaded=bool(loaded),
                                 headroom_mb=headroom)
        self.prev_loaded = loaded
        self.last_reasons = reasons
        actions = []
        if loaded and reasons:
            actions.append("unload")
            self.unloads += 1
            self.strikes += 1
            self.pause_until = now + retry_delay(self.strikes)
            self._set_mode("on-demand", "unloaded: " + "; ".join(reasons), now)
        # Clean readings toward the promotion: none of the signals above, every reading read, and while the model
        # is not loaded, room for it beside the warm cache. Readings with the model loaded add no room (a mapped
        # model counts part of its pages as available).
        # A lower bound of the footprint proves no room, so it starts no clean period.
        unread = (r.get("mem_available_mb") is None or self.w_ref is None or fp is None
                  or src in LOWER_BOUND_SOURCES)
        stalled = (r.get("psi_full_avg10") or 0) > 0
        swapped = bool(unload_reasons(r, self.swap_out_period, self.w_ref, fp, psi_pct, loaded=bool(loaded),
                                      headroom_mb=headroom))
        bad = (bool(reasons) or swapped or stalled or unread
               or (self.pause_until is not None and now < self.pause_until))
        if not bad and loaded is False:
            if r["mem_available_mb"] - self.w_ref < fp + headroom:
                bad = True
        if loaded is None:
            bad = True
        if bad:
            self.clean_since, self.clean_unloaded = None, False
        else:
            if self.clean_since is None:
                self.clean_since = now
            if loaded is False:
                self.clean_unloaded = True
            if self.clean_unloaded and now - self.clean_since >= self.conf.get("promote_after_s", math.inf):
                self.strikes = 0
                if (self.mode == "on-demand" and self.ceiling == "resident" and not self.settings_error
                        and self.conf.get("host_kind") != "wsl"):
                    self._set_mode("resident", f"{int(now - self.clean_since)} s of clean readings", now)
        g, why = self.gate_now(r, now)
        self.gate_state, self.gate_why = g, why
        return actions

    def gate_now(self, r, now):
        if self.settings_error:
            return "closed", f"the memory settings are written wrong: {self.settings_error}"
        fp, src = self.footprint()
        return gate(r, fp, self.conf.get("headroom_mb"), self.conf.get("mem_limit_mb"), self.w_ref,
                    self.pause_until, now, source=src)

    def doc(self) -> dict:
        """The `judge_memory` object of the daemon's heartbeat. Every field is written; null = not read.
        `settings_error` names the memory settings written wrong (null when they read): with it the juror loads
        nothing and abstains at STAGE_SETTINGS, and the reader is told why."""
        fp, src = self.footprint()
        r = self.r
        return {"at": self.at, "settings_error": self.settings_error or None,
                "ceiling": self.ceiling, "mode": self.mode, "mode_since": self.mode_since,
                "mode_why": self.mode_why, "gate": self.gate_state, "gate_why": self.gate_why,
                "resident": self.resident, "footprint_mb": fp, "footprint_source": src, "load_s": self.load_s,
                "mem_total_mb": r.get("mem_total_mb"), "mem_available_mb": r.get("mem_available_mb"),
                "active_file_ref_mb": self.w_ref, "swap_total_mb": r.get("swap_total_mb"),
                "swap_used_mb": r.get("swap_used_mb"), "swap_out_pages_period": self.swap_out_period,
                "psi_some_avg10": r.get("psi_some_avg10"), "psi_full_avg10": r.get("psi_full_avg10"),
                "psi_some_avg300": r.get("psi_some_avg300"), "unloads": self.unloads,
                "oom_seen": self.oom_own + self.oom_juror,
                "pause_until": int(self.pause_until) if self.pause_until is not None else None}


# -- The daemon's thread ----------------------------------------------------------------------------------------
_LOCAL = None   # the Watcher of this process, when it runs one: its callers read it, not the file it writes


def judge_model_named(env=None):
    """The judge model as the juror resolved it (its state file), or the kit's names when that state is not read."""
    doc, _why = hb.read(hb.judge_state_path(), what="judge state")
    if doc and isinstance(doc.get("model"), str) and doc["model"].strip():
        return doc["model"].strip()
    e = _env(env)
    return ((e.get("DENDRA_JUDGE_MODEL_OVERRIDE") or "").strip() or (e.get("DENDRA_JUDGE_MODEL_ID") or "").strip()
            or None)


class Watcher:
    """The daemon's memory thread: reads every period, runs the MemWatch's actions, publishes its document.

    `publish(doc)` writes the heartbeat (miner._status_write without touching the loop's timestamp). A
    period that raises is recorded in the document and the thread goes on: it dies only with its process."""

    def __init__(self, publish=None, env=None, clock=time.time, root=None, endpoint=None):
        self.env = env
        self.clock = clock
        self.root = root
        self.publish = publish
        self.lock = threading.RLock()
        err = ""
        try:
            conf = settings(env)
        except SettingsError as e:
            err = str(e)
            conf = {"period_s": PERIOD_S_DEFAULT, "psi_some_pct": PSI_SOME_PCT_DEFAULT,
                    "promote_after_s": parse_duration_s(RESIDENT_KEEPALIVE_DEFAULT), "headroom_mb": 0,
                    "mem_limit_mb": None, "ceiling": None, "host_kind": None,
                    "resident_keepalive": RESIDENT_KEEPALIVE_DEFAULT,
                    "ondemand_keepalive": f"{inference.ollama_timeout_s()}s"}
        self.conf = conf
        self.endpoint = _norm(endpoint) if endpoint is not None else None
        self.mw = MemWatch(conf, now=clock(), settings_error=err)
        self._stop = threading.Event()
        self._thread = None
        self._kv = (None, None)   # (model, context cache MiB) once the engine's metadata gave it
        if err:
            print(f"[memwatch] WARNING: the judge's memory settings are written wrong ({err}). No judge model is loaded "
                  f"until they are fixed: the juror abstains on every audit at stage {STAGE_SETTINGS}, and the heartbeat "
                  f"says so (judge_memory.settings_error). Fix the value in the kit's .env and restart.", flush=True)

    def _estimate(self, ep, model):
        """The file, the context cache and the declared footprint of the judge model; the cache read once per model,
        the declared footprint only while the engine gives no cache (`declared_footprint`)."""
        file_mb = model_file_mb(ep, model)
        ctx, par = engine_context_tokens(self.env), engine_num_parallel(self.env)
        if self._kv[0] != model or self._kv[1] is None:
            self._kv = (model, kv_cache_mb(model_info(ep, model), ctx, par))
        decl = (declared_footprint(model, ctx, par, file_mb=file_mb)
                if self._kv[1] is None and file_mb is not None else None)
        return file_mb, self._kv[1], decl

    def _endpoint(self):
        if self.endpoint is not None:
            return self.endpoint
        from modea.judge import judge_endpoint
        return _norm(judge_endpoint())

    def step(self):
        """One period: read, decide, act, publish. Returns the actions run."""
        now = self.clock()
        ep = self._endpoint()
        model = judge_model_named(self.env)
        r = readings(self.root if self.root is not None else proc_root(self.env), now)
        loaded, size, _why = model_loaded(ep, model)
        file_mb, kv_mb, decl = self._estimate(ep, model) if size is None else (None, None, None)
        doc, _w = hb.read(hb.judge_state_path(), what="judge state")
        with self.lock:
            if doc is not None and isinstance(doc.get("counters"), dict):
                new = self.mw.note_juror(doc["counters"])
                if new:
                    self.mw.note_oom(now, "the juror's engine")
            actions = self.mw.observe(r, loaded, size, file_mb, now, kv_mb=kv_mb, declared_mb=decl)
        for act in actions:
            if act == "unload" and model:
                why = unload_model(ep, model, _safe_options(self.env))
                if why:
                    print(f"[memwatch] the judge model could not be unloaded ({why}); it leaves at its keep_alive",
                          flush=True)
                else:
                    print(f"[memwatch] judge model unloaded: {self.mw.mode_why}", flush=True)
        self._publish()
        return actions

    def _publish(self):
        if self.publish is None or self._stop.is_set():
            return
        try:
            with self.lock:
                d = self.mw.doc()
            self.publish(d)
        except Exception as e:  # noqa: BLE001 -- the heartbeat never takes the thread down
            print(f"[memwatch] judge_memory not written ({type(e).__name__}: {e})", flush=True)

    def doc(self):
        with self.lock:
            return self.mw.doc()

    def mode(self):
        with self.lock:
            return self.mw.mode

    def note_oom(self, who="the engine"):
        with self.lock:
            self.mw.oom_own += 1
            self.mw.note_oom(self.clock(), who)
        self._publish()

    def load_gate(self):
        """The check right before the daemon itself may load the model (a served request or a presence probe on a
        machine whose miner serves the judge model): fresh readings, never the last period's. -> (open, why)."""
        now = self.clock()
        ep = self._endpoint()
        model = judge_model_named(self.env)
        loaded, size, why = model_loaded(ep, model)
        if loaded:
            return True, "the judge model is loaded"
        r = readings(self.root if self.root is not None else proc_root(self.env), now)
        with self.lock:
            fp, src = self.mw.footprint()
            need_estimate = fp is None or src in LOWER_BOUND_SOURCES
        estimate = self._estimate(ep, model) if need_estimate else (None, None, None)
        with self.lock:
            if loaded is False and r.get("active_file_mb") is not None:
                self.mw.w_ref = r["active_file_mb"]
            if estimate[0] is not None:
                self.mw.file_mb = estimate[0]
            if estimate[1] is not None:
                self.mw.kv_mb = estimate[1]
            if estimate[2] is not None:
                self.mw.declared_mb = estimate[2]
            g, gwhy = self.mw.gate_now(r, now)
            if loaded is None:
                g, gwhy = "closed", f"the judge engine was not read ({why})"
            self.mw.gate_state, self.mw.gate_why = g, gwhy
        return g == "open", gwhy

    def run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.mw.gate_state, self.mw.gate_why = "closed", f"memory reading failed ({type(e).__name__}: {e})"
                self._publish()
            self._stop.wait(self.conf["period_s"])

    def start(self):
        global _LOCAL
        _LOCAL = self
        self._thread = threading.Thread(target=self.run, name="memwatch", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Ends the thread at its next wake-up; nothing is published from now on."""
        global _LOCAL
        self._stop.set()
        if _LOCAL is self:
            _LOCAL = None


def _safe_options(env=None):
    try:
        return engine_options(env)
    except SettingsError:
        return {}


# -- The juror's side -------------------------------------------------------------------------------------------
def daemon_decision(path=None, now=None, period_s=None):
    """The daemon's `judge_memory`, when its heartbeat holds one fresh enough to be its current decision.
    -> (document, "") or (None, why)."""
    now = time.time() if now is None else now
    if period_s is None:
        try:
            period_s = settings()["period_s"]
        except SettingsError:
            period_s = PERIOD_S_DEFAULT
    doc, why = hb.read(path or hb.status_path())
    if doc is None:
        return None, why
    jm = doc.get("judge_memory")
    if not isinstance(jm, dict):
        return None, "the heartbeat carries no judge_memory"
    at = jm.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return None, "judge_memory has not been read yet"
    if now - at > STALE_PERIODS * period_s:
        return None, f"judge_memory is {int(now - at)} s old"
    if jm.get("mode") not in MODES:
        return None, f"judge_memory names no mode ({jm.get('mode')!r})"
    return jm, ""


def current_mode(now=None):
    """(mode, why): this process's Watcher, else the daemon's fresh decision, else on demand (the caller then
    measures for itself before any load)."""
    if _LOCAL is not None:
        return _LOCAL.mode(), "this process's reading"
    jm, why = daemon_decision(now=now)
    if jm is None:
        return "on-demand", f"the daemon's decision is not read ({why})"
    return jm["mode"], "the daemon's decision"


def keepalive_for_call(env=None, now=None) -> str:
    """The keep_alive a call to the judge engine sends: the resident one in resident mode, the on-demand one
    otherwise. Raises SettingsError on a duration written wrong."""
    conf = settings(env)
    mode, _why = current_mode(now)
    return conf["resident_keepalive"] if mode == "resident" else conf["ondemand_keepalive"]


class Held(RuntimeError):
    """A call to the judge engine held back: the model is not loaded, and the check of the room for it is closed
    (a memory pause, too little room, settings written wrong, or an engine whose loaded models were not read).
    Sending it would make the engine load the model with no check of the room for it. `stage` is the juror's
    abstention stage for that cause."""

    def __init__(self, why, stage=STAGE):
        super().__init__(why)
        self.stage = stage


def check_room(endpoint, model, now=None, root=None, extra_pause=None, env=None):
    """The check before a load of the judge model by a process that is not the daemon, on fresh readings of this
    machine. The caller has read that the model is not loaded. -> (stage, why, context): stage "" when the load may
    go, else the abstention stage; context {"readings", "mode", "conf", "source"} for the caller that loads.

    The footprint is the daemon's when its decision is fresh and is not a lower bound, else this process's own
    estimate (`footprint_estimate`), unless that estimate is below the daemon's lower bound (a load ran out of memory
    on it); the pause is the later of the daemon's and `extra_pause`."""
    now = time.time() if now is None else now
    try:
        conf = settings(env)
    except SettingsError as e:
        return STAGE_SETTINGS, f"the memory settings are written wrong: {e}", {}
    jm, jwhy = daemon_decision(now=now, period_s=conf["period_s"])
    fp, src, pause, mode = None, None, None, "on-demand"
    said = f"this process's own readings ({jwhy})"
    if jm is not None:
        said = "the daemon's decision and this process's readings"
        mode = jm["mode"]
        if _whole(jm.get("footprint_mb")) is not None and jm.get("footprint_source") in FOOTPRINT_SOURCES:
            fp, src = jm["footprint_mb"], jm["footprint_source"]
        p = jm.get("pause_until")
        if type(p) is int:
            pause = p
    if fp is None or src in LOWER_BOUND_SOURCES:
        efp, esrc = footprint_estimate(endpoint, model, env)
        if efp is not None and (fp is None or (esrc not in LOWER_BOUND_SOURCES and efp >= fp)):
            fp, src = efp, esrc
    paused = max([p for p in (pause, extra_pause) if p is not None], default=None)
    r = readings(root if root is not None else proc_root(env), now)
    # The model is not loaded (the caller read it): the file cache now is the cache without it.
    state, gwhy = gate(r, fp, conf["headroom_mb"], conf["mem_limit_mb"], r.get("active_file_mb"), paused, now,
                       source=src)
    ctx = {"readings": r, "mode": mode, "conf": conf, "source": src}
    if state != "open":
        return STAGE, f"{gwhy} ({said}; footprint source {src or 'none'})", ctx
    return "", gwhy, ctx


def hold_check(endpoint, model, now=None, root=None) -> tuple:
    """(stage, why) when a call to the judge engine must not go now, ("", "") when it may. A call to an engine that
    holds the judge model loaded goes; any other call would load it, so it passes the check before a load first:
    the daemon's own check in the daemon's process (`Watcher.load_gate`), `check_room` in any other (the juror in the
    middle of an audit, the self-test, the application's machine test)."""
    loaded, _size, why = model_loaded(endpoint, model)
    if loaded:
        return "", ""
    if loaded is None:
        return STAGE_ENGINE_UNREAD, (f"the judge engine's loaded models were not read ({why}); a call could load the "
                                     f"judge model with no check of the room for it")
    if _LOCAL is not None:
        with _LOCAL.lock:
            err = _LOCAL.mw.settings_error
        if err:
            return STAGE_SETTINGS, f"the memory settings are written wrong: {err}"
        ok, gwhy = _LOCAL.load_gate()
        return ("", "") if ok else (STAGE, f"the judge model is not loaded, and {gwhy}")
    stage, gwhy, _ctx = check_room(endpoint, model, now=now, root=root)
    return ("", "") if not stage else (stage, f"the judge model is not loaded, and {gwhy}")


def hold_call(endpoint, model, now=None, root=None):
    """Raises Held when a call to the judge engine must not go now (`hold_check`)."""
    stage, why = hold_check(endpoint, model, now=now, root=root)
    if stage:
        raise Held(why, stage)


def hold_reason(endpoint, model, now=None) -> str:
    """Why a call to the judge engine must not go now, or "" (`hold_check`, its reason alone)."""
    return hold_check(endpoint, model, now=now)[1]
