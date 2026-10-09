"""The miner daemon's HEARTBEAT: one small JSON file the daemon rewrites as its loop moves.

WHAT IT IS FOR, AND WHAT IT IS NOT. The daemon's loop used to print its exceptions and nothing else:
nothing outside the log said that the loop was still turning, when the relay last answered, what the
last availability proof came to, or what happened to the last job. This file says it, for two readers
only -- the container's healthcheck (is the loop moving?) and `miner_selftest.py` (what did the daemon
last see?). It is never a verdict about the chain: a proof the daemon calls proven here is a proof whose
transaction `wait_tx` saw included, and the self-test still reads the chain itself before it says
"present".

A THIRD READER, FOR TWO FIELDS: the HiveOS stats (deploy/hiveos/dendra/h-stats.sh) read `commits_anchored`
and `commits_refused`, two counts of create-commit transactions SINCE THE DAEMON'S PROCESS STARTED (defined
next to the code that counts them, miner._COMMITS). Both are in every heartbeat from the first one;
a heartbeat without them was written by an older daemon, and a reader shows nothing rather than a zero.

IT LIVES IN THE CONTAINER'S /tmp ON PURPOSE. It describes this process and dies with it: a heartbeat
that survived a restart would describe a loop that no longer exists.

THE WRITE IS ATOMIC (a temporary file in the same directory, then a rename) because the healthcheck
reads it at any moment: a half-written file would read as a malformed heartbeat, which is a third
state, and the reader would have to guess which one it is.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from modea import inference

STATUS_FILE_DEFAULT = "/tmp/dendra-miner-status.json"

# THE BOUND THE DAEMON PUTS ON ONE `dendrad` CALL. It is defined here, next to the staleness rule that
# depends on it, and applied by `miner.run`: one constant, two readers, so the bound the rule
# assumes is the bound the daemon applies.
DENDRAD_CALL_BOUND_S = 600


def status_path() -> str:
    return os.environ.get("DENDRA_STATUS_FILE") or STATUS_FILE_DEFAULT


def max_age_s() -> int:
    """How old the heartbeat may be before the loop is called STUCK.

    DERIVED, NOT CHOSEN. The daemon rewrites the file between steps; the longest single step it takes is
    one generation (bounded by OLLAMA_TIMEOUT, `inference.ollama_timeout_s`) or one `dendrad` call
    (bounded by DENDRAD_CALL_BOUND_S). Two of the longest step is the gap a moving loop can leave between
    two writes when a slow step follows another; anything older is a loop that is not moving."""
    return 2 * max(DENDRAD_CALL_BOUND_S, inference.ollama_timeout_s())


def write_atomic(path: str, doc: dict) -> None:
    """Writes `doc` as JSON at `path`, atomically. RAISES on failure: the caller decides whether a failed
    write matters (the daemon's never does -- see miner._status_write)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".dendra-status.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(path: str | None = None, what: str = "heartbeat") -> tuple:
    """(document, None) when the file was read; (None, why) otherwise. An absent file and a malformed one are
    both "not read", and the reason says which: neither is a heartbeat that says the loop moves. `what` names
    the file in that reason (the judge state below is read the same way)."""
    p = path or status_path()
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, f"no {what} at {p}"
    except (OSError, ValueError) as e:
        return None, f"{what} unreadable at {p} ({type(e).__name__})"
    if not isinstance(doc, dict):
        return None, f"{what} at {p} is not a JSON object"
    return doc, None


# ── THE JUDGE WORKER'S STATE: the model it judges with, as IT resolved it ──────────────────────────────────
# judge_worker.py resolves its judge model ONCE, at its start (judge_worker.py::resolve_judge_model: an explicit
# --model-id, then the chain's pin, then the kit's DENDRA_JUDGE_MODEL_ID, then the default), and every verdict
# it posts uses that model. A reader that resolves the model again later reads the chain OF THAT MOMENT, not
# the process: a worker started while the chain did not answer judges with its fallback even after the chain
# answers again. So the worker writes what it resolved here, and `miner_selftest.py` (the judge role a capacity
# report declares) reads it. Same rules as the heartbeat: the container's /tmp, an atomic write, a reader that
# says why it could not read. And the file is BOUND TO ITS PROCESS -- its pid and that pid's start time
# (/proc/<pid>/stat) -- because a file outlives the process that wrote it, and a pid is reused.
JUDGE_STATE_FILE_DEFAULT = "/tmp/dendra-judge-state.json"
# The four sources judge_worker.py::resolve_judge_model returns, in its order of precedence.
JUDGE_MODEL_SOURCES = ("cli", "chain", "env", "default")


def judge_state_path() -> str:
    return os.environ.get("DENDRA_JUDGE_STATE_FILE") or JUDGE_STATE_FILE_DEFAULT


def proc_starttime(stat_text):
    """The start time (field 22, clock ticks since boot) of a /proc/<pid>/stat line, or None when the line is
    not one. Field 2, the command name, is in parentheses and may itself hold spaces and parentheses: the
    fields are counted after the LAST closing parenthesis, where field 3 is the first."""
    if not isinstance(stat_text, str):
        return None
    i = stat_text.rfind(")")
    if i < 0:
        return None
    fields = stat_text[i + 1:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return int(fields[19])


def write_judge_state(model: str, source: str, path: str | None = None, counters: dict | None = None) -> None:
    """judge_worker.py, once its model is resolved: the model, its source, and the process they describe.
    RAISES on failure (the worker says so and goes on: the self-test then declares the role unknown, never
    active). A start time that cannot be read is written as null, which the reader does not recognise.
    `counters`, when given, is what the worker has done since it started (judge_worker.py::JudgeLedger)."""
    try:
        with open("/proc/self/stat", encoding="utf-8", errors="replace") as f:
            start = proc_starttime(f.read())
    except OSError:
        start = None
    doc = {"schema": 1, "pid": os.getpid(), "starttime": start,
           "model": model, "source": source, "written_at": int(time.time())}
    if counters is not None:
        doc["counters"] = counters
    write_atomic(path or judge_state_path(), doc)


# ── WHAT THE JUDGE HAS DONE: its counters, on the state it wrote ───────────────────────────────────────────
# A judge whose process runs and whose state names a model looks the same whether it votes or never can. The
# worker therefore keeps counts (seats seen, verdicts anchored, abstentions by stage, verdicts the chain
# refused) and writes them HERE, next to the model it judges with. They ride on the state the worker wrote
# at its start and never create one: counts without the model and the process they belong to would be
# figures about nobody, and a reader could not tell them from a worker that never started.
def update_judge_counters(counters: dict, path: str | None = None) -> bool:
    """Rewrites the judge state with `counters` -- only when the state exists AND names this process.
    -> True when written, False when there was no state of this process to write on. RAISES on a failed
    write, like `write_judge_state`."""
    p = path or judge_state_path()
    doc, _why = read(p, what="judge state")
    if doc is None or doc.get("pid") != os.getpid():
        return False
    doc["counters"] = counters
    doc["written_at"] = int(time.time())
    write_atomic(p, doc)
    return True


def age_s(doc: dict, now: float | None = None):
    """Seconds since the daemon last wrote, or None when `written_at` is absent or not an integer. The
    file is written by this kit, not by proto3: an absent timestamp is a malformed heartbeat, never 0."""
    w = doc.get("written_at")
    if isinstance(w, bool) or not isinstance(w, int):
        return None
    return int((time.time() if now is None else now) - w)
