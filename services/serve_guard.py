#!/usr/bin/env python3
"""serve_guard.py -- NO MINING MODEL ON THE CPU, measured where the model runs.

The testnet's rule is decided by deploy/hw_probe.sh --role and applied by the installers (deploy/join.sh,
deploy/install.sh, docker/cloud-start.sh) before anything is written. Every other way of starting this
container -- the manual compose path, the Start button of the desktop application, the remedies of
miner_health.sh, the HiveOS resume -- goes through docker/entrypoint-services.sh, which runs this file before
miner.py. So the rule is applied there too, to the engine itself: this file loads the served model (an
empty prompt loads a model without generating anything) and reads /api/ps, where `size_vram` is the part of
the loaded model that a GPU holds.

Three answers, never two (the exit code):
  0  the served model is on a GPU (size_vram > 0), or this is the JUDGE ROLE ON THE CPU: the judge role is on
     (DENDRA_MINER_JUDGE=1) and the served model IS the judge model (OLLAMA_MODEL == DENDRA_JUDGE_MODEL_ID),
     the one model the CPU serves on the testnet;
  2  refused: the served model runs on the CPU (size_vram 0) outside the judge role;
  3  not measured: the engine did not load the model, or /api/ps could not be read, within the bound
     (DENDRA_SERVE_GUARD_WAIT_S). An unread placement is never a pass: the miner does not start on it.
A model held partly by the GPU (0 < size_vram < size) passes, and the share is printed.

`size_vram` is read with the zero value of its type when the entry omits it (a Go JSON encoder may omit a zero),
and that zero is the CPU. `size` is what makes the share readable: an entry without a positive size is
not a reading (3).

Usage: python3 serve_guard.py            (environment: BACKEND, OLLAMA_ENDPOINT, OLLAMA_MODEL,
                                           DENDRA_MINER_JUDGE, DENDRA_JUDGE_MODEL_ID, DENDRA_SERVE_GUARD_WAIT_S)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

OK, REFUSED, UNMEASURED = 0, 2, 3
TAG = "[serve-guard]"


def say(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


def norm(model: str) -> str:
    """The name Ollama reports for a model: a tag without `:<version>` is `:latest`."""
    m = (model or "").strip()
    if not m:
        return ""
    last = m.rsplit("/", 1)[-1]
    return m if ":" in last else m + ":latest"


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def placement(endpoint: str, model: str, wait_s: float, step_s: float = 5.0):
    """(size, size_vram) of `model` once loaded, or (None, why) when it could not be read within wait_s."""
    base = endpoint.rstrip("/")
    want = norm(model)
    deadline = time.monotonic() + wait_s
    why = "nothing was asked yet"
    while True:
        left = deadline - time.monotonic()
        try:
            _post(base + "/api/generate", {"model": model, "prompt": "", "stream": False}, timeout=max(5.0, left))
            ps = _get(base + "/api/ps", timeout=10)
            models = ps.get("models")
            if not isinstance(models, list):
                why = "/api/ps answered without a list of models"
            else:
                for e in models:
                    if not isinstance(e, dict):
                        continue
                    if want in (norm(str(e.get("name", ""))), norm(str(e.get("model", "")))):
                        size = e.get("size", 0)
                        vram = e.get("size_vram", 0)
                        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                            return None, f"/api/ps lists {want} without a positive size"
                        if not isinstance(vram, int) or isinstance(vram, bool) or vram < 0:
                            return None, f"/api/ps lists {want} with a size_vram that is not a count of bytes"
                        return size, vram
                why = f"{want} is not among the models /api/ps lists as loaded"
        except urllib.error.HTTPError as e:
            why = f"the engine answered HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 -- any failure to read is a non-reading, said with its type
            why = f"the engine could not be read ({type(e).__name__})"
        if time.monotonic() + step_s > deadline:
            return None, why
        time.sleep(step_s)


def decide(env: dict, measure=placement) -> int:
    backend = env.get("BACKEND", "ollama") or "ollama"
    if backend != "ollama":
        # miner.pick_backend refuses every other backend in production (the mock needs DENDRA_ALLOW_MOCK).
        say(f"backend {backend!r}: not an inference engine this guard reads; miner.pick_backend decides it.")
        return OK
    endpoint = env.get("OLLAMA_ENDPOINT", "http://localhost:11434")
    model = env.get("OLLAMA_MODEL", "")
    if not model:
        say("NOT MEASURED: OLLAMA_MODEL is empty, so no served model can be placed. The miner does not start.")
        return UNMEASURED
    try:
        wait_s = float(env.get("DENDRA_SERVE_GUARD_WAIT_S", "900"))
    except ValueError:
        wait_s = 900.0
    judge_on = env.get("DENDRA_MINER_JUDGE", "0") == "1"
    judge_model = env.get("DENDRA_JUDGE_MODEL_ID", "")
    cpu_judge = judge_on and bool(judge_model) and norm(model) == norm(judge_model)
    size, vram = measure(endpoint, model, wait_s)
    if size is None:
        say(f"NOT MEASURED: where {model} runs on {endpoint} could not be read within {wait_s:.0f} s ({vram}).")
        say("An unread placement is not a pass: the miner does not start. The container restarts and asks again.")
        return UNMEASURED
    if vram > 0:
        share = 100 * vram // size
        say(f"{model} on {endpoint}: {share} % of it held by a GPU (size_vram {vram} of {size} bytes).")
        if vram < size:
            say("Part of the model runs on the CPU: inference is slower than on a card that holds it whole.")
        return OK
    if cpu_judge:
        say(f"{model} on {endpoint} runs on the CPU: the JUDGE ROLE ON THE CPU (DENDRA_MINER_JUDGE=1, the served"
            " model is the judge model), the one model the testnet serves on a CPU.")
        return OK
    say(f"SERVE REFUSED: NO MINING MODEL ON THE CPU. {model} on {endpoint} runs entirely on the CPU"
        " (size_vram 0), and this is not the judge role on the CPU.")
    say("Either the NVIDIA card is not given to the engine (deploy/join.sh writes the GPU override when Docker"
        " reaches the card: the NVIDIA container toolkit), or this machine has no usable card: then it joins as"
        " a judge, from MOE_CPU_MIN_RAM_MB of RAM (deploy/hw_probe.sh --role decides). Re-run deploy/join.sh.")
    return REFUSED


if __name__ == "__main__":
    sys.exit(decide(dict(os.environ)))
