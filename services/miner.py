#!/usr/bin/env python3
"""Persistent local miner (the real Dendra miner client).

At startup it creates its encryption key (X25519, local) and publishes the public key to the relay, then
creates its chain key, funds itself at the faucet and registers on chain with its own signature. In owner
mode (DENDRA_MINER_OWNER, deploy/join.sh --owner) it prepares the registration its owner signs elsewhere
and waits for it (see `OWNER` below).
Then it loops: it fetches the jobs assigned to it from the relay, decrypts them in locked memory, runs the
inference on Ollama (GPU), returns the sealed response and anchors its content_commit on chain.
Content stays encrypted; only metadata and hashes go on chain. It earns tokens on each honest verdict
(paid out of the client's escrow).

Usage: python3 miner.py --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea import keyring as kring
from modea import relay_signature
from modea import dendrad_argv as da
from modea import chain_id as _cid
from modea import heartbeat as hb
from modea import kit_version as kitv
from modea import owner_tx
from modea.crypto import Sealed
from modea.miner import Miner, answer_embedding
import relay_client as relay
# The faucet PoW contract (digest, verifier, solver) is defined once, in the service that enforces it.
# The solver is imported rather than reimplemented: a copy that drifted on a separator or an encoding
# would make the faucet unreachable, and neither side would report it.
import faucet as faucet_pow

# The chain id is read (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
NODE = os.environ.get("DENDRA_NODE", "")  # e.g. "tcp://chain:26657" in a container; "" = local node
# Model registry: if set, the miner declares this model_id in its commits (--model-id flag).
# Required when enforce_model_registry=ON; empty (the default) sends no flag, as before.
MODEL_ID = os.environ.get("DENDRA_MODEL_ID", "")

# Persistent keyring. Empty keeps the keyring under the process HOME. In a container that HOME is not a
# volume, so `docker compose up --build` would recreate the container without the Cosmos key and the
# miner would restart on a new address, with no funds, no registration and no stake. Pointing
# DENDRA_KEYRING_DIR into the keys volume keeps the on-chain signing key with the other keys.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")

# Time bound on the faucet PoW. 20 bits is about a million digests; the bound makes a network configured
# too hard fail with a named error instead of looping forever at startup.
POW_MAX_S = float(os.environ.get("DENDRA_FAUCET_POW_MAX_S", "300"))


# The keyring is resolved once, from the state of the disk (modea/keyring.py): `test` or `file`, and in
# `file` mode the passphrase, which reaches dendrad on stdin. Resolving raises a named KeyringError (two
# keyrings, passphrase missing or unreadable): main() says why and stops -- a keyring this process cannot
# open is never replaced by a new one.
_KR = None


def _keyring():
    global _KR
    if _KR is None:
        _KR = kring.resolve(KEYRING_DIR or None)
    return _KR


def _kr():
    """Keyring flags common to every command that signs or reads a key."""
    return _keyring().flags()


def _krun(c, t=hb.DENDRAD_CALL_BOUND_S):
    """`run` for a command that opens the keyring: the passphrase goes on stdin, never in argv."""
    return run(c, t, stdin=_keyring().stdin())
_RE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # safe relay key (no dendrad injection): a dash is allowed ("miner-A") but never first, so a key cannot be read as a dendrad flag.


def _node():
    return ["--node", NODE] if NODE else []


def ollama_ref(name) -> str:
    """An Ollama model reference with its tag. Ollama reads a name without a tag as `<name>:latest`, and lists
    it so in /api/tags: compared as written, `mistral-nemo` never matches `mistral-nemo:latest`, and a miner
    serving an untagged name anchors its commits with an empty weights_hash. The tag is what follows the last ':'
    after the last '/', since a registry host in front of the name may carry a port of its own."""
    n = str(name or "").strip()
    if not n:
        return ""
    return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"


def model_weights_hash():
    """Digest of the served model's weights (Ollama manifest), which binds the commit to the artifact.
    Best effort: "" when Ollama is unreachable or the model absent (the on-chain check only applies with
    enforce_model_registry=ON and a non-empty weights anchor in the registry). The served model is found in
    /api/tags by its reference with its tag (ollama_ref); the commit keeps naming MODEL_ID as written."""
    if not MODEL_ID:
        return ""
    want = ollama_ref(MODEL_ID)
    ep = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(ep + "/api/tags", timeout=10) as r:
            data = json.loads(r.read())
        for m in data.get("models", []):
            if ollama_ref(m.get("name")) == want or ollama_ref(m.get("model")) == want:
                dg = m.get("digest", "")
                return dg.split(":")[-1] if dg else ""
    except Exception:
        return ""
    return ""


def run(c, t=hb.DENDRAD_CALL_BOUND_S, stdin=""):
    # stdin is always given (empty when nothing is to be read): an inherited stdin would leave a prompt
    # nobody answers waiting until the call's time bound.
    r = subprocess.run(c, capture_output=True, text=True, timeout=t, input=stdin)
    return (r.stdout or "") + (r.stderr or "")


# -- The heartbeat (modea/heartbeat.py) ------------------------------------------------------------
# One JSON file in the container's /tmp, rewritten as the loop moves: when the loop last went round, its
# last error, the last time the relay answered the work queue, the outcome of the last availability proof
# (the height `wait_tx` read, or the chain's refusal), and what happened to the last job. It has two
# readers, the container's healthcheck and miner_selftest.py, and is a diagnosis, never a verdict on the
# chain. A failed write never stops the loop: a full /tmp or a read-only filesystem must cost a missing
# file, not a miner that stops serving. The failure is logged once, so that it does not bury the log.
_STATUS: dict = {}
_STATUS_WARNED = False

# >>> commit counters -- read by the HiveOS stats as `ar` (deploy/hiveos/dendra/h-stats.sh).
# Two counts of create-commit transactions, since this process started: a restart sets both back to zero,
# like the heartbeat file itself, which describes this process and dies with it.
#   commits_anchored  transactions this process broadcast whose commitment it then read on the chain
#                     (`get-commit`): at most one per job, since a job is done once its commitment is read;
#   commits_refused   transactions this process broadcast that the chain answered with a non-zero code,
#                     at broadcast or once included (`tx_fate`). A job refused three times counts three.
# A transaction whose fate was not read (no transaction response, including a refusal while the node
# estimates its gas, which is never broadcast; not found in time; a node that did not answer) counts in
# neither: unknown is not refused. A commitment found on the chain that this process did not broadcast (an
# earlier run anchored it) is not counted either: these are this process's counts.
# Both exist from the first heartbeat on (zero is then a reading: nothing counted yet), are copied into every
# write by _status_write, and are incremented by _commit_count, which never raises.
_COMMITS = {"commits_anchored": 0, "commits_refused": 0}
_COMMITS_SENT: set = set()   # relay keys whose create-commit this process broadcast and not yet counted anchored


def _commit_count(name: str) -> None:
    try:
        _COMMITS[name] += 1
    except Exception:  # noqa: BLE001 -- counting is diagnosis, like the heartbeat: it never takes the loop down
        pass


def _commit_anchored(key: str) -> None:
    """Counts the job `key` anchored once, and only when this process broadcast its create-commit."""
    if key in _COMMITS_SENT:
        _COMMITS_SENT.discard(key)
        _commit_count("commits_anchored")
# <<< commit counters

# The request counters, since this process started, like the commit counters above (a restart sets them back to
# zero), in every heartbeat from the first one (zero is then a reading):
#   inference_failed         inferences that raised (each attempt counts, see JOB_FAILURES);
#   deposit_refused          sealed responses the relay refused, at the first deposit or at a replay of it;
#   abandoned                requests given up after job_max_attempts() failed inferences, each followed by an
#                            engine that answered a test request (a failure while the engine is silent is the
#                            engine's, and gives nothing up);
#   requests_stale           new requests not served because older than request_max_age_s() by this machine's
#                            clock and, once read, the chain's (request_stale_age), once per request;
#   answers_on_time          answers generated within the wait their client declared in its request (wait_s);
#   answers_late             answers whose generation alone took longer than that declared wait;
#   answers_wait_undeclared  answers whose request declared no wait: no verdict, never a default one.
_JOB_COUNTS = {"inference_failed": 0, "deposit_refused": 0, "abandoned": 0, "requests_stale": 0,
               "answers_on_time": 0, "answers_late": 0, "answers_wait_undeclared": 0}


def _job_count(name: str) -> None:
    try:
        _JOB_COUNTS[name] += 1
    except Exception:  # noqa: BLE001 -- diagnosis never takes the loop down
        pass


def _status_note(**fields) -> None:
    """Records fields for the next write without writing: the loop writes once per pass, not once per
    fact, so a pass that only read the queue costs one small file and not three. Values go through JSON
    on the way in (anything else becomes its text): a value that cannot be written would otherwise stay in
    the record and fail every later write."""
    _STATUS.update(json.loads(json.dumps(fields, default=str)))


def _status_write(**fields) -> bool:
    global _STATUS_WARNED
    try:
        _status_note(**fields)
        _STATUS.update(_COMMITS)
        _STATUS.update(_JOB_COUNTS)
        _STATUS["schema"] = 1
        _STATUS["written_at"] = int(time.time())
        hb.write_atomic(hb.status_path(), _STATUS)
        return True
    except Exception as e:  # noqa: BLE001 -- the heartbeat must never take the loop down
        if not _STATUS_WARNED:
            _STATUS_WARNED = True
            print(f"[daemon] heartbeat not written to {hb.status_path()} ({type(e).__name__}: {e}). Mining "
                  f"continues; the container's healthcheck and miner_selftest.py will report the loop as "
                  f"not measured until the file can be written. Said once.", flush=True)
        return False


# -- The bootstrap deposits, retried ---------------------------------------------
# A new miner deposits its encryption key (`pub`) and its attestation (`attest`) at the relay once it is
# registered. A relay whose copy of the registry predates the registration refuses both as UNKNOWN_MINER,
# and until this record the daemon marked them done whatever the relay answered, so they were missing until
# the next restart. Each kind is now done only when the relay accepted it or already held it; a refusal is
# tried again after a delay that doubles from BOOTSTRAP_RETRY_S up to BOOTSTRAP_RETRY_MAX_S, for as long as
# the relay refuses. Two outcomes end a kind without an acceptance, as before this record: `skipped`, the
# key differs from the one anchored on chain (ENC_MISMATCH, a retry changes nothing), and `unavailable`, an
# attestation this build cannot produce. The state is in the heartbeat (`bootstrap_deposits`), so a miner
# whose deposits the relay keeps refusing shows it there and not only in a rotated log.
BOOTSTRAP_KINDS = ("pub", "attest")
BOOTSTRAP_RETRY_S = 60.0
BOOTSTRAP_RETRY_MAX_S = 3600.0
BOOTSTRAP_DONE = ("ok", "exists", "skipped", "unavailable")


class BootstrapDeposits:
    """What each bootstrap deposit came to, and when the refused ones are due again."""

    def __init__(self, retry_s=None, retry_max_s=None):
        self.retry_s = float(BOOTSTRAP_RETRY_S if retry_s is None else retry_s)
        self.retry_max_s = float(BOOTSTRAP_RETRY_MAX_S if retry_max_s is None else retry_max_s)
        self.outcome = {k: None for k in BOOTSTRAP_KINDS}
        self.delay = 0.0
        self.next_at = 0.0
        self.rounds = 0

    def pending(self) -> list:
        """The kinds still to deposit: never tried, or refused at the last round."""
        return [k for k in BOOTSTRAP_KINDS if self.outcome[k] not in BOOTSTRAP_DONE]

    def complete(self) -> bool:
        return not self.pending()

    def due(self, now: float) -> bool:
        return not self.complete() and now >= self.next_at

    def record(self, kind: str, outcome: str) -> None:
        if kind not in self.outcome:
            raise ValueError(f"not a bootstrap deposit: {kind!r}")
        self.outcome[kind] = outcome

    def close_round(self, now: float) -> None:
        """After a round: nothing more to do, or the next attempt is due after a delay that doubles."""
        self.rounds += 1
        if self.complete():
            self.next_at = 0.0
            return
        self.delay = self.retry_s if self.delay <= 0 else min(self.delay * 2, self.retry_max_s)
        self.next_at = now + self.delay

    def to_status(self) -> dict:
        due = None if (self.complete() or self.rounds == 0) else int(self.next_at)
        return {"pub": self.outcome["pub"], "attest": self.outcome["attest"], "complete": self.complete(),
                "rounds": self.rounds, "next_at": due}


# -- The kit version, against what the network publishes ------------------------
# Once an hour the daemon reads `network-info.txt` at DENDRA_CONFIG_URL (the URL join.sh set the kit up
# from, passed by the compose file), a key=value text that is parsed and never executed, with the character
# set of deploy/join.sh::_load_config_url. Three keys are read: KIT_VERSION, the kit the latest release
# carries; KIT_MIN and KIT_DEADLINE, the floor and the instant from which the network's services refuse a
# kit below it (deploy/testnet/publish_network.sh writes all three). Behind KIT_VERSION, the daemon prints
# the banner `Update available` with the update instruction join.sh recorded (DENDRA_KIT_UPDATE) and says
# the deadline. The heartbeat carries `kit_behind` (true, false, or null until a read compared the two
# numbers) and `kit_latest` (the published number as text, null until read), which the application and
# miner_health.sh read, and this kit's own `kit_version`, read from the file the image ships
# (modea.kit_version). Nothing here updates anything: the update is the host's timer, never the container.
KIT_CHECK_S = 3600.0
NETWORK_INFO_KIT_KEYS = ("KIT_VERSION", "KIT_MIN", "KIT_DEADLINE")
# The schemes network-info is read under: TLS and nothing else. A bench that serves the file from a loopback
# socket widens this in its own process (tests/test_registration_retry.py); the kit never does.
KIT_INFO_URL_PREFIXES = ("https://",)
_NETWORK_INFO_VALUE = re.compile(r"^[A-Za-z0-9:/._@,-]+$")


def parse_network_info(text: str) -> dict:
    """The kit keys of a network-info text, as strings. A line that is not `KEY=VALUE`, a key outside the
    three, or a value outside the character set is dropped; nothing is interpreted."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in NETWORK_INFO_KIT_KEYS and v and _NETWORK_INFO_VALUE.match(v):
            out[k] = v
    return out


def fetch_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(65536).decode("utf-8", "replace")


def kit_status(mine, url, fetch=fetch_text, now=None) -> dict:
    """The comparison of this kit with the network's, as the heartbeat carries it.

    `kit_behind` is None until both numbers are known: no URL, an unreadable file, a file without
    KIT_VERSION, or a kit whose own number could not be read all leave it null, and `kit_why` says which.
    None is never false: a miner that could not read the network is not a miner that is current."""
    st = {"kit_version": mine, "kit_latest": None, "kit_behind": None, "kit_min": None, "kit_deadline": None,
          "kit_read_at": None, "kit_why": ""}
    if not url:
        st["kit_why"] = "DENDRA_CONFIG_URL is unset: the kit the network publishes is not read"
        return st
    # HTTPS only: over plain HTTP whoever sits on the path writes KIT_VERSION, hence the
    # banner and kit_behind. A file that was not read is reported in kit_why, never compared.
    if not url.lower().startswith(KIT_INFO_URL_PREFIXES):
        st["kit_why"] = f"DENDRA_CONFIG_URL is not https:// ({url}): the published kit is read over TLS only, so nothing is compared"
        return st
    try:
        text = fetch(url)
    except Exception as e:  # noqa: BLE001 -- an unread file is reported in kit_why, never read as current
        st["kit_why"] = f"network-info unreadable at {url} ({type(e).__name__})"
        return st
    info = parse_network_info(text)
    st["kit_read_at"] = int(time.time() if now is None else now)
    st["kit_min"] = info.get("KIT_MIN")
    st["kit_deadline"] = info.get("KIT_DEADLINE")
    latest = info.get("KIT_VERSION")
    if latest is None or not latest.isdigit():
        st["kit_why"] = "the network-info names no KIT_VERSION: whether this kit is current is not checked"
        return st
    st["kit_latest"] = latest
    if mine is None:
        st["kit_why"] = "this kit's KIT_VERSION file is unreadable: nothing to compare with the network's"
        return st
    st["kit_behind"] = int(mine) < int(latest)
    return st


def kit_banner(st: dict, hint: str = "") -> str:
    """The lines printed while the kit is behind; "" otherwise."""
    if st.get("kit_behind") is not True:
        return ""
    lines = [f"[daemon] Update available: kit {st['kit_latest']} is published, this machine runs kit "
             f"{st['kit_version']}.",
             f"[daemon] Update: {hint or 'see deploy/join.sh (the kit-update instruction it prints)'}"]
    kmin, deadline = st.get("kit_min"), st.get("kit_deadline")
    if kmin and deadline:
        lines.append(f"[daemon] Update before {deadline}: from then the network refuses kits below {kmin} "
                     f"(the relay refuses their deposits, the faucet their grants, the season their pay).")
    return "\n".join(lines)


def tx_from(frm, sub, *positionals, flags=()):
    # Nonce retries: a miner that also judges shares one account across three processes (commit, reveal,
    # verdict), so two close transactions can take the same sequence and fail with "account sequence
    # mismatch". dendrad reads the sequence again on each attempt, so the retry passes once the transaction
    # in flight is included. The backoff is bounded (about 14 s in total).
    # Positionals go after `--`: the content commitment is an embedding vector whose first number is
    # negative about half the time, and placed before the terminator pflag would read it as options.
    # See modea/dendrad_argv.py.
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, *_kr(), "--chain-id", _cid.chain_id(tuple(_node())),
         "--gas", "auto", "--gas-adjustment", da.GAS_ADJUSTMENT, "--yes", *_node()])
    o = ""
    for attempt in range(6):
        o = _krun(cmd)
        if "account sequence mismatch" not in o:
            return o
        time.sleep(1.0 + 0.8 * attempt)
    return o


def _tx_err(o):
    """Short error message from a tx output (to log a failing create-commit).

    The usage error is checked first, and the fallback reads the head of the output, not the tail. A cobra
    usage failure carries neither `raw_log` nor `code`, and the end of its output is the echoed argument
    itself (for create-commit, the embedding vector), not the reason.
    """
    usage = da.cli_usage_error(o)
    if usage:
        return usage
    m = re.search(r'raw_log:\s*"?([^\n"]+)', o) or re.search(r'(code:\s*\d+[^\n]*)', o)
    return (m.group(1) if m else (o or "")[:200]).strip()[:200]


def query(sub, *positionals, flags=()):
    return run(da.dendrad_argv(("dendrad", "query", "jobs"), sub, positionals, [*flags, *_node()]))


def _tx_code(t):
    """The code a transaction response carries, in three states, never two:
      * the number on its `code:` line when it has one;
      * 0 when the text is a transaction response (it names its `txhash:`) and carries no `code:` line --
        proto3 omits a field at its zero value, so an absent code is a zero, not a failure;
      * None when the text is no transaction response at all (a usage error, an unreachable node, a
        transaction the node refused while estimating its gas, which is never broadcast): nothing was read,
        and an unread code is neither an acceptance nor a refusal."""
    t = t or ""
    m = re.search(r'(^|\n)code: (\d+)', t)
    if m:
        return int(m.group(2))
    if re.search(r'(^|\n)txhash:\s*"?[A-Fa-f0-9]{64}', t):
        return 0
    return None


def _ok(t):
    return _tx_code(t) == 0


def tx_fate(o, timeout=24):
    """(height, code) of the transaction broadcast in `o`. The height is the one it was included at, 0 when
    it was not seen included; the code is the one read -- at broadcast when the node refused it there, else
    the one of the included transaction -- and None when no code was read (no transaction response, or not
    found within `timeout` polls). Only a non-zero code read is a refusal: an unknown is never one."""
    c = _tx_code(o)
    if c != 0:
        return 0, c
    h = re.search(r'txhash:\s*"?([A-Fa-f0-9]{64})', o)
    if not h:
        return 0, None
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return int(m.group(2)), _tx_code(q)
        time.sleep(2)
    return 0, None


def wait_tx_height(o, timeout=24) -> int:
    """The height at which the transaction broadcast in `o` was included and accepted; 0 otherwise (not
    accepted at broadcast, not found, not included in time, or included and failed). The heartbeat keeps
    this height for an availability proof: it is what makes "proven" a reading rather than a hope."""
    h, c = tx_fate(o, timeout)
    return h if c == 0 else 0


def wait_tx(o, timeout=24):
    return wait_tx_height(o, timeout) > 0


# -- Consensus epoch 15, read on the chain ------------------------------------------------------------
# One daemon runs on the epoch 14 chain and, after its governed upgrade, on the epoch 15 chain, and two of its
# transactions depend on which one answers. The epoch 14 binary refuses at decode a presence proof that carries
# kit_version (field 5: an unknown field below 1024 is critical) and knows no MsgDeclareJudge; on the epoch 15 chain a
# jury seat belongs to a declared judge only. The epoch is read on the node this daemon already
# talks to, in three answers, never two:
#   applied      the plan answers a height; or no height while the jobs module runs its epoch 15 version, which is how
#                a chain started, or restored by export, on the epoch 15 binary reads (no plan ran there);
#   not_applied  no height for the plan, and the jobs module below its epoch 15 version;
#   unknown      nothing was read: the node did not answer, or the answer is not the response's shape.
# The daemon acts on the last answer that was not unknown (EpochProbe.acting), the way drain_state keeps its state
# through an unreadable directory: one missed read does not turn an epoch 15 miner back into an epoch 14 one. Before
# any answer it sends nothing that only the epoch 15 chain decodes.
EPOCH15_PLAN = "epoch-15-jurors-are-judges"   # the plan the epoch 15 binary handles (its app/upgrades.go)
# The jobs module's consensus version from epoch 15 on; chain/x/jobs/module/module.go::ConsensusVersion answers 1
# before it, and the epoch 15 migration is the step from 1 to 2.
EPOCH15_JOBS_VERSION = 2
EPOCH_APPLIED, EPOCH_NOT_APPLIED, EPOCH_UNKNOWN = "applied", "not_applied", "unknown"
# Chosen: one reading per EPOCH_PROBE_S once an answer is known, one per EPOCH_RETRY_S while none is. The epoch 15
# migration writes its new parameters at 0, min_kit_version among them, so a proof sent without kit_version in the
# minutes after the upgrade is still accepted until governance arms that floor.
EPOCH_PROBE_S = 600.0
EPOCH_RETRY_S = 120.0


def run_rc(c, t=hb.DENDRAD_CALL_BOUND_S):
    """(exit code, stdout, stderr) of `c`, kept apart: telling an answer from a failure needs the code, which `run`
    drops. The code is None when the command could not start or did not end within `t`. Never raises."""
    try:
        r = subprocess.run(c, capture_output=True, text=True, timeout=t, input="")
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:  # noqa: BLE001 -- not started, or not finished: no code was read
        return None, "", f"{type(e).__name__}: {e}"


def _first_line(text, limit=200) -> str:
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()[:limit]
    return ""


def _json_object(text):
    """The JSON object `text` holds as a whole, or None: a reader parses the field, it never matches the text."""
    try:
        d = json.loads((text or "").strip())
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _uint(v):
    """A non-negative integer from a JSON value (a 64-bit integer prints as a string), or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and v.isascii() and v.isdigit():
        return int(v)
    return None


def _failed(what, rc, out, err) -> str:
    return f"`{what}` failed ({_first_line(err) or _first_line(out) or 'exit ' + str(rc)})"


def read_plan_applied(runner=None):
    """(state, height, why) of EPOCH15_PLAN, from `dendrad query upgrade applied`. The upgrade keeper answers height 0
    for a plan that never ran, with no error, and proto3 omits a zero: `{}` is the measured "not applied"."""
    cmd = da.dendrad_argv(("dendrad", "query", "upgrade"), "applied", (EPOCH15_PLAN,), ["--output", "json", *_node()])
    rc, out, err = (runner or run_rc)(cmd)
    if rc != 0:
        return EPOCH_UNKNOWN, None, _failed("dendrad query upgrade applied", rc, out, err)
    d = _json_object(out)
    if d is None or set(d) - {"height"}:
        return EPOCH_UNKNOWN, None, "the answer of `dendrad query upgrade applied` is not its response"
    h = _uint(d.get("height", 0))
    if h is None:
        return EPOCH_UNKNOWN, None, f"the plan's height is not an integer ({d.get('height')!r})"
    return (EPOCH_APPLIED, h, "") if h > 0 else (EPOCH_NOT_APPLIED, None, "")


def read_jobs_version(runner=None):
    """(version, why): the consensus version the chain records for its jobs module, from `dendrad query upgrade
    module-versions jobs`. None when it was not read; an omitted version reads 0, the proto3 zero."""
    cmd = da.dendrad_argv(("dendrad", "query", "upgrade"), "module-versions", ("jobs",),
                          ["--output", "json", *_node()])
    rc, out, err = (runner or run_rc)(cmd)
    if rc != 0:
        return None, _failed("dendrad query upgrade module-versions jobs", rc, out, err)
    d = _json_object(out)
    mv = d.get("module_versions") if d is not None else None
    if not isinstance(mv, list):
        return None, "the answer of `dendrad query upgrade module-versions jobs` is not its response"
    for m in mv:
        if isinstance(m, dict) and m.get("name") == "jobs":
            v = _uint(m.get("version", 0))
            if v is None:
                return None, f"the jobs module's version is not an integer ({m.get('version')!r})"
            return v, ""
    return None, "the answer of `dendrad query upgrade module-versions jobs` names no jobs module"


def read_chain_epoch(runner=None):
    """(state, why, detail): the plan first, and the jobs module's version only when the plan answers no height."""
    st, h, why = read_plan_applied(runner)
    if st == EPOCH_APPLIED:
        return EPOCH_APPLIED, f"the plan {EPOCH15_PLAN} was applied at height {h}", {"plan_height": h}
    if st == EPOCH_UNKNOWN:
        return EPOCH_UNKNOWN, why, {}
    v, vwhy = read_jobs_version(runner)
    if v is None:
        return EPOCH_UNKNOWN, f"the plan {EPOCH15_PLAN} answers no height, and {vwhy}", {}
    if v >= EPOCH15_JOBS_VERSION:
        return (EPOCH_APPLIED, f"the plan {EPOCH15_PLAN} answers no height and the jobs module runs version {v}: the "
                               f"chain was started or restored on the epoch 15 binary", {"jobs_version": v})
    return (EPOCH_NOT_APPLIED, f"the plan {EPOCH15_PLAN} answers no height and the jobs module runs version {v}",
            {"jobs_version": v})


def epoch_line(state, acting, why) -> str:
    if state == EPOCH_APPLIED:
        return (f"[daemon] consensus epoch 15: {why}. Presence proofs carry kit_version, and the judge declaration "
                f"follows this container's judge role (DENDRA_MINER_JUDGE).")
    if state == EPOCH_NOT_APPLIED:
        return (f"[daemon] consensus epoch 14: {why}. Presence proofs carry no kit_version and no judge is declared: "
                f"the epoch 14 chain decodes neither.")
    kept = {EPOCH_APPLIED: "as on epoch 15, its last reading",
            EPOCH_NOT_APPLIED: "as on epoch 14, its last reading"}.get(
        acting, "as on epoch 14 until a reading: nothing only epoch 15 decodes is sent")
    return f"[daemon] consensus epoch not read ({why}); this daemon acts {kept}. Read again in {int(EPOCH_RETRY_S)} s."


class EpochProbe:
    """The chain's epoch, read on a bounded period (EPOCH_PROBE_S, EPOCH_RETRY_S while unknown), logged when the
    reading changes and noted in the heartbeat (`chain_epoch`). `state` is the last reading; `acting` the last reading
    that was not unknown, None before the first one."""

    def __init__(self, reader=None):
        self.reader = reader
        self.state, self.acting, self.why, self.read_at = None, None, "", None

    def due(self, now) -> bool:
        if self.read_at is None:
            return True
        return now - self.read_at >= (EPOCH_RETRY_S if self.state == EPOCH_UNKNOWN else EPOCH_PROBE_S)

    def tick(self, now=None, force=False) -> bool:
        """Reads again when due (or forced). True when `acting` changed."""
        now = time.time() if now is None else now
        if not force and not self.due(now):
            return False
        self.read_at = now
        try:
            st, why, detail = (self.reader or read_chain_epoch)()
        except Exception as e:  # noqa: BLE001 -- a reader that raises is a reading that failed
            st, why, detail = EPOCH_UNKNOWN, f"the epoch reader raised {type(e).__name__}", {}
        if st not in (EPOCH_APPLIED, EPOCH_NOT_APPLIED):
            st = EPOCH_UNKNOWN
        before_state, before_acting = self.state, self.acting
        self.state, self.why = st, why
        if st != EPOCH_UNKNOWN:
            self.acting = st
        _status_note(chain_epoch={"plan": EPOCH15_PLAN, "read": st, "acting": self.acting or EPOCH_UNKNOWN,
                                  "why": why, "at": int(now), **(detail or {})})
        if st != before_state:
            print(epoch_line(st, self.acting, why), flush=True)
        return self.acting != before_acting

    def applied(self) -> bool:
        return self.acting == EPOCH_APPLIED


# -- What this kit's dendrad offers --------------------------------------------------------------------------------
# A dendrad built before the plan has neither `tx jobs declare-judge` nor `--kit-version` on prove-availability, and it
# is the binary of this container's image, so the answer holds for the life of the process. It is read from the help:
# a verb the binary lacks prints its parent's help, whose usage line names the parent, and at a real call the parent
# refuses the first flag it does not know ("unknown flag: --from"), never "unknown command". Measured on the epoch 14
# and epoch 15 binaries.
def cli_offers(rc, out, err, base, verb, flag=None):
    """True when the help of `dendrad <base> <verb>` shows the verb (and `flag` among its flags), False when it shows
    the parent's help instead (or the verb without the flag), None when no help was read."""
    if rc != 0:
        return None
    lines = [ln.strip() for ln in (out or "").splitlines() + (err or "").splitlines()]
    if "Usage:" not in lines:
        return None
    usage = next((ln for ln in lines[lines.index("Usage:") + 1:] if ln), "")
    own = " ".join(("dendrad", *base, verb))
    parent = " ".join(("dendrad", *base))
    if usage != own and not usage.startswith(own + " "):
        return False if usage.startswith(parent + " ") else None
    if flag is None:
        return True
    return any(ln.split(" ")[0] == flag for ln in lines)


class KitCli:
    """The verbs and flags of this process's dendrad, read once each (cli_offers); an unread help is asked again."""

    def __init__(self, runner=None):
        self.runner = runner
        self.known = {}
        self.said = []

    def has(self, base, verb, flag=None):
        key = (tuple(base), verb, flag)
        if key not in self.known:
            rc, out, err = (self.runner or run_rc)(["dendrad", *base, verb, "--help"])
            ans = cli_offers(rc, out, err, tuple(base), verb, flag)
            if ans is None:
                return None
            self.known[key] = ans
        return self.known[key]

    def predates(self, mid, what) -> None:
        """Says once per missing piece that the kit's dendrad predates the plan, and notes it in the heartbeat."""
        if what in self.said:
            return
        self.said.append(what)
        _status_note(kit_cli={"predates_plan": list(self.said)})
        print(f"[daemon] {mid}: the kit's dendrad predates the plan {EPOCH15_PLAN}: it has no {what}. Update the kit "
              f"(deploy/testnet-miner/kit_update.sh, or the automatic update). This process does not try it again: "
              f"its dendrad cannot change while it runs. Said once.", flush=True)


def proof_kit_flags(acting, kit, cli_flag) -> tuple:
    """`--kit-version <kit>` for a presence proof, or nothing. Only on a chain known to run epoch 15, where the field
    exists, and with a dendrad that has the flag; never on epoch 14, whose binary refuses the proof at decode, nor
    before any reading. Nothing either when this kit's number is unreadable (main says so at start): the proof then
    reads 0, which the chain accepts until governance arms min_kit_version."""
    if acting != EPOCH_APPLIED or cli_flag is not True:
        return ()
    if isinstance(kit, bool) or not isinstance(kit, int) or kit <= 0:
        return ()
    return ("--kit-version", str(kit))


# -- The judge declaration ------------------------------------------------------------------
# On the epoch 15 chain a jury seat belongs to a miner whose operator declared a judge (MsgDeclareJudge), and the
# declaration follows this container's judge role: `true` while the judge worker runs (judge_role_on) and the miner is
# not draining, `false` once the role is off (judge.sh off recreates this container without it) or the drain is set
# (exit-miner.sh --yes). Each is sent against a reading of the chain, and only when the chain holds the other state: a
# second `true` rewrites the declaration height and clears the missed-draw counter, so a daemon
# that declared at every start would launder the count that excludes a judge that never answers. Nothing is declared
# on a chain not known to run epoch 15, where the message does not exist.
#
# The reading. The epoch 15 branch adds a query of a miner's declaration (rpc MinerJudge of x/jobs), read from the
# CLI as `dendrad query jobs miner-judge <miner_id>` and answering the fields of JUDGE_READ_KEYS; a search of
# `judge_declared` events would miss the exits and evictions that clear it. A dendrad without that verb (an older
# image) reads `unsupported`, and nothing is declared either way.
JUDGE_DECLARED, JUDGE_NOT_DECLARED = "declared", "not_declared"
JUDGE_READ_UNKNOWN, JUDGE_READ_UNSUPPORTED = "unknown", "unsupported"
JUDGE_READ_VERB = "miner-judge"
JUDGE_READ_KEYS = ("declared", "declared_height", "last_verdict_height", "missed_draws")
# Chosen: at most one reading of the declaration per JUDGE_TURN_S, and at most one transaction per reading. A
# declaration whose fate was not read is never sent again before the next reading, which finds it if it landed.
JUDGE_TURN_S = 600.0


def judge_role_on(env=None) -> bool:
    """The judge role of this container: DENDRA_MINER_JUDGE=1, the one value docker/entrypoint-services.sh starts the
    judge worker on. Any other value starts none, and a declaration would seat a judge that never answers."""
    return (os.environ if env is None else env).get("DENDRA_MINER_JUDGE") == "1"


def read_judge_declaration(mid, cli=None, runner=None):
    """(state, fields, why) of the chain's judge declaration for `mid`: declared, not_declared, unknown, or
    unsupported when this dendrad has no `query jobs miner-judge`. `{}` is not_declared (proto3 omits a false and
    zeros). An answer with a field outside JUDGE_READ_KEYS is unknown: an interface this daemon was not written
    against is never read as "not declared", which would send a second `true`."""
    offered = (cli or KitCli(runner)).has(("query", "jobs"), JUDGE_READ_VERB)
    if offered is False:
        return JUDGE_READ_UNSUPPORTED, {}, f"this dendrad has no `query jobs {JUDGE_READ_VERB}`"
    if offered is None:
        return JUDGE_READ_UNKNOWN, {}, f"the help of `dendrad query jobs {JUDGE_READ_VERB}` could not be read"
    cmd = da.dendrad_argv(("dendrad", "query", "jobs"), JUDGE_READ_VERB, (mid,), ["--output", "json", *_node()])
    rc, out, err = (runner or run_rc)(cmd)
    if rc != 0:
        return JUDGE_READ_UNKNOWN, {}, _failed(f"dendrad query jobs {JUDGE_READ_VERB}", rc, out, err)
    d = _json_object(out)
    if d is None:
        return JUDGE_READ_UNKNOWN, {}, f"the answer of `dendrad query jobs {JUDGE_READ_VERB}` is not a JSON object"
    extra = sorted(set(d) - set(JUDGE_READ_KEYS))
    if extra:
        return JUDGE_READ_UNKNOWN, {}, f"the answer carries fields this daemon does not read ({', '.join(extra)})"
    declared = d.get("declared", False)
    if not isinstance(declared, bool):
        return JUDGE_READ_UNKNOWN, {}, f"`declared` is not a boolean ({declared!r})"
    fields = {"declared": declared}
    for k in JUDGE_READ_KEYS[1:]:
        fields[k] = _uint(d.get(k, 0))
        if fields[k] is None:
            return JUDGE_READ_UNKNOWN, {}, f"`{k}` is not an integer ({d.get(k)!r})"
    return (JUDGE_DECLARED if declared else JUDGE_NOT_DECLARED), fields, ""


def judge_action(acting, role_on, draining, registered, read_state):
    """What to send about the judge declaration: "true", "false" or None (nothing).
      a seat is wanted (role on, not draining): "true" when the chain holds no declaration, never when it holds one;
      no seat is wanted (role off, or draining): "false" when the chain holds a declaration, nothing otherwise.
    Nothing on a chain not known to run epoch 15, for a miner the chain does not record with this key as operator, or
    against a reading that is unknown or unsupported."""
    if acting != EPOCH_APPLIED or not registered or read_state not in (JUDGE_DECLARED, JUDGE_NOT_DECLARED):
        return None
    want = bool(role_on) and not draining
    if want and read_state == JUDGE_NOT_DECLARED:
        return "true"
    if not want and read_state == JUDGE_DECLARED:
        return "false"
    return None


class JudgeSeat:
    """The judge declaration of one miner, kept in step with its role (judge_action), noted in the heartbeat
    (`judge_declaration`). `stopped` names why this process declares nothing any more: its dendrad lacks the verb
    that sends, or the verb that reads."""

    def __init__(self, mid, cli, reader=None, send=None):
        self.mid, self.cli = mid, cli
        self.reader = reader
        self.send = send
        self.next_at = 0.0
        self.stopped = ""
        self.read_said = None

    def wake(self) -> None:
        """The next turn reads at once: the drain or the epoch changed."""
        self.next_at = 0.0

    def _note(self, **fields) -> None:
        _status_note(judge_declaration={"at": int(time.time()), **fields})

    def turn(self, acting, role_on, draining, registered, now=None):
        """One turn; returns the value sent and accepted ("true" or "false"), or None."""
        now = time.time() if now is None else now
        role = {"role": "on" if role_on else "off", "draining": bool(draining)}
        if acting != EPOCH_APPLIED:
            self._note(state="not_sent", why="the chain is not known to run epoch 15", **role)
            return None
        if not registered:
            self._note(state="not_sent", why="the chain does not record this miner with this key as operator", **role)
            return None
        if self.stopped or now < self.next_at:
            return None
        self.next_at = now + JUDGE_TURN_S
        if self.cli.has(("tx", "jobs"), "declare-judge") is False:
            self.stopped = "kit_cli_predates_plan"
            self.cli.predates(self.mid, "`tx jobs declare-judge`")
            self._note(state=self.stopped, why="the kit's dendrad predates the plan: update the kit", **role)
            return None
        state, fields, why = (self.reader or (lambda m: read_judge_declaration(m, cli=self.cli)))(self.mid)
        if state == JUDGE_READ_UNSUPPORTED:
            self.stopped = "no_read"
            self._note(state=self.stopped, why=why, **role)
            print(f"[daemon] {self.mid}: the chain runs epoch 15 and {why}, the read of a miner's judge declaration: "
                  f"no declaration is sent, either way, since a `true` sent blind could clear the missed-draw count. "
                  f"The epoch 15 chain draws an undeclared miner into no jury. Said once.",
                  flush=True)
            return None
        if state != self.read_said and state in (JUDGE_DECLARED, JUDGE_NOT_DECLARED):
            self.read_said = state
            print(f"[daemon] {self.mid}: the chain " + (
                f"holds this miner's judge declaration (declared at height {fields.get('declared_height')}, missed "
                f"draws {fields.get('missed_draws')})." if state == JUDGE_DECLARED
                else "holds no judge declaration for this miner.") + f" Judge role {role['role']}"
                  + (", draining." if draining else "."), flush=True)
        act = judge_action(acting, role_on, draining, registered, state)
        self._note(state=state, read=fields, why=why, sent=act, **role)
        if act is None:
            return None
        out = (self.send or (lambda m, v: tx_from(m, "declare-judge", m, v)))(self.mid, act)
        h = wait_tx_height(out)
        if h:
            self.read_said = JUDGE_DECLARED if act == "true" else JUDGE_NOT_DECLARED
            self._note(state=self.read_said, sent=act, height=h, **role)
            if act == "true":
                print(f"[daemon] {self.mid}: judge declared on chain at height {h} (declare-judge true): the epoch 15 "
                      f"chain may draw this miner into a jury, and its judge worker answers the seats it gets.",
                      flush=True)
            else:
                print(f"[daemon] {self.mid}: judge declaration withdrawn on chain at height {h} (declare-judge false, "
                      f"{'this miner is draining' if draining else 'the judge role is off'}): no new jury seat; a seat "
                      f"already anchored stays until its audit concludes.", flush=True)
            return act
        sent_why = _tx_err(out) or "its fate was not read"
        self._note(state="not_confirmed", sent=act, why=sent_why, **role)
        print(f"[daemon] {self.mid}: declare-judge {act} not confirmed ({sent_why}); the declaration is read again "
              f"in {int(JUDGE_TURN_S)} s before anything is sent.", flush=True)
        return None


RECOVERY_FILE = "recovery-phrase.json"


def keep_recovery_phrase(keydir, created_output: str, seal_with: str = "") -> bool:
    """Keep the recovery phrase of a key this process just created, for the owner to write down.

    `dendrad keys add` prints the phrase once, into this container's output, which nobody reads: the
    account and its rewards would then exist with no way to recover them. The phrase is
    written next to the keyring it can rebuild, in the same volume, mode 0600 from its creation. With an
    encrypted keyring it is sealed under the same passphrase (`seal_with`), so it never sits unencrypted
    next to a key that does not; with the `test` keyring it is written unencrypted, which adds no exposure
    there: that keyring already holds the private key unencrypted in the volume. The Dendra application shows it
    once, asks the owner to type three of its words back, and only then removes it from the machine.
    Nothing is written for a key that already existed: its phrase is not known here, and inventing a
    file for it would be worse than having none.

    An existing file is never replaced. It is the phrase of an earlier key, not yet confirmed (confirming
    removes it), and that key may hold the stake: replacing it would delete the only copy of its words.
    `keys_addr` refuses to create a key while one is kept; this is the last safeguard of that rule. The file
    is written under a temporary name, then linked to its name, because a link refuses an existing name."""
    if not keydir:
        return False
    try:
        d = json.loads(created_output[created_output.find("{"):created_output.rfind("}") + 1])
    except ValueError:
        return False
    words = str(d.get("mnemonic", "")).split()
    if len(words) not in (12, 24):
        return False
    path = os.path.join(keydir, RECOVERY_FILE)
    body = json.dumps({"address": d.get("address", ""), "name": d.get("name", ""),
                       "mnemonic": " ".join(words)}).encode("utf-8")
    tmp = os.path.join(keydir, f".{RECOVERY_FILE}.{os.getpid()}.new")
    crypto.store_secret(tmp, body, seal_with, crypto.AAD_RECOVERY)
    try:
        os.link(tmp, path)
    except FileExistsError:
        print(f"[daemon] WARNING: {path} already holds the recovery phrase of an earlier key; it is kept. The "
              f"phrase of the new key {d.get('address', '')} is not kept: that key has no recovery phrase on "
              f"this machine.", flush=True)
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"[daemon] recovery phrase of the new key kept in {path} (mode 0600, "
          f"{'encrypted with the keyring passphrase' if seal_with else 'unencrypted, like the test keyring'}): "
          f"the Dendra application shows it once, then removes it once three of its words are typed back. "
          f"Write it down; it is the only way to recover this account.", flush=True)
    return True


def keys_addr(name, keydir=None):
    """The address of the key `name`, creating the key only when it is absent.

    Three states, never two. An encrypted keyring answers a missing or wrong passphrase with "<name> is
    not a valid name or address" -- the words of an absent key. Reading "no address" as "no key" would
    create a new key in silence: a new address, a new identity, an unfunded account, while the stake stays
    on chain under the old one. `modea.keyring.key_state` calls a key absent only when dendrad says so
    without a passphrase refusal and the disk holds no `<name>.info`; anything else raises, and nothing
    is created."""
    kr = _keyring()
    state, val = kring.key_state(kr, name)
    if state == kring.PRESENT:
        return val
    if state != kring.ABSENT:
        raise kring.KeyringUnreadable(f"the key '{name}' cannot be read ({val}). No new key was created.")
    # A kept phrase blocks a creation. It is the phrase of an earlier key, never confirmed as written down,
    # and the file has one name: creating now would replace the only copy of words whose key may still hold
    # the stake (an identity file that could not be written, a MINER_ID naming another key). Said, and stopped.
    kept = os.path.join(keydir, RECOVERY_FILE) if keydir else ""
    if kept and os.path.lexists(kept):
        head = kring.recovery_head(kept, kr.open_with)
        whose = head.get("address") or "a key this process cannot name"
        raise kring.RecoveryPending(f"the key '{name}' is absent, and {kept} still holds the recovery phrase of "
                                    f"{whose}, never confirmed as written down. No new key was created.")
    rc, out, err = kring.create_key(kr, name)
    keep_recovery_phrase(keydir, out or "", seal_with=kr.seal_with)
    state, val = kring.key_state(kr, name)
    if state == kring.PRESENT:
        print(f"[daemon] new key '{name}' created in the {kr.backend} keyring ({kr.directory}).", flush=True)
        return val
    # Never from its stdout. On success `keys add --output json` prints one line whose last field is the
    # mnemonic; a message built from that line would carry the words into `docker logs`, the application's
    # log view and the heartbeat. The reason is read from stderr only.
    why = kring.explain(err or "") or kring.last_line(err or "") or "no message on stderr"
    raise kring.KeyringUnreadable(f"the key '{name}' could not be created and read back (dendrad exited "
                                  f"{rc}: {why}; then: {val})")


def align_identity(name, addr, owner=""):
    """Return the identifier the chain will accept, renaming the keyring key to match.

    The chain derives it from the key that signs create-miner: this machine's key `addr`, or in owner mode
    the owner's address `owner` (the key that registers the miner, which is not on this machine). The
    keyring entry still holds this machine's key; only its name follows the identifier.

    Since the third consensus epoch, CreateMiner derives the miner id from the signer's address and
    refuses any other value. Nothing else in this kit derives it: `join.sh` invents an `m-<hash>` for a
    fresh operator, so without this alignment `create-miner` is refused and the node runs outside the
    registry, receiving no job.

    It runs before any file is named after the identifier. The x25519, attestation and VRF keys all live
    at `<keydir>/<id>.*`; aligning after they exist would orphan them and create a second encryption
    identity without a word.

    It never guesses. If the derivation cannot be computed, or the target key name is already taken by a
    different address, it keeps the current name and says why: registering under a refused identifier
    can be recovered from, adopting someone else's key name cannot."""
    try:
        from modea.miner_id import miner_id_for_account
        expected = miner_id_for_account(owner or addr)
    except Exception as e:
        print(f"[daemon] cannot derive the on-chain identifier ({type(e).__name__}: {e}).\n"
              f"         Keeping '{name}'. If the chain refuses the registration, this is why.", flush=True)
        return name
    if name == expected:
        return name

    # Is the target name already taken, and by whom? Adopting a key that is not ours would give this miner
    # an identity that signs with a different address: every commit refused, with nothing saying why.
    show = _krun(["dendrad", "keys", "show", expected, "-a", *_kr()])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    if m and m.group(1) != addr:
        print(f"[daemon] refusing to adopt the derived identifier {expected}: a key of that name already\n"
              f"         exists in this keyring and holds a different address ({m.group(1)}).\n"
              f"         Keeping '{name}'. Resolve the keyring by hand before restarting.", flush=True)
        return name
    if m:
        print(f"[daemon] on-chain identity is {expected} (key already present); adopting it.", flush=True)
        return expected

    out = _krun(["dendrad", "keys", "rename", name, expected, "-y", *_kr()])
    show = _krun(["dendrad", "keys", "show", expected, "-a", *_kr()])
    if re.search(r'(dendra1[0-9a-z]+)', show or ""):
        print(f"[daemon] identity aligned with the chain: '{name}' -> {expected}\n"
              f"         (the chain derives the miner id from the signer address and refuses any other"
              + (f"; the signer of create-miner is the owner {owner})" if owner else ")"),
              flush=True)
        return expected
    print(f"[daemon] could not rename the keyring key '{name}' -> '{expected}': {str(out)[:200]}\n"
          f"         Keeping '{name}'; the registration will be refused until this is fixed.", flush=True)
    return name


_JOURNAL = "pending-commitments.json"
# The journal's earlier name, in French, as an older kit wrote it. A volume of an older kit may hold commitments in flight under it: they are read from it while the current file does
# not exist, and the old file is removed once the current one is written. Never written itself.
_JOURNAL_BEFORE_2026_10_10 = "engagements-en-attente.json"


def _journal_path(keydir) -> Path:
    return Path(keydir) / _JOURNAL


def _retire_old_journal(keydir) -> None:
    """Called once the current journal is written: the old file's content is in it (it was read from there)."""
    try:
        (Path(keydir) / _JOURNAL_BEFORE_2026_10_10).unlink()
    except OSError:
        pass


def _read_journal(keydir) -> dict:
    """The register of commitments computed but not yet anchored.

    It lives next to the keys because it has the same lifetime as a miner identity: losing either means
    the same thing. An unreadable file yields {} -- a commitment is never guessed, and losing the
    journal falls back to the previous behaviour: degraded, never wrong."""
    path = _journal_path(keydir)
    if not path.exists() and (Path(keydir) / _JOURNAL_BEFORE_2026_10_10).exists():
        path = Path(keydir) / _JOURNAL_BEFORE_2026_10_10
    try:
        with io.open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _journal_commitment(keydir, key: str, commit: str, pcommit: str, model_id: str = "",
                        weights_hash: str = "") -> None:
    """Makes the commitment durable before the deposit and before the anchoring.

    Atomic write. A half-written journal would be worse than none: the resumption would read a
    truncated commitment and anchor something other than what the client can open.

    The model is journalled with it (`model_id`, `weights_hash`: the flags of the first anchoring attempt), so a
    resumption anchors the commitment with the model that answered, never with the one this process serves when
    it resumes (see _journal_flags)."""
    if not commit:
        return
    d = _read_journal(keydir)
    entry = {"commit": commit, "pcommit": pcommit or commit, "ts": int(time.time())}
    if model_id:
        entry["model_id"] = str(model_id)
    if weights_hash:
        entry["weights_hash"] = str(weights_hash)
    d[key] = entry
    target = _journal_path(keydir)
    tmp = target.with_suffix(".tmp")
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, target)
        _retire_old_journal(keydir)
    except Exception as e:
        print(f"[daemon] commitment journal not written ({type(e).__name__}: {e}) -> an anchoring"
              f" failure on {key} would become permanent again", flush=True)


def _pending_commitment(keydir, key: str) -> dict:
    return _read_journal(keydir).get(key) or {}


def _journal_flags(entry) -> list:
    """The --model-id / --weights-hash flags a journalled commitment is anchored with: the ones journalled next
    to it, in the order the first attempt used. Without them an armed model registry (enforce_model_registry)
    would refuse every resumed commit for good, and otherwise the commit would land with an empty model_id.
    An entry journalled before these fields existed carries neither and is anchored without them, as it was
    then, never with this process's MODEL_ID, which may have changed since."""
    flags = []
    if isinstance(entry, dict):
        if entry.get("model_id"):
            flags += ["--model-id", str(entry["model_id"])]
        if entry.get("weights_hash"):
            flags += ["--weights-hash", str(entry["weights_hash"])]
    return flags


def _forget_commitment(keydir, key: str) -> None:
    """Call only once the chain carries the commitment: the journal is never cleared on a hope, it is
    cleared on a measurement."""
    d = _read_journal(keydir)
    if key not in d:
        return
    d.pop(key, None)
    target = _journal_path(keydir)
    tmp = target.with_suffix(".tmp")
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, target)
        _retire_old_journal(keydir)
    except Exception:
        pass


# -- A request that fails is remembered, not replayed at every pass ----------------------------------------
# Retrying a failed inference or a refused deposit at every pass would cost one generation every few seconds
# for a request that fails for good, and delay every request behind it. Each failure is written to
# JOB_FAILURES, a file of its own next to the keys. It is not the commitment journal: an entry there promises
# an anchoring, and a retry schedule must never be read as one, nor cleared with one.
#   * the next attempt waits JOB_RETRY_PAUSES_S: the first pause after the first failure, the second after the
#     second, the last after the third and every one after it;
#   * a failed inference counts towards giving the request up only when the engine then answered a test
#     request and embedded it (serving_probe): a failure while the engine cannot do both is the engine's and
#     says nothing about the request. After job_max_attempts() such failures the request is abandoned and
#     never generated again;
#   * a deposit the relay refused keeps its sealed response in the file, and the next attempt deposits that
#     response again, never a new inference: the commitment journalled with it describes that response;
#   * a refused anchoring on the resumption path waits the same pauses. Neither of these two is ever abandoned:
#     a journalled commitment is an obligation, kept until the relay stops listing the request.
# Entries for the requests the relay no longer lists are dropped. An unreadable file reads as empty, so every
# request is simply retried: degraded, never wrong.
JOB_FAILURES = "job-failures.json"
# Chosen, not measured: short enough that a passing hiccup costs a minute, long enough that a request failing
# for good costs a handful of generations an hour instead of one per pass.
JOB_RETRY_PAUSES_S = (60.0, 300.0, 1800.0)
JOB_MAX_ATTEMPTS_DEFAULT = 3
_FAILURES_WARNED = {"read": False, "write": False}
_ENV_SAID: set = set()


def _env_positive(name, default, cast):
    """A positive number read from the environment; empty or unset is `default`. A value that is not a positive
    number is logged once, and `default` is used: a typo must not silently disable what the setting tunes."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except (TypeError, ValueError, OverflowError):
        v = None
    if v is not None and v == v and v > 0:      # `v == v` refuses a NaN
        return v
    if name not in _ENV_SAID:
        _ENV_SAID.add(name)
        print(f"[daemon] {name}={raw!r} is not a positive number: {default} is used. Said once.", flush=True)
    return default


def job_max_attempts() -> int:
    """How many failed inferences, each followed by an engine that answered, give a request up
    (DENDRA_JOB_MAX_ATTEMPTS)."""
    return _env_positive("DENDRA_JOB_MAX_ATTEMPTS", JOB_MAX_ATTEMPTS_DEFAULT, int)


def job_retry_pause_s(failures) -> float:
    try:
        n = max(1, int(failures))
    except (TypeError, ValueError):
        n = 1
    return JOB_RETRY_PAUSES_S[min(n, len(JOB_RETRY_PAUSES_S)) - 1]


def _read_failures(keydir) -> dict:
    p = Path(keydir) / JOB_FAILURES
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if not _FAILURES_WARNED["read"]:
            _FAILURES_WARNED["read"] = True
            print(f"[daemon] {p} could not be read ({type(e).__name__}): the requests it paused are retried as "
                  f"if nothing had failed. Said once.", flush=True)
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _write_failures(keydir, fails) -> None:
    """Atomic and private (crypto.write_private). A write that fails is logged once and never stops the loop: its
    cost is a request retried sooner than it should be after a restart."""
    try:
        crypto.write_private(os.path.join(keydir, JOB_FAILURES), (json.dumps(fails) + "\n").encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if not _FAILURES_WARNED["write"]:
            _FAILURES_WARNED["write"] = True
            print(f"[daemon] {keydir}/{JOB_FAILURES} could not be written ({type(e).__name__}): after a restart, "
                  f"a paused request may be retried before its pause is over. Said once.", flush=True)


def note_job_failure(fails, key, kind, why, now, counted=False, sealed=None) -> dict:
    """Records one failure of `kind` ("inference" | "deposit" | "anchor") for the request `key` in `fails`
    (the file's content; the caller writes it) and returns the entry. A failure of another kind than the
    entry's starts the entry over: an inference that now succeeds followed by a failed deposit is a new case.
    `counted`: the engine answered a test request after this failed inference (engine_answered_since); only
    such failures give a request up. `sealed`: the response a refused deposit keeps."""
    e = fails.get(key) if isinstance(fails.get(key), dict) else {}
    if e.get("kind") != kind:
        e = {"kind": kind, "failures": 0, "counted": 0}
    e["failures"] = int(e.get("failures", 0) or 0) + 1
    if counted:
        e["counted"] = int(e.get("counted", 0) or 0) + 1
    e["why"] = str(why)[:200]
    e["last_at"] = round(float(now), 3)
    e["next_at"] = round(float(now) + job_retry_pause_s(e["failures"]), 3)
    if sealed is not None:
        e["sealed"] = sealed
    if kind == "inference" and e["counted"] >= job_max_attempts():
        e["abandoned"] = True
    fails[key] = e
    return e


def job_due(entry, now) -> bool:
    """May the request this entry describes be attempted now? An absent entry: yes. Abandoned: never."""
    if not isinstance(entry, dict) or not entry:
        return True
    if entry.get("abandoned"):
        return False
    try:
        return float(now) >= float(entry.get("next_at", 0) or 0)
    except (TypeError, ValueError):
        return True


# -- The engine's latest answer to a test request ---------------------------------------------------------
# Every test inference this process runs -- the one an availability proof waits for, the one a failed request
# asks for -- leaves its time here, as answered or not. A failed inference counts against its request only when
# the engine answered after it.
_ENGINE = {"answered_at": 0.0, "unanswered_at": 0.0}


def recorded_probe(probe):
    """`probe` (a callable rendering presence_probe's triple), with its outcome kept as the engine's reading."""
    def run_it():
        st, why, secs = _probe_now(probe)
        _ENGINE["answered_at" if st == PROBE_ANSWERED else "unanswered_at"] = time.time()
        return st, why, secs
    return run_it


def engine_answered_since(t, probe) -> bool:
    """Did the engine answer a test request at or after `t`? The latest reading decides when it is that recent;
    otherwise one test request is run now. A probe that cannot run is not an answer."""
    if _ENGINE["answered_at"] >= t:
        return True
    if _ENGINE["unanswered_at"] >= t:
        return False
    st, _, _ = recorded_probe(probe)()
    return st == PROBE_ANSWERED


# -- A request nobody waits for any more is not served, and the newest are served first -----------------------
# After an outage, serving the queue oldest first would deliver answers long after their client gave up, while
# fresh requests waited behind them. So the keys are walked newest first, and a new request older than
# request_max_age_s() is not served. Its age is read in its identifier, `job<milliseconds since the epoch>`
# (client.submit_job); an identifier of another form has no readable age, and a request of unknown age
# is served after those whose age is known. The filter never applies to a request whose commitment is
# journalled: that request was served, and only its anchoring is left.
REQUEST_MAX_AGE_S_DEFAULT = 600.0
_RE_JOB_MS = re.compile(r"^job(\d{13})$")


def request_max_age_s() -> float:
    """The age past which a new request is not served (DENDRA_REQUEST_MAX_AGE_S). The default is chosen, not
    measured: the client the Final Testnet Season runs waits a bounded time for the answer and then for its
    commitment (client.quick_metered, its `timeout`, then settle_when_ready), and a request older than
    both is one nobody waits for."""
    return _env_positive("DENDRA_REQUEST_MAX_AGE_S", REQUEST_MAX_AGE_S_DEFAULT, float)


def request_age_s(jid, now):
    """Seconds since the request `jid` was made, read in its identifier, or None when the identifier is not
    `job<13 digits>`. A negative age (a client clock ahead of this one) is returned as read: it is not stale."""
    m = _RE_JOB_MS.match(str(jid))
    if not m:
        return None
    return float(now) - int(m.group(1)) / 1000.0


# -- Two clocks judge a request stale, never one -----------------------------------------------------------------
# The age compares the client's clock (the identifier) with this machine's. A machine whose clock runs ahead of the
# real time by more than request_max_age_s() would call every fresh request stale and still prove its presence, so
# the chain would draw a miner that serves nothing. A request this machine's clock calls stale is therefore judged
# again against the chain's clock (chain_now): the time of the latest block, carried forward between two readings
# by the monotonic clock, which nothing sets. The younger age decides, so the second clock can only make the
# filter serve more: a chain that stalls, or a node that is catching up, reads as an old time and lets every
# request through. While no block time has been read, this machine's clock decides alone. Not covered: a client
# clock behind the real time, since its requests carry the time it wrote in them.
CHAIN_CLOCK_READ_S = 30.0   # Chosen: at most one `dendrad status` per this many seconds, only while a request is judged
_CHAIN_CLOCK = {"block_time": None, "mono_at": 0.0, "tried_at": None, "said": False, "unread_said": False}
_RE_RFC3339 = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2})$")


def rfc3339_epoch(text):
    """Seconds since the epoch of an RFC 3339 time as CometBFT writes `latest_block_time` (fractional seconds,
    `Z` or an offset), or None. A time without a zone is None: its zone is not guessed."""
    m = _RE_RFC3339.match(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    try:
        base = datetime.datetime(*(int(x) for x in m.groups()[:6]), tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:                       # a month 13, a day 31 in April: not a time
        return None
    frac = float("0." + m.group(7)) if m.group(7) else 0.0
    tz = m.group(8)
    off = 0 if tz in ("Z", "z") else (1 if tz[0] == "+" else -1) * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return base + frac - off


def chain_block_time():
    """`latest_block_time` of the node's status, in seconds since the epoch, or None when it was not read."""
    si = _node_sync_info()
    return rfc3339_epoch(si.get("latest_block_time")) if si is not None else None


def chain_now():
    """The chain's present time as this process can tell it, or None while no block time was ever read. Read at
    most every CHAIN_CLOCK_READ_S; a reading that fails keeps the previous one, carried forward."""
    mono = time.monotonic()
    c = _CHAIN_CLOCK
    if c["tried_at"] is None or mono - c["tried_at"] >= CHAIN_CLOCK_READ_S:
        c["tried_at"] = mono
        bt = chain_block_time()
        if bt is not None:
            c["block_time"], c["mono_at"] = bt, mono
    if c["block_time"] is None:
        return None
    return c["block_time"] + max(0.0, mono - c["mono_at"])


def request_stale_age(jid, now, chain_clock=None):
    """The age at which the new request `jid` is judged stale (not served), or None when it is served: older than
    request_max_age_s() by this machine's clock (`now`) and, once the chain's clock has been read (`chain_clock`,
    chain_now, asked only here), by the chain's. A chain's clock that calls it fresh is logged once, with the gap."""
    limit = request_max_age_s()
    age = request_age_s(jid, now)
    if age is None or age <= limit:
        return None
    cnow = chain_clock() if chain_clock is not None else None
    if cnow is None:
        if chain_clock is not None and not _CHAIN_CLOCK.get("unread_said"):
            _CHAIN_CLOCK["unread_said"] = True
            print(f"[daemon] the chain's clock could not be read (`dendrad status`, latest_block_time): a request "
                  f"older than {limit:.0f} s by this machine's clock alone is not served. Said once.", flush=True)
        return age
    chain_age = request_age_s(jid, cnow)
    if chain_age is not None and chain_age <= limit:
        ahead = int(float(now) - float(cnow))
        _status_note(clock_ahead_of_chain_s=ahead)
        if not _CHAIN_CLOCK["said"]:
            _CHAIN_CLOCK["said"] = True
            print(f"[daemon] this machine's clock is about {ahead} s ahead of the chain's latest block: a request it "
                  f"calls older than {limit:.0f} s is judged by the chain's clock, which calls it fresh, and is "
                  f"served. Set this machine's clock (NTP). Said once.", flush=True)
        return None
    return age if chain_age is None else min(age, chain_age)


def newest_first(keys, suffix):
    """This miner's keys (ending with `suffix`) of a relay listing, the most recent request first. A key whose age
    cannot be read comes after every key whose age can, in the reverse of the listing's order."""
    mine = [k for k in (keys or []) if isinstance(k, str) and suffix and k.endswith(suffix) and len(k) > len(suffix)]

    def order(item):
        i, k = item
        m = _RE_JOB_MS.match(k[: -len(suffix)])
        return (int(m.group(1)) if m else -1, i)
    return [k for _, k in sorted(enumerate(mine), key=order, reverse=True)]


def bal_token(addr):
    """udndr held by `addr`: the amount read; 0 when the answer was read and names no udndr (proto3 omits an empty
    balance); None when nothing was read. A query that failed is never an empty account: read as one, it would
    ask the faucet again for an address whose balance simply could not be read."""
    try:
        d = json.loads(run(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()]))
    except Exception:  # noqa: BLE001 -- no document: not read
        return None
    if not isinstance(d, dict):
        return None
    coins = d.get("balances") or []
    if not isinstance(coins, list):
        return None
    for c in coins:
        if isinstance(c, dict) and c.get("denom") == "udndr":
            try:
                return int(c.get("amount") or 0)
            except (TypeError, ValueError):
                return None
    return 0


def miner_operator(mid):
    """Address of the operator registered for this miner_id ("" if the miner does not exist)."""
    m = re.search(r'operator:\s*"?(dendra1[0-9a-z]+)"?', query("get-miner", mid))
    return m.group(1) if m else ""


PRESENT, ABSENT, UNREAD = "present", "absent", "unread"


def registry_record(mid):
    """(state, record) of `mid` in the chain's miner registry, three states, never two:
      ("present", {"creator", "operator", "region", "stake", "enc_pubkey", "vrf_pubkey"}) -- a record was read;
          inside it an absent string is "" and an absent number 0 (proto3 omits zero values);
      ("absent", {})  -- the chain answered NotFound, its own word for "no such miner";
      ("unread", {"why": ...}) -- anything else: a node that does not answer is never "not registered".
    The JSON is parsed, never searched as text."""
    out = query("get-miner", mid, flags=("--output", "json"))
    try:
        d = json.loads(out[out.find("{"):out.rfind("}") + 1]) if "{" in out else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        if "code = NotFound" in out:
            return ABSENT, {}
        return UNREAD, {"why": " ".join(str(out).split())[:200] or "no answer"}
    m = d.get("miner")
    if not isinstance(m, dict):
        return UNREAD, {"why": "the answer is not a miner record"}
    try:
        stake = int(m.get("stake", 0) or 0)
    except (TypeError, ValueError):
        return UNREAD, {"why": f"stake is not an integer: {m.get('stake')!r}"}
    return PRESENT, {"creator": str(m.get("creator", "") or ""), "operator": str(m.get("operator", "") or ""),
                     "region": str(m.get("region", "") or ""), "stake": stake,
                     "enc_pubkey": str(m.get("enc_pubkey", "") or ""),
                     "vrf_pubkey": str(m.get("vrf_pubkey", "") or "")}


def miner_vrf_onchain(mid):
    """The vrf_pubkey anchored on-chain for this miner: the key read, "" when the record carries none (the zero
    value of a string, which proto3 omits), or None when no record was read -- the registry did not answer, or
    does not hold this miner yet.

    Read from the chain, never assumed from what this process holds locally. A key generated here is
    not a key the chain knows: anchoring happens at registration, and a miner already in the registry
    keeps whatever it registered with until an explicit rotation.
    None never means "no key": a node that did not answer must not make main() announce a missing key
    that nobody has read."""
    st, rec = registry_record(mid)
    return rec.get("vrf_pubkey", "") if st == PRESENT else None


SUBSIDY_MIN = int(os.environ.get("DENDRA_SUBSIDY_MIN", "50000"))     # claim from 0.05 DNDR up


def claimable_subsidy(mid):
    """udndr this miner may claim now, or None when it could not be read.

    The cap is the chain's (`msg_server_claim_subsidy.go`): demand x work_gate_bps / 10000, minus what was
    already claimed. Rule of zero: inside an answer that was read, an absent field is 0; a query that
    failed is None, and None never becomes a claim."""
    try:
        m = json.loads(query("get-miner", mid, flags=("--output", "json")))
        p = json.loads(query("params", flags=("--output", "json")))
    except (ValueError, TypeError):
        return None
    rec, prm = m.get("miner"), p.get("params")
    if not isinstance(rec, dict) or not isinstance(prm, dict):
        return None
    cap = int(rec.get("demand", 0) or 0) * int(prm.get("work_gate_bps", 0) or 0) // 10000
    return max(0, cap - int(rec.get("subsidy_claimed", 0) or 0))


def maybe_claim_subsidy(mid) -> str:
    """Claims the subsidy when at least SUBSIDY_MIN is claimable. The subsidy otherwise sits unclaimed:
    nothing else in the network claims it for the miner."""
    c = claimable_subsidy(mid)
    if c is None:
        return "subsidy not read"
    if c < SUBSIDY_MIN:
        return f"subsidy {c} udndr, below {SUBSIDY_MIN}: not claimed yet"
    out = tx_from(mid, "claim-subsidy", mid)
    ok = wait_tx(out)
    return f"subsidy claim of up to {c} udndr {'confirmed' if ok else 'not confirmed: ' + _tx_err(out)}"


def _params_doc(out):
    """The jobs params document dendrad printed, as a dict, or None when `out` is not one. It is read in the
    form asked for (`--output json`), or in the CLI's text form of the same document: its first line is
    `params:` and every line after it one indented `key: value`. Anything else -- a nested field the text form
    cannot carry flat, a line of another kind -- is not a document read: a field cut off by a parse that stopped
    early would read as absent, so as a zero, and a zero is a reading."""
    try:
        d = json.loads(out)
    except (TypeError, ValueError):
        d = None
    if isinstance(d, dict):
        p = d.get("params")
        return p if isinstance(p, dict) else None
    lines = [ln for ln in str(out or "").splitlines() if ln.strip()]
    if not lines or lines[0].rstrip() != "params:":
        return None
    doc = {}
    for ln in lines[1:]:
        m = re.match(r'^\s+([a-z0-9_]+):\s*"?([^"]*)"?\s*$', ln)
        if not m:
            return None
        doc[m.group(1)] = m.group(2)
    return doc


def chain_min_stake():
    """min_stake read from the chain, never a local constant: the value read, 0 when the params document was read
    and carries none (proto3 omits a zero), None when it was not read.

    `min_stake` is a governable parameter: a value hardcoded here diverges as soon as a vote changes it,
    and a stake below the real minimum makes every `create-miner` fail while the miner may look ready.
    There is no default either: a registration sent with a guessed stake is refused, and in owner mode
    the owner would be handed a create-miner to sign offline for nothing. Unread means "send nothing,
    read again later" (register_attempt, owner_wait).
    """
    try:
        p = _params_doc(query("params", flags=("--output", "json")))
    except Exception:  # noqa: BLE001 -- a query that raises is a query not read
        return None
    if p is None:
        return None
    try:
        return int(p.get("min_stake") or 0)
    except (TypeError, ValueError):
        return None


def _faucet_pow_bits(faucet):
    """PoW difficulty announced by the faucet (GET /), or None if the probe fails.

    None is not 0: `0` means "the faucet declares the PoW disarmed", None means "unknown". Treating them
    alike would post a request without a token while believing the contract was honoured.
    """
    try:
        with urllib.request.urlopen(faucet, timeout=10) as r:
            return int(json.loads(r.read()).get("pow_bits", 0))
    except Exception:
        return None


# Why the faucet refused, as one word. A refusal of the faucet is one of three anti-abuse caps (a 429 whose
# JSON `info` is one of faucet's REFUSAL_* constants), a kit below the network's floor (a 403 whose
# JSON carries the floor as an integer under faucet.KIT_MIN_FIELD), a proof of
# work it did not accept (a 400 that names `pow_bits`), another HTTP answer, or no answer at all. The `info`
# is parsed and compared with the faucet's own constants (this module imports it), never matched as text and
# never copied here: a server whose `info` matches none of them is `unknown` -- never `ip_quota` by default,
# since that word stops deploy/join.sh from starting the next identities of a rig. `stale_kit` is read by
# deploy/join.sh too: retrying does nothing for it, only the update does, and join.sh names the update.
FAUCET_REASONS = {"ip_quota", "addr_cooldown", "global_cap", "stale_kit", "pow", "transport", "unknown"}  # plus http_<code>


def faucet_reason(code, raw) -> str:
    """The class of a faucet refusal from its HTTP code and body (see FAUCET_REASONS)."""
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        doc = None
    if not isinstance(doc, dict):
        doc = {}
    if code == 429:
        info = doc.get("info")
        for word, text in (("ip_quota", faucet_pow.REFUSAL_IP_QUOTA),
                           ("addr_cooldown", faucet_pow.REFUSAL_ADDR_COOLDOWN),
                           ("global_cap", faucet_pow.REFUSAL_GLOBAL_CAP)):
            if info == text:
                return word
        return "unknown"
    if code == 403:
        # The floor is read by field, as an integer; a 403 that carries none (a proxy, another refusal) stays
        # an HTTP code. A bool is an int in Python and is not a floor.
        floor = doc.get(faucet_pow.KIT_MIN_FIELD)
        if isinstance(floor, int) and not isinstance(floor, bool):
            return "stale_kit"
    if code == 400 and "pow_bits" in doc:
        return "pow"
    return f"http_{int(code)}" if isinstance(code, int) else "unknown"


def _faucet_post(faucet, addr, nonce):
    """Post the request. Returns (ok, detail, required_bits, reason) — required_bits = 0 when the server
    names none; reason is "" on success, else a word of faucet_reason.

    The body of a refusal is read: the faucet names the cause and, on a PoW refusal, the expected
    difficulty. Discarding it would make the failure undiagnosable from the miner's machine.
    """
    # The kit this daemon runs, under the field the faucet reads (faucet.py::declared_kit_version):
    # from DENDRA_KIT_DEADLINE a request that declares a number below the floor is refused before the proof
    # of work, and one that declares none is served. The number comes from the file the
    # relay deposits read too (modea.kit_version). Unreadable, the body carries no field and main() says so at
    # start; no 0 is written in its place, since a 0 would read as a kit older than every release.
    body = kitv.with_kit_version({"address": addr}, kitv.read_kit_version())
    if nonce:
        body["pow"] = nonce
    req = urllib.request.Request(faucet, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True, "", 0, ""
    except urllib.error.HTTPError as e:
        raw = ""
        # The whole body is parsed, bounded only against a server that would never stop sending: the stale-kit
        # refusal names the update in its `info` and carries its floor after it, so a body cut at a few hundred
        # bytes parses as nothing and the refusal reads as a bare HTTP code. Only the human detail is shortened.
        try:
            raw = e.read(65536).decode("utf-8", "replace")
        except Exception:
            pass
        bits = 0
        try:
            bits = int(json.loads(raw).get("pow_bits", 0))
        except Exception:
            pass
        return False, f"HTTP {e.code} {raw[:300]}".strip(), bits, faucet_reason(e.code, raw)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", 0, "transport"


def faucet_fund_classified(faucet, addr):
    """Fund the address at the faucet. Returns (ok, detail, reason) — a failure is always named, and classed.

    The public faucet requires a PoW bound to the address (DENDRA_FAUCET_POW_BITS > 0): a request
    without a token takes a 400, and the miner then runs `create-miner` on an empty account, with
    neither funds nor registration. The solver is the faucet's own (`faucet.solve_pow`): one
    piece of code on both sides of the contract.
    """
    bits = _faucet_pow_bits(faucet)
    nonce = ""
    if bits:
        nonce = _solve_faucet_pow(addr, bits)
        if not nonce:
            return False, f"faucet PoW unsolved within {POW_MAX_S:.0f} s ({bits} bits)", "pow"
    ok, detail, want_bits, reason = _faucet_post(faucet, addr, nonce)
    if ok:
        return True, "", ""
    # The server names a difficulty that was not honoured (GET probe unavailable, or difficulty raised
    # between the probe and the request) -> a single retry, with the required token.
    if want_bits > 0 and not nonce:
        nonce = _solve_faucet_pow(addr, want_bits)
        if not nonce:
            return False, f"faucet PoW unsolved within {POW_MAX_S:.0f} s ({want_bits} bits)", "pow"
        ok, detail, _, reason = _faucet_post(faucet, addr, nonce)
        if ok:
            return True, "", ""
    return False, detail, reason


def faucet_fund(faucet, addr):
    """Fund the address at the faucet. Returns (ok, detail): faucet_fund_classified without the class."""
    ok, detail, _ = faucet_fund_classified(faucet, addr)
    return ok, detail


def _solve_faucet_pow(addr, bits):
    def _tick(attempts, elapsed):
        print(f"[daemon] faucet PoW ({bits} bits): {attempts} attempts, {elapsed:.0f} s ...", flush=True)
    print(f"[daemon] faucet PoW required: {bits} bits bound to {addr} -> solving "
          f"(bounded at {POW_MAX_S:.0f} s, tunable via DENDRA_FAUCET_POW_MAX_S).", flush=True)
    t0 = time.time()
    nonce = faucet_pow.solve_pow(addr, bits, deadline_s=POW_MAX_S, progress=_tick)
    if nonce:
        print(f"[daemon] PoW solved in {time.time() - t0:.1f} s.", flush=True)
    return nonce


def pick_backend(want):
    """Chooses the backend, and never falls back to the mock silently in production.
    A miner without a GPU would collect rewards for fake text anchored on chain as real, and several
    deterministic mocks would agree, form a majority and get the real miners evicted or slashed.
    The mock is allowed only with DENDRA_ALLOW_MOCK=1 (tests); otherwise the start fails."""
    allow_mock = os.environ.get("DENDRA_ALLOW_MOCK", "0") == "1"
    if want == "mock":
        if not allow_mock:
            print("[daemon] FATAL: backend 'mock' requested but DENDRA_ALLOW_MOCK!=1 (anti fake-miner guard).")
            sys.exit(3)
        print("[daemon] explicit mock backend (tests) -> not intended for production")
        return "mock"
    if want == "ollama":
        try:
            m = Miner("probe", backend="ollama")
            m.backend.generate("ok")
            # Where it runs (a GPU, or the CPU for the judge role only) is measured before this process starts:
            # serve_guard.py, called by docker/entrypoint-services.sh. Reachable says nothing about it.
            print(f"[daemon] Ollama reachable ({m.backend.endpoint}) -> real LLM")
            return "ollama"
        except Exception as e:
            if allow_mock:
                print(f"[daemon] Ollama unreachable ({type(e).__name__}) -> mock fallback (DENDRA_ALLOW_MOCK=1)")
                return "mock"
            print(f"[daemon] FATAL: Ollama unreachable ({type(e).__name__}) and mock is forbidden in production. "
                  f"Start Ollama (or set DENDRA_ALLOW_MOCK=1 for tests).")
            sys.exit(3)
    print(f"[daemon] FATAL: unknown backend '{want}'.")
    sys.exit(3)


def _vrf_bin():
    """Path of the dendra-vrf binary. It is built by dendra_modea_vrf_avail.sh, in the
    development repository; from a published tree, build it from chain/cmd/dendra-vrf/.
    "" if absent."""
    for c in (os.path.expanduser("~/go/bin/dendra-vrf"), "/usr/local/bin/dendra-vrf"):
        if os.path.exists(c):
            return c
    return "dendra-vrf"  # otherwise, let PATH resolve it (subprocess will fail cleanly if absent)


def vrf_identity(keydir, mid):
    """Loads or creates the miner's Ed25519 VRF key via dendra-vrf. Returns (sk_hex, pk_hex), or ("","")
    when the binary is absent or unusable.

    ("","") is not a harmless fallback. Under verification_mode=1, the incentivised configuration, the chain
    closes the GPU-less echo: a miner with no anchored vrf_pubkey has ProveAvailability refused
    (ErrUnauthorized) and is skipped at payout. So ("","") means "this miner will never prove availability",
    and the caller must say so rather than register quietly without a key.

    The secret is written in the key envelope (crypto.store_secret): sealed under the keyring's passphrase
    in `file` mode, and created 0600 by os.open so that it is never readable by others, even for an
    instant. A secret found unencrypted while a passphrase is there to seal it is re-encrypted in place. A
    sealed secret that does not open raises: a VRF key that exists and cannot be read is not a missing
    one, and registering as if it were would anchor nothing."""
    vbin = _vrf_bin()
    vpath = Path(keydir) / f"{mid}.vrf"
    try:
        kr = _keyring()
        seal, opener = kr.seal_with, kr.open_with
    except kring.KeyringError:
        seal, opener = "", ""
    try:
        if vpath.exists():
            sk = crypto.load_secret(str(vpath), opener, crypto.AAD_VRF, seal_with=seal).decode("ascii").strip()
            # The key goes through the environment, never argv: /proc/<pid>/cmdline and `ps` are
            # world-readable, and none of the confinement this daemon applies covers that channel.
            pk = subprocess.run([vbin, "pubkey"], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "DENDRA_VRF_SK": sk}).stdout.strip()
        else:
            out = subprocess.run([vbin, "keygen"], capture_output=True, text=True, timeout=10).stdout.strip()
            sk, pk = out.split()
            crypto.store_secret(str(vpath), sk.encode("ascii"), seal, crypto.AAD_VRF)
        if len(sk) == 128 and len(pk) == 64:   # 64-byte sk / 32-byte pk, hex-encoded
            return sk, pk
    except crypto.KeyEnvelopeError:
        raise
    except Exception as e:
        print(f"[daemon] VRF unavailable ({type(e).__name__}): dendra-vrf could not be run. "
              f"This miner will register without a vrf_pubkey.")
        return "", ""
    # Reached when the binary ran but produced key material of the wrong size. It is logged, because an
    # operator who sees no message assumes there was nothing to see.
    print("[daemon] VRF key material has an unexpected size -> registering without a vrf_pubkey.")
    return "", ""


# -- The availability window: settled once, said once, remembered across restarts -----------------------
# A challenge is answered within `avail_deadline_blocks` of its epoch's first block (chain/x/jobs/keeper/
# msg_server_prove_availability.go, ProveAvailability: refused when h > rollH + avail_deadline_blocks, rollH
# the epoch's first block). The height is compared with the deadline before sending, so a closed window costs
# no refused transaction: one line names the block of the next challenge. The challenge this
# daemon settled (proven, or its window closed) is written to the key directory, so a restart neither proves
# it again nor repeats the line.
AVAIL_RECORD = "availability-last.json"
_AVAIL_RECORD_WARNED = False


def avail_window(epoch, eb, deadline, height):
    """(state, next_challenge_block). Three answers and one more, never a guess:
      open     the proof may be sent (no deadline, or the height is within it);
      closed   the height is past the epoch's first block + deadline: the chain would refuse it;
      stale    the height belongs to another epoch than the challenge read: read the challenge again;
      unknown  the height or the deadline was not read: nothing is pre-judged, the chain decides.
    `deadline` 0 is a reading (no deadline: the whole epoch counts); None is "not read"."""
    if not isinstance(eb, int) or eb <= 0:
        return "unknown", None
    nxt = (epoch + 1) * eb
    if deadline is None or height is None:
        return "unknown", nxt
    if height // eb != epoch:
        return "stale", nxt
    if deadline == 0:
        return "open", nxt
    return ("closed" if height > epoch * eb + deadline else "open"), nxt


def _node_sync_info():
    """The `sync_info` object of `dendrad status` (`SyncInfo` in older releases), or None when it was not read.
    Never raises."""
    try:
        out = run(["dendrad", "status", *_node()])
    except Exception:  # noqa: BLE001
        return None
    i = (out or "").find("{")
    if i < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(out[i:])
    except ValueError:
        return None
    si = (d.get("sync_info") or d.get("SyncInfo")) if isinstance(d, dict) else None
    return si if isinstance(si, dict) else None


def _chain_height():
    """The latest height the node reports, or None when it was not read (never 0 for "not read").
    Never raises: a reading that fails is a value, "not read", and the proof is then left to the chain."""
    si = _node_sync_info()
    try:
        return int(si["latest_block_height"]) if si is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _avail_deadline():
    """avail_deadline_blocks read from the jobs params: 0 when the answer omits it (proto3, zero value: no
    deadline), None when the params were not read. Never raises."""
    try:
        out = query("params", flags=("--output", "json"))
        d = json.loads(out)
    except Exception:  # noqa: BLE001 -- not a document: not read
        return None
    p = d.get("params") if isinstance(d, dict) else None
    if not isinstance(p, dict):
        return None
    try:
        return int(p.get("avail_deadline_blocks") or 0)
    except (TypeError, ValueError):
        return None


def _avail_record(keydir) -> dict:
    if not keydir:
        return {}
    try:
        d = json.loads((Path(keydir) / AVAIL_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _tx_on_chain(txhash) -> bool:
    """True when the node shows the transaction `txhash` included (a height) and accepted (code 0) on the chain
    it serves now. Anything else is False: no hash (a record written before the hash was kept), "not found" (a
    chain rescued by export keeps the challenge, not the transactions), a node that does not answer. False is
    never a claim that the proof failed: it only withholds the trust a record would otherwise skip a proof on."""
    if not txhash:
        return False
    try:
        q = run(["dendrad", "query", "tx", str(txhash), *_node()])
    except Exception:  # noqa: BLE001 -- not read: not trusted
        return False
    m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q or "")
    return bool(m and int(m.group(2)) > 0 and _tx_code(q) == 0)


def _avail_remember(keydir, **fields) -> None:
    """Writes the settled challenge. A failed write is logged once and never stops the loop: its cost is a
    proof attempted again after a restart, which the chain judges."""
    global _AVAIL_RECORD_WARNED
    if not keydir:
        return
    try:
        crypto.write_private(os.path.join(keydir, AVAIL_RECORD), (json.dumps(fields) + "\n").encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if not _AVAIL_RECORD_WARNED:
            _AVAIL_RECORD_WARNED = True
            print(f"[daemon] the settled availability challenge could not be written to {keydir}/{AVAIL_RECORD} "
                  f"({type(e).__name__}): after a restart this miner may send a proof for it again. Said once.",
                  flush=True)


def _window_closed(mid, keydir, chal, epoch, nxt, why):
    _avail_remember(keydir, challenge=chal, state="window_closed", epoch=epoch, next_at=nxt)
    _status_write(presence={"at": int(time.time()), "result": "window closed", "challenge": chal[:12],
                            "next_at": nxt, "why": why})
    print(f"[daemon] {mid} availability window closed for challenge {chal[:12]}... ({why}): no proof is sent "
          f"for it; next challenge at block {nxt if nxt is not None else '? (avail_epoch_blocks not read)'}.",
          flush=True)


# -- Presence is proven by a model that answers ----------------------------------------------------------
# The chain draws a request's primary among the miners present (an accepted availability proof), and a proof
# says nothing about the model: a miner whose engine lost its model, or never pulled it, could prove its
# presence, be drawn, and let the request expire. So a proof is sent only right after a test inference on the
# served model answered within a bound. Three states, and only the first proves anything:
#   answered    the served model returned a non-empty text within the bound;
#   silent      the engine answered without an answer: an HTTP error (the model absent or failing), or an
#               empty text -- a measured "no";
#   unmeasured  nothing came back within the bound, the engine could not be reached, or no probe was given.
# After a probe that did not answer, the next one waits PRESENCE_PROBE_RETRY_S: a hung engine would otherwise
# hold the loop for the whole bound at every tick. The window a proof must land in is read on chain
# (avail_deadline_blocks); a probe that answers inside it proves the challenge as before.
PROBE_ANSWERED, PROBE_SILENT, PROBE_UNMEASURED = "answered", "silent", "unmeasured"
PRESENCE_PROBE_PROMPT = "Reply with the single word: ready"
PRESENCE_PROBE_MAX_OUT = 16          # a short answer; the backend applies its own floor on top
PRESENCE_PROBE_RETRY_S = 60.0
# The bound. A test answer is a few tokens, so it is given much less time than a request: the client of the
# Final Testnet Season waits for a whole answer (client.py::quick_metered), and a model that cannot
# return a few tokens within this bound cannot serve that request either. DENDRA_PRESENCE_PROBE_S sets it; a
# value that is not a positive number is logged and replaced by the default.
PRESENCE_PROBE_S_DEFAULT = 120.0
_PROBE_STATE = {"failed_at": 0.0, "said_for": "", "bad_bound_said": False}


def presence_probe_bound_s() -> float:
    raw = os.environ.get("DENDRA_PRESENCE_PROBE_S", "").strip()
    if not raw:
        return PRESENCE_PROBE_S_DEFAULT
    try:
        v = float(raw)
    except ValueError:
        v = 0.0
    if v > 0:
        return v
    if not _PROBE_STATE["bad_bound_said"]:
        _PROBE_STATE["bad_bound_said"] = True
        print(f"[daemon] DENDRA_PRESENCE_PROBE_S={raw!r} is not a positive number of seconds: the test inference "
              f"is bounded at {PRESENCE_PROBE_S_DEFAULT:.0f} s. Said once.", flush=True)
    return PRESENCE_PROBE_S_DEFAULT


def presence_probe(backend, bound_s=None, clock=time.monotonic):
    """One test inference on the served model: (state, why, seconds). Never raises. Nothing of the prompt or of
    the answer is logged (inference.py's rule): only its length counts."""
    b = presence_probe_bound_s() if bound_s is None else float(bound_s)
    t0 = clock()
    try:
        out = backend.generate(PRESENCE_PROBE_PROMPT, max_out=PRESENCE_PROBE_MAX_OUT, timeout_s=b)
    except Exception as e:  # noqa: BLE001 -- every failure is a state, never a crash of the loop
        secs = round(clock() - t0, 1)
        code = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return (PROBE_SILENT, f"the engine refused the test request (HTTP {code}): the served model is "
                                  f"absent or failing", secs)
        return PROBE_UNMEASURED, f"no answer within {b:.0f} s ({type(e).__name__})", secs
    secs = round(clock() - t0, 1)
    if secs > b:
        return PROBE_UNMEASURED, f"the answer took {secs} s, past the {b:.0f} s bound", secs
    if not isinstance(out, str) or not out.strip():
        return PROBE_SILENT, "the served model returned an empty answer", secs
    return PROBE_ANSWERED, "", secs


def _probe_now(probe):
    """Runs `probe` (a callable rendering presence_probe's triple). No probe, a probe that raises, or a triple
    whose state is not one of the three: unmeasured -- never answered."""
    if probe is None:
        return PROBE_UNMEASURED, "no test inference was given to this call", 0.0
    try:
        st, why, secs = probe()
    except Exception as e:  # noqa: BLE001
        return PROBE_UNMEASURED, f"the test inference raised {type(e).__name__}", 0.0
    if st not in (PROBE_ANSWERED, PROBE_SILENT, PROBE_UNMEASURED):
        return PROBE_UNMEASURED, f"the test inference gave no state ({st!r})", 0.0
    return st, why, secs


def serving_probe(probe, embed=None):
    """`probe` (presence_probe's triple), then one embedding of PRESENCE_PROBE_PROMPT through `embed` -- the
    function a request's answer is embedded with (modea/miner.py::answer_embedding, on the served backend). A
    request is answered and embedded before its commitment exists (modea/miner.py::Miner.handle_job), so an engine
    that answers but cannot embed serves nothing. Counting a generation alone as "the engine answered" would give
    up every request as failing on its own while the miner kept proving availability without being able to
    commit anything. An embedding that raises or comes back empty is silent. Its time is bounded by the engine's
    own embedding call, not by the probe's bound. Never raises."""
    st, why, secs = _probe_now(probe)
    if st != PROBE_ANSWERED or embed is None:
        return st, why, secs
    try:
        vec = embed(PRESENCE_PROBE_PROMPT)
    except Exception as e:  # noqa: BLE001 -- every failure is a state, never a crash of the loop
        return (PROBE_SILENT, f"the served model answered, but no embedding could be made ({type(e).__name__}): "
                              f"no answer can be committed", secs)
    if not vec:
        return PROBE_SILENT, "the served model answered, but its embedding came back empty", secs
    return st, why, secs


def prove_availability_once(mid, vsk, last_chal="", keydir=None, probe=None, kit_flags=()):
    """Best-effort: if availability is on (challenge present) and new, proves presence with a VRF proof --
    only once a test inference on the served model has just answered (`probe`, see presence_probe; without one
    nothing is sent). Never raises (must not disturb the inference loop). Returns the settled challenge --
    proven, or whose window is closed -- or last_chal. `keydir` holds the record that survives a restart
    (AVAIL_RECORD). `kit_flags` is what proof_kit_flags allows, empty everywhere but on epoch 15."""
    if not vsk:
        return last_chal
    try:
        out = query("get-avail-challenge", flags=("--output", "json"))
        # Three states, not two. A query that fails (node down, wrong --node, subcommand renamed) prints
        # text no JSON parser accepts, while a chain with availability disarmed answers an empty challenge.
        # The first needs an operator and the second needs nothing, so they are told apart first.
        try:
            answer = json.loads(out)
        except Exception:
            print(f"[daemon] {mid} availability challenge unreadable (query failed): "
                  f"{str(out).strip()[:200]}", flush=True)
            _status_write(presence={"at": int(time.time()), "result": "challenge unreadable",
                                    "why": str(out).strip()[:200]})
            return last_chal
        # proto3 omits a string at its zero value, so an absent `challenge` is the empty challenge:
        # availability is off. That default is only safe because the failing query is ruled out above.
        chal = answer.get("challenge", "")
        if not chal or chal == last_chal:
            return last_chal   # availability off, or challenge already settled by this process
        # Settled before this process (the record in the key directory): proven, or its window closed and
        # already said. Neither is sent again nor said again.
        # "proven" is trusted only while its transaction is on this chain. The record is keyed by the
        # challenge's text, and a rescue by export (same chain-id, same numbering) carries AvailChallenge
        # over but deliberately not `Available` (keeper/genesis.go): the same challenge with no proof recorded
        # for it. A proof sent twice costs one transaction (Available is a set), while a skipped proof costs
        # the epoch, so a record whose transaction this chain does not show is proven again.
        rec = _avail_record(keydir)
        if rec.get("challenge") == chal:
            if rec.get("state") == "window_closed":
                return chal
            if rec.get("state") == "proven" and _tx_on_chain(rec.get("txhash")):
                return chal
        # Read, so zero is a value (proto3 omits it): epoch 0 is the first epoch, avail_epoch_blocks 0 is off.
        try:
            epoch = int(answer.get("epoch") or 0)
            eb = int(answer.get("avail_epoch_blocks") or 0)
        except (TypeError, ValueError):
            epoch, eb = 0, 0
        dl = _avail_deadline()
        h_now = _chain_height()
        st, nxt = avail_window(epoch, eb, dl, h_now)
        if st == "stale":
            return last_chal   # the challenge read is not this height's: read again at the next tick
        if st == "closed":
            _window_closed(mid, keydir, chal, epoch, nxt,
                           f"epoch {epoch} accepts proofs up to block {epoch * eb + dl}, the chain is at {h_now}")
            return chal
        # The model answers, or no presence (see presence_probe). Right before the proof, never cached: a proof
        # says "this miner serves now". A probe that did not answer is retried after PRESENCE_PROBE_RETRY_S,
        # within the same window; the challenge stays unsettled, so the next tick asks again.
        if _PROBE_STATE["failed_at"] and time.time() - _PROBE_STATE["failed_at"] < PRESENCE_PROBE_RETRY_S:
            return last_chal
        pst, pwhy, psecs = _probe_now(probe)
        if pst != PROBE_ANSWERED:
            _PROBE_STATE["failed_at"] = time.time()
            _status_write(presence={"at": int(time.time()), "result": "model did not answer", "probe": pst,
                                    "probe_s": psecs, "challenge": chal[:12], "why": pwhy})
            if _PROBE_STATE["said_for"] != chal:
                _PROBE_STATE["said_for"] = chal
                print(f"[daemon] {mid} availability not proven for challenge {chal[:12]}...: the served model did "
                      f"not answer a test request ({pst}: {pwhy}). A miner whose model does not answer is not "
                      f"present: no proof is sent. Tried again every {int(PRESENCE_PROBE_RETRY_S)} s while the "
                      f"window is open; said once per challenge.", flush=True)
            return last_chal
        _PROBE_STATE["failed_at"] = 0.0
        # Same channel as above: the secret is handed over in the environment, not on the
        # command line that every local process can read.
        pi = subprocess.run([_vrf_bin(), "prove", chal], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "DENDRA_VRF_SK": vsk}).stdout.strip()
        if pi:
            # What counts is what the chain recorded, so the transaction is judged before anything is
            # announced. The caller stores the return value as `_last_chal`, and the guard above treats a
            # challenge equal to it as proven: a success announced without reading the answer would lose
            # the availability share for the whole window with nothing left to retry. On failure the old
            # challenge is kept, so the next tick tries again (the same judgement as `wait_tx` for
            # create-miner and create-commit).
            out = tx_from(mid, "prove-availability", mid, chal, pi, flags=tuple(kit_flags))
            # The heartbeat records "proven" only with the height `wait_tx` read, so the file never says
            # more than the chain answered.
            h = wait_tx_height(out)
            if not h:
                why = _tx_err(out)
                # The chain's own "too late" (ProveAvailability, see AVAIL_RECORD): reached when the height or
                # the deadline could not be read before sending. The window is closed for this challenge:
                # retrying it every tick only repeats the refusal until the next epoch.
                if "proof too late" in why:
                    _window_closed(mid, keydir, chal, epoch, nxt if eb > 0 else None, f"the chain said: {why}")
                    return chal
                # The epoch 15 floor on the kit: this process's kit number cannot change while it runs, so the
                # challenge is settled here instead of retried every tick. The record does not skip it after a
                # restart, which is when an updated kit runs.
                if "below min_kit_version" in why:
                    _avail_remember(keydir, challenge=chal, state="kit_below_floor", epoch=epoch)
                    _status_write(presence={"at": int(time.time()), "result": "kit below the chain's floor",
                                            "challenge": chal[:12], "why": why})
                    print(f"[daemon] {mid} availability proof refused for challenge {chal[:12]}... ({why}): this "
                          f"kit is below the chain's min_kit_version. Update the kit; this process sends no proof "
                          f"for this challenge again.", flush=True)
                    return chal
                _status_write(presence={"at": int(time.time()), "result": "refused",
                                        "challenge": chal[:12], "why": why})
                print(f"[daemon] {mid} availability proof refused for challenge {chal[:12]}... "
                      f"({why}) -> not marked proven, will retry")
                return last_chal
            _th = re.search(r'txhash:\s*"?([A-Fa-f0-9]{64})', out or "")
            _avail_remember(keydir, challenge=chal, state="proven", epoch=epoch, height=h,
                            txhash=_th.group(1) if _th else "")
            _status_write(presence={"at": int(time.time()), "result": "proven", "height": h,
                                    "challenge": chal[:12], "probe_s": psecs})
            print(f"[daemon] {mid} availability PROVEN (VRF) for challenge {chal[:12]}... (the served model "
                  f"answered a test request in {psecs} s)")
            return chal
        _status_write(presence={"at": int(time.time()), "result": "no proof produced",
                                "challenge": chal[:12], "why": "the VRF tool returned no proof"})
    except Exception as e:
        _status_write(presence={"at": int(time.time()), "result": "skipped", "why": type(e).__name__})
        print(f"[daemon] {mid} availability proof skipped ({type(e).__name__})")
    return last_chal


def _stop_on_keyring(e) -> None:
    """Says why the keys cannot be opened, records it in the heartbeat, and exits (4). The container's
    restart policy brings the daemon back, which reads the keyring again: once the cause is fixed, it
    starts; until then, it says the same thing and creates nothing."""
    hint = getattr(e, "hint", "")
    print(f"[daemon] KEYS NOT OPENED -- {e}\n"
          f"         The miner stops here and creates no new key: a new key would be a new identity, with no\n"
          f"         funds and no registration, while the stake stays on chain under the one it cannot open.\n"
          + (f"         {hint}\n" if hint else ""), flush=True)
    _status_write(phase="keys not opened", keys_error=str(e)[:300])
    sys.exit(4)


# -- Owner mode (deploy/join.sh --owner) -------------------------------------------------------------
# The miner is registered by its owner, a key that is not on this machine: the identifier derives from the
# owner's address, the bond is the owner's, and only the owner can update or delete the miner and get the
# bond back. This machine's key operates it -- commits, availability proofs, subsidy claims, key rotations --
# and is the one the chain pays (chain/x/jobs/keeper/owner_operator_split_test.go fixes both halves).
# What it protects: the capital. A copy of this machine's key still takes the income (payments and subsidy
# go to the operator) and can get the bond slashed by bad commits; it can no longer withdraw the bond.
# The daemon never sends create-miner in this mode: it prepares it, prints the three commands that register
# the miner (modea/owner_tx.py), and mines only once the chain records this machine's key as the operator.
OWNER = os.environ.get("DENDRA_MINER_OWNER", "").strip()
OWNER_POLL_S = float(os.environ.get("DENDRA_OWNER_POLL_S", "30"))
OWNER_REPRINT_S = float(os.environ.get("DENDRA_OWNER_REPRINT_S", "1800"))
OWNER_TX_FILE = {"create-miner": "owner-create-miner.json", "update-miner": "owner-update-miner.json"}


class OwnerModeError(RuntimeError):
    """Owner mode cannot run as configured. Said, recorded, and the daemon stops (exit 5) before renaming,
    creating or registering anything: a wrong owner is another identity."""


def _stop_on_owner(e) -> None:
    print(f"[daemon] OWNER MODE NOT STARTED -- {e}", flush=True)
    _status_write(phase="owner mode not started", owner_error=str(e)[:300])
    sys.exit(5)


def decide_owner(mid, addr, configured=None, read=None) -> str:
    """The owner this miner runs under ("" = it registers itself), or OwnerModeError.

    `mid` is the identity this start resumed (a `dm1...` read from the volume) or the name passed in, `addr`
    this machine's key. The registry decides when the volume and the setting disagree, because a miner that
    exists on chain is a fact and a setting is a wish:
      · DENDRA_MINER_OWNER set, and the volume already holds another identity that the chain registers: the
        owner would be a new identity -- refused, with the way out (leave, or drop the setting);
      · DENDRA_MINER_OWNER missing, and the volume's identity is registered on chain by another key with this
        machine's key as operator: that is an owner-mode miner whose setting was lost -- kept, and said;
      · DENDRA_MINER_OWNER missing, and the volume's identity (not derived from this machine's key) is not
        registered: refused, since renaming it would register the owner's identity with this machine's key;
      · whenever the registry cannot be read in either case, nothing is decided: an unknown never renames.
    """
    from final_season_address import payable_address
    from modea.miner_id import miner_id_for_account
    configured = (OWNER if configured is None else configured).strip()
    read = read or registry_record
    if configured:
        bad = payable_address(configured)
        if bad:
            raise OwnerModeError(f"DENDRA_MINER_OWNER={configured} is not an owner address: {bad}. A wrong owner "
                                 f"is another identity; nothing was renamed, created or registered.")
        configured = configured.lower()
        if configured == str(addr).lower():
            print(f"[daemon] DENDRA_MINER_OWNER names this machine's own key ({addr}): there is nothing to "
                  f"separate, and this miner registers itself as it does without it.", flush=True)
            return ""
        want = miner_id_for_account(configured)
        if not str(mid).startswith("dm1") or mid == want:
            return configured
        state, rec = read(mid)
        if state == UNREAD:
            raise OwnerModeError(f"this volume's identity {mid} is not the one {configured} registers ({want}), "
                                 f"and the chain cannot be read to say whether {mid} is registered "
                                 f"({rec.get('why')}). Nothing is renamed while that is unknown.")
        if state == PRESENT:
            raise OwnerModeError(
                f"this volume's miner {mid} is registered (owner {rec['creator'] or '?'}, operator "
                f"{rec['operator'] or '?'}), and DENDRA_MINER_OWNER={configured} is another identity ({want}). "
                f"Owner mode starts a new identity: leave with deploy/testnet-miner/exit-miner.sh (the stake goes "
                f"back to {rec['creator'] or 'its owner'}) and join again with --owner from a new miner-keys "
                f"volume -- or remove DENDRA_MINER_OWNER from deploy/testnet-miner/.env to keep this miner.")
        return configured
    try:
        own = miner_id_for_account(addr)
    except Exception:  # noqa: BLE001 -- an address that does not decode: align_identity says so, and keeps the name
        own = None
    if str(mid).startswith("dm1") and own is not None and mid != own:
        state, rec = read(mid)
        if state == UNREAD:
            raise OwnerModeError(f"this volume's identity {mid} is not derived from this machine's key {addr}, "
                                 f"and the chain cannot be read to say who owns it ({rec.get('why')}): it may be "
                                 f"an owner-mode miner whose DENDRA_MINER_OWNER is missing. Nothing is renamed "
                                 f"while that is unknown.")
        # Absent does not mean "mine". An identity that is not derived from this machine's key and that the
        # chain does not register is, as far as this code can tell, an owner-mode miner whose owner has not
        # signed the registration yet (or has left) and whose setting was lost (join.sh run again after an
        # uninstall, a hand-edited .env). Going on would register it with this machine's key, the very key
        # owner mode keeps the stake away from. Nothing is renamed or registered; the two ways out are named.
        if state == ABSENT:
            raise OwnerModeError(
                f"this volume's identity {mid} is not derived from this machine's key {addr}, and the chain does "
                f"not register it -- the identity of an owner-mode miner its owner has not registered yet, with "
                f"DENDRA_MINER_OWNER missing from deploy/testnet-miner/.env, reads exactly like this. Nothing was "
                f"renamed, created or registered. Either write the owner back (DENDRA_MINER_OWNER=<owner address> in "
                f"deploy/testnet-miner/.env, or bash deploy/join.sh --owner <owner address>), or, to give that "
                f"identity up and let this machine's key register a miner of its own, remove the file "
                f"{kring.RESOLVED_IDENTITY} from the miner's key directory and start again.")
        cr, op = rec.get("creator", ""), rec.get("operator", "")
        if state == PRESENT and op.lower() == str(addr).lower() and cr and cr.lower() != str(addr).lower():
            print(f"[daemon] OWNER MODE (read from the chain): {mid} is owned by {cr} and operated by this "
                  f"machine's key. DENDRA_MINER_OWNER is missing from the kit's .env; this start keeps the "
                  f"identity. Write it back: DENDRA_MINER_OWNER={cr} in deploy/testnet-miner/.env, or "
                  f"bash deploy/join.sh --owner {cr}.", flush=True)
            return cr.lower()
    return ""


def _owner_block(keydir, mid, sub, prep, owner, cid, host_name, title) -> str:
    """The printed procedure for one owner transaction, complete: no placeholder to fill in."""
    signed = host_name.replace(".json", ".signed.json")
    return "\n".join([
        f"         {title}",
        f"         1. copy out the unsigned {sub} this daemon prepared (nothing was signed or sent):",
        f"              docker compose -p dendra-miner exec -T miner cat {keydir}/{OWNER_TX_FILE[sub]} > {host_name}",
        f"            (produced by: {owner_tx.shell(prep['generate'])})",
        f"         2. sign it on the machine that holds the owner key, offline (dendrad of the release binaries;",
        f"            add the --keyring-backend / --keyring-dir of that machine's keyring):",
        f"              {owner_tx.shell(owner_tx.sign_argv(host_name, owner, cid, prep['account_number'], prep['sequence'], signed))}",
        f"         3. broadcast it from this host, through the miner's container:",
        f"              docker compose -p dendra-miner exec -T miner {owner_tx.shell(owner_tx.broadcast_argv(tuple(_node())))} < {signed}",
    ])


def owner_prepare(keydir, sub, positionals, owner, mid, extra=()):
    """(ok, text): the transaction prepared (simulation, unsigned file in the volume, account read) and the
    printed procedure, or why it could not be prepared."""
    cid = _cid.chain_id(tuple(_node()))
    prep = owner_tx.prepare(sub, positionals, owner, cid, node_flags=tuple(_node()), extra=extra, miner_id=mid)
    if not prep["ok"]:
        said = f"\n         the chain said: {prep['chain_said']}" if prep.get("chain_said") else ""
        return False, f"         {prep['why']}{said}"
    crypto.write_private(os.path.join(keydir, OWNER_TX_FILE[sub]), (prep["unsigned"] + "\n").encode("utf-8"))
    host = sub + ".json"
    return True, _owner_block(keydir, mid, sub, prep, owner, cid, host,
                              f"{sub} for {mid}, signed by the owner {owner} (gas {prep['gas']}, from a simulation the "
                              f"chain accepted):")


def owner_wait(a, addr, owner, mypub, vpk, sleep=time.sleep) -> bool:
    """Owner mode: returns True once the chain records `addr` as the operator of `a.id` -- the only state in
    which this machine mines. Never sends create-miner. While the miner is absent it funds what the faucet
    can fund (this machine's key needs an account to sign anything; the owner needs the stake), prepares the
    registration and prints its three commands; while another operator is recorded it prints the owner's
    update-miner. With --once it returns False after one pass instead of waiting.

    A reading that failed (the registry, or min_stake) prepares nothing and does not move `printed_at`: the next
    pass, OWNER_POLL_S later, reads again and prepares as soon as both are read, instead of waiting a full
    OWNER_REPRINT_S. What a failed reading prints is logged at most once per OWNER_REPRINT_S (`unread_said_at`)."""
    printed_at, funded, prepared, unread_said_at = 0.0, set(), None, 0.0
    while True:
        state, rec = registry_record(a.id)
        if state == PRESENT and rec["operator"].lower() == addr.lower():
            if rec["creator"].lower() != owner.lower():
                print(f"[daemon] INFO: {a.id} is registered by {rec['creator']}, not by DENDRA_MINER_OWNER={owner}. "
                      f"The chain decides who owns it: {rec['creator']} is the key that can delete it.", flush=True)
            print(f"[daemon] {a.id} is registered by its owner {rec['creator']} and operated by this machine "
                  f"({addr}): mining.", flush=True)
            _status_write(phase="registered", owner=rec["creator"])
            return True
        due = time.time() - printed_at >= OWNER_REPRINT_S or not printed_at
        if state == PRESENT and due:
            printed_at = time.time()
            ok, text = owner_prepare(a.keydir, "update-miner", (a.id, addr, rec["region"] or "eu", "0"), owner, a.id)
            print(f"[daemon] OWNER MODE -- {a.id} is registered, but its OPERATOR is {rec['operator'] or '(none)'},\n"
                  f"         and this machine signs as {addr}. The chain refuses every commit and proof this machine\n"
                  f"         would send, so it does not mine. The owner names this machine's key the operator\n"
                  f"         (update-miner always carries the operator: an empty one silences the miner):\n{text}",
                  flush=True)
            prepared = ok
        elif state == ABSENT and due and (stake := _registration_stake()) is None:
            # min_stake not read: nothing is funded and nothing is prepared. A create-miner prepared with a stake
            # nobody read is a transaction the owner signs offline for nothing. `printed_at` stays: read again at
            # the next pass.
            if not unread_said_at or time.time() - unread_said_at >= OWNER_REPRINT_S:
                unread_said_at = time.time()
                print(f"[daemon] OWNER MODE -- the chain's min_stake could not be read: the registration of {a.id} "
                      f"is not prepared; read again in {OWNER_POLL_S:.0f} s.", flush=True)
        elif state == ABSENT and due:
            printed_at = time.time()
            # `stake` was read by the branch above (min_stake read).
            # Each address is asked for once per start: the faucet has a per-address cooldown, and a refusal
            # repeated every pass would only fill the log. A balance that could not be read (None) asks nothing:
            # it is not an empty account, and the address is asked about again at the next due pass.
            for who, need in ((addr, 1), (owner, stake)):
                held = bal_token(who)
                if who in funded or held is None or held >= need:
                    continue
                funded.add(who)
                fok, fwhy = faucet_fund(a.faucet, who)
                if not fok:
                    print(f"[daemon] faucet refused ({a.faucet}) for {who}: {fwhy}", flush=True)
                for _ in range(20):
                    held = bal_token(who)
                    if not fok or (held is not None and held >= need):
                        break
                    sleep(2)
            # The VRF key is anchored at registration, as a flag: without it the miner can never prove its
            # availability, and the omission is logged, as the self-registering path logs it.
            ok, text = owner_prepare(a.keydir, "create-miner", (a.id, addr, "eu", stake, mypub), owner, a.id,
                                     extra=("--vrf-pubkey", vpk) if vpk else ())
            if not vpk:
                text += ("\n         Warning: prepared without a VRF key (dendra-vrf not found in this image): this miner will"
                         "\n         prove no availability until a key is anchored with rotate-miner-keys.")
            print(f"[daemon] OWNER MODE -- {a.id} is registered by its owner, not by this machine.\n"
                  f"         owner    : {owner}  (pays the stake, alone updates or deletes the miner, gets the stake back)\n"
                  f"         operator : {addr}  (this machine's key: signs the commits, proofs and claims, and is paid)\n"
                  f"         This machine does not hold the owner key, so it does not send create-miner.\n{text}\n"
                  f"         This miner waits: it mines once the chain records {addr} as the operator of {a.id}.",
                  flush=True)
            if bal_token(addr) == 0:
                print(f"[daemon] this machine's key {addr} has no funds, so no account on chain: it cannot sign a "
                      f"commit until it receives some (any amount) -- the faucet above, or a transfer.", flush=True)
            prepared = ok
        elif state == UNREAD and (not unread_said_at or time.time() - unread_said_at >= OWNER_REPRINT_S):
            # Not read: nothing is prepared, and `printed_at` stays -- read again at the next pass.
            unread_said_at = time.time()
            print(f"[daemon] OWNER MODE -- the registration of {a.id} could not be read ({rec.get('why')}); "
                  f"read again in {OWNER_POLL_S:.0f} s.", flush=True)
        # The heartbeat moves on every pass: a miner waiting for its owner is waiting, not stuck, and the
        # container's healthcheck reads only the age of this file.
        _status_write(phase={PRESENT: "owner mode: operator mismatch", ABSENT: "awaiting the owner's registration",
                             UNREAD: "owner mode: registry unread"}[state],
                      owner=owner, operator=rec.get("operator", ""), prepared=prepared)
        if a.once:
            return False
        sleep(OWNER_POLL_S)


def _registration_stake():
    """The stake a registration carries: DENDRA_MINER_STAKE when set and not below the chain's min_stake,
    else min_stake read from the chain (a governed parameter). None when min_stake was not read: no stake is
    then chosen at all, and the caller sends nothing."""
    _min = chain_min_stake()
    if _min is None:
        return None
    try:
        want = int(os.environ.get("DENDRA_MINER_STAKE") or _min)
    except ValueError:
        want = _min
    if want < _min:
        print(f"[daemon] WARNING: DENDRA_MINER_STAKE={want} < the chain's min_stake ({_min}) -> create-miner "
              f"would be rejected. Using {_min}.", flush=True)
        want = _min
    return want


PAYOUT_ADDRESS = os.environ.get("DENDRA_PAYOUT_ADDRESS", "").strip()
PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "").strip().rstrip("/")


def maybe_declare_payout(keydir, mid, operator="", owner="") -> tuple:
    """(settled, message). Declares DENDRA_PAYOUT_ADDRESS to the Final Testnet Season programme once this
    miner is registered, and again only when that address changes: the record of what was declared, and
    from which setting, lives in the miner's volume (modea.keyring.PAYOUT_RECORD), next to the identity
    it is about. A declaration made since from the Dendra application is not overwritten while the
    setting stays the same. `settled` is True when nothing is left to do.

    In owner mode the programme accepts the declaration from the owner's key only, which is not here: the
    daemon does not sign it, it prints the three steps (final_season_miner.py payout-prepare, the owner's
    signature, payout-submit) once per start, until the record shows the setting declared."""
    addr = PAYOUT_ADDRESS
    if not addr:
        return True, ""
    import final_season_miner as fsm
    from final_season_address import payable_address
    bad = payable_address(addr)
    if bad:
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is not declared: {bad}. The season pays this machine's own "
                      f"key until a payable address is declared (deploy/join.sh --payout-address).")
    if not PROGRAMME:
        return True, ("DENDRA_PAYOUT_ADDRESS is set but DENDRA_FINAL_SEASON_URL is empty: there is no programme "
                      "to declare it to.")
    rec = kring.payout_record(keydir) or {}
    # DENDRA_PAYOUT_LOCK=1 (set by docker/cloud-start.sh on a rented pod): the declaration asks the programme to
    # lock the address, so a host that reads this machine's key cannot redirect the rewards afterwards.
    lock = os.environ.get("DENDRA_PAYOUT_LOCK", "") == "1"
    if rec.get("miner_id") == mid and str(rec.get("setting") or "").lower() == addr.lower():
        # With the lock asked for, only a record of this address that the programme answered locked is settled.
        # A record without it -- an earlier declaration made without the lock, by a programme that does not
        # lock, or on a volume that comes from another machine -- is declared again, with the lock. A programme
        # that locks only an identity's first declaration refuses it (409, "lock_refused"), said below.
        if not lock or (rec.get("locked") is True and str(rec.get("address") or "").lower() == addr.lower()):
            return True, ""
    if owner and owner.lower() != str(operator).lower():
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is not declared yet, and this machine cannot declare it: "
                      f"{mid} is owned by {owner}, and the programme accepts a declaration from the owner's key only.\n"
                      f"         1. docker compose -p dendra-miner exec -T miner python3 final_season_miner.py "
                      f"payout-prepare --address {addr} > payout.json\n"
                      f"         2. on the owner's machine, the dendrad tx sign command payout-prepare prints\n"
                      f"         3. docker compose -p dendra-miner exec -T miner python3 final_season_miner.py "
                      f"payout-submit < payout.signed.json\n"
                      f"         Until then the season pays this machine's key, the miner's operator.")
    if lock and operator and addr.lower() == str(operator).lower():
        # The lock would hold, for good, the address of the very key a host can read: nothing is sent.
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is not declared and not locked: it is this machine's own key "
                      f"(the operator of {mid}), and DENDRA_PAYOUT_LOCK=1 would hold that address for good, where a "
                      f"copy of this machine's keys can spend it. Set DENDRA_PAYOUT_ADDRESS to the public address of a "
                      f"key that is not on this machine, then restart.")
    if operator and addr.lower() == operator.lower():
        note = " (this is the machine's own key: the declaration changes nothing about who holds it)"
    else:
        note = ""
    # Without the lock the call keeps its earlier shape, word for word.
    code, r = fsm.declare_payout(PROGRAMME, mid, addr, lock=True) if lock else fsm.declare_payout(PROGRAMME, mid, addr)
    if code == 200:
        locked = (r or {}).get("locked") is True
        # The record says what the programme answered: an accepted declaration without "locked": true is
        # recorded without it, never as locked on the strength of the request.
        fsm.record_declaration(keydir, mid, addr, source="daemon", setting=addr, locked=locked)
        if lock and not locked:
            # Asked for the lock and answered without it: a programme that does not lock (one deployed before
            # the lock existed). The address is filed, and a copy of this machine's key could still change it,
            # so this is not settled: it is logged, and the declaration is made again, with the lock.
            return False, (f"payout declaration of {addr} for {mid} accepted without the lock it asked for (the "
                           f"programme's answer carries no \"locked\": true): a copy of this machine's key could still "
                           f"change the address. Declared again with the lock in 30 minutes (a programme that locks "
                           f"only an identity's first declaration will refuse it, this one being filed).")
        return True, f"Final Testnet Season rewards of {mid} declared to {addr}{' and locked' if locked else ''}{note}."
    if code == 409:
        # Final in both of its forms: retrying every half hour would change nothing, so it is logged once and not
        # retried. The answer's own field says which form it is (parsed, never matched as text).
        if (r or {}).get("lock_refused") is True:
            # The lock is granted only on an identity's first declaration (or from its owner's key), and this
            # identity has declared before without it. Nothing was filed: the declaration before stands. The
            # refusal is recorded, as the programme answered it: the payout record says nothing (nothing was
            # filed), and a pod's watch (docker/cloud-start.sh) that read only that record would wait for good.
            fsm.record_lock_refusal(keydir, mid, addr)
            return True, (f"payout declaration of {addr} for {mid} refused for good with the lock: the programme locks "
                          f"an address only on an identity's first declaration, and {mid} has declared before without "
                          f"the lock. Nothing was filed: the season pays the address declared before (this machine's "
                          f"key if none), and a copy of this machine's key can still change it. Only a new identity, "
                          f"whose first declaration carries the lock, is paid at a locked address "
                          f"({str((r or {}).get('error') or r)[:200]}).")
        # The programme holds a locked address for this identity and refuses any other.
        return True, (f"payout declaration of {addr} refused for good: the programme holds a locked payout address "
                      f"for {mid} and refuses any other ({str((r or {}).get('error') or r)[:200]}). The season pays "
                      f"the locked address.")
    return False, (f"payout declaration of {addr} not accepted yet ({code or 'no answer'}: "
                   f"{str((r or {}).get('error') or r)[:200]}); retried in 30 minutes.")


# -- The registration, as an attempt that is replayed -------------------------------------------------
# A single attempt at start would leave a miner unregistered for good whenever the faucet refused (its daily
# quota per IP is reached fast when several identities share one address: one identity per card, a rented box
# behind a shared IP). So the attempt is a function the loop replays, guarded by registry_record, which has
# three states. stake_of is not used: it reads -1 both when the query fails and when the stake is omitted
# (proto3, a miner slashed to zero), and would send the faucet and a create-miner for a miner that exists.
#   PRESENT -> nothing to do (even at stake 0: that miner exists, and create-miner would be refused);
#   ABSENT  -> the faucet, then create-miner, as at start before;
#   UNREAD  -> nothing: a node that does not answer is never "not registered". Read again later.
# The state goes into the heartbeat (`registration`), which deploy/join.sh reads to stop starting the next
# identities of a rig at the first IP-quota refusal, and which miner_selftest.py and the rig tools show.
REGISTERED, DEFERRED, REFUSED = "registered", "deferred", "refused"
# Chosen, not measured: each attempt costs a proof of work (bounded by DENDRA_FAUCET_POW_MAX_S) and one
# request, and the faucet records only a drip it paid (faucet._rate_ok), so a refused attempt spends
# no quota. Ten minutes keeps that cost small and still registers within the hour a quota frees up.
# An empty value is unset (deploy/testnet-miner/docker-compose.yml forwards it empty when the .env names none).
REGISTER_RETRY_S = float(os.environ.get("DENDRA_REGISTER_RETRY_S", "").strip() or "600")
# An address in cooldown is refused until the faucet's own cooldown (DENDRA_FAUCET_ADDR_COOLDOWN, on the
# faucet's side, not readable here) has passed: this daemon waits its cadence times this factor instead of
# asking every ten minutes for a drip it cannot get. A chosen factor, written as one.
ADDR_COOLDOWN_RETRY_FACTOR = 6
# The one human line a deferred registration prints. Neither "create-miner" nor "ready" may appear in what a
# not-registered miner prints: deploy/join.sh::wait_healthy reads those words as a registration.
REGISTRATION_DEFERRED_LINE = "[daemon] REGISTRATION DEFERRED (faucet: {reason})"
_VRF_OMISSION_SAID = False


def registration_retry_delay(state, reason) -> float:
    return REGISTER_RETRY_S * ADDR_COOLDOWN_RETRY_FACTOR if (state, reason) == (DEFERRED, "addr_cooldown") \
        else REGISTER_RETRY_S


def not_registered_line(mid, reason, delay_s) -> str:
    if reason == "draining":
        return (f"[daemon] {mid} is not registered, and it is draining (the file {DRAIN_FILE} in its key "
                f"directory): no registration is attempted while that file is there. Removing it registers this "
                f"miner again at the next pass. It receives no job until then.")
    return (f"[daemon] {mid} is not registered yet ({reason or 'no reason given'}); next attempt in "
            f"{max(0, int(delay_s))} s. It receives no job until then.")


def identity_mismatch_text(mid, operator, addr, slot, creator="", region="") -> str:
    """The identity mismatch notice: the chain records another key as the operator of this identity.

    A new identifier is not a way out. The chain derives the identifier from the key that registers it
    (align_identity), and main() resumes `resolved-identity` for any name that is not a `dm1...`, so
    `join.sh --id <new-id>` (slot 0) or MINER_ID=<new-id> in a slot's env (slot k) gives back the same
    identifier, the same key and the same mismatch. The two ways that change something are on the chain
    (the creator names this key the operator with update-miner, which keeps the bond) or in the keyring
    (the operator's own key restored). `slot` (DENDRA_SLOT as compose hands it, 0 when the kit
    runs one identity) names which keyring: one volume per card (deploy/join.sh --gpus). A value that is
    not a slot number names no volume, and the notice says so rather than picking one."""
    s = str(slot if slot is not None else "0").strip()
    if s == "0":
        where = "this kit's keys volume (dendra-miner_miner-keys)"
    elif s.isdigit() and not s.startswith("0"):
        where = f"the keys volume of card slot {s} (dendra-miner-g{s}_miner-keys)"
    else:
        where = (f"this identity's keys volume (its slot is not known here, DENDRA_SLOT={s!r}: "
                 f"bash deploy/testnet-miner/slots.sh list names each card's identity)")
    who = creator or f"its creator, as dendrad query jobs get-miner {mid} names it"
    mine = (f"\n             That creator is this machine's key: this machine can sign it."
            if creator and creator.lower() == str(addr).lower() else "")
    return (f"[daemon] IDENTITY MISMATCH: '{mid}' is registered on-chain for the operator\n"
            f"         {operator}\n"
            f"         but this machine signs with\n"
            f"         {addr}\n"
            f"         -> the chain will refuse every commit (unauthorized). Two ways out:\n"
            f"         (a) the key that registered {mid} ({who}) names this machine's key the operator:\n"
            f"             dendrad tx jobs update-miner {mid} {addr} {region or '<its region>'} 0 --from <that key>\n"
            f"             (a stake of 0 leaves the bond as it is){mine}\n"
            f"         (b) restore the original key of {operator} into {where}.\n"
            f"         A new identifier is not a way out: the chain derives the identifier from the key, so the\n"
            f"         key in {where} keeps '{mid}'.")


def check_operator(mid, addr, slot, said=None):
    """The operator the chain records for `mid`, compared with this machine's key -> "mismatch" | "ok", or
    `said` unchanged when nothing was read (a registry that does not answer, or no record yet).

    The state goes into the heartbeat, not only into the log. The notice is printed once, at start, the kit
    rotates the container logs (deploy/testnet-miner/docker-compose.yml, `logging:`), and the registration in
    the heartbeat says "registered" whatever the operator (register_attempt reads PRESENT); without this field
    a long-lived container could look healthy to deploy/join.sh::wait_healthy. It is written as
    `identity_mismatch` (null once the chain records this key), read by slots.sh::slot_registration, and
    re-read by the loop: a mismatch fixed on chain (update-miner) clears without a restart, and the change is logged."""
    op = miner_operator(mid)
    if op and op.lower() != str(addr).lower():
        _status_write(identity_mismatch={"operator": op, "address": addr})
        if said != "mismatch":
            _st, _rec = registry_record(mid)
            _rec = _rec if _st == PRESENT else {}
            print(identity_mismatch_text(mid, op, addr, slot, creator=_rec.get("creator", ""),
                                         region=_rec.get("region", "")), flush=True)
        return "mismatch"
    if op:
        _status_write(identity_mismatch=None)
        if said == "mismatch":
            print(f"[daemon] {mid}: the chain records this machine's key ({addr}) as its operator again; its "
                  f"commits are accepted.", flush=True)
        return "ok"
    return said


def register_attempt(a, addr, mypub, vpk):
    """One registration attempt. Returns (state, reason): registered | deferred (reason: the faucet's word,
    see faucet_reason) | refused (the chain did not confirm create-miner) | unread (the registry could not be
    read: nothing was sent). Self-registering miners only; owner mode never calls it."""
    global _VRF_OMISSION_SAID
    state, rec = registry_record(a.id)
    if state == PRESENT:
        return REGISTERED, ""
    if state == UNREAD:
        return UNREAD, "registry_unread"
    # The stake is read before anything is asked of anyone. If min_stake is not read nothing is sent, the
    # faucet included: a registration with a stake nobody read is refused, and a drip asked for it is wasted.
    _min = chain_min_stake()
    _stake_n = _registration_stake() if _min is not None else None
    if _min is None or _stake_n is None:
        print(f"[daemon] {a.id}: the chain's min_stake could not be read, so no registration was attempted and "
              f"the faucet was not asked; tried again at the next attempt.", flush=True)
        return UNREAD, "params_unread"
    # A faucet refusal is logged: without funds `create-miner` is rejected and the miner runs outside the
    # registry, and the diagnostic must name the faucet rather than suggest a chain problem.
    _fok, _fwhy, _freason = faucet_fund_classified(a.faucet, addr)
    if not _fok:
        print(f"[daemon] faucet refused ({a.faucet}) for {addr}: {_fwhy}\n"
              f"         Without funds the on-chain registration is rejected and this miner will "
              f"receive no job. Check the faucet URL, or ask the operator for funding.",
              flush=True)
        print(REGISTRATION_DEFERRED_LINE.format(reason=_freason), flush=True)
    else:
        # Wait for the credit only if the request was accepted: after a refusal the balance will not move.
        for _ in range(20):
            if (bal_token(addr) or 0) > 0:     # not read (None) is not a credit: keep waiting
                break
            time.sleep(2)
    _stake = str(_stake_n)
    _out = ""
    for _ in range(3):
        if registry_record(a.id)[0] == PRESENT:
            break
        # The X25519 public key is anchored on chain (5th argument), signed by the miner's Cosmos key, so
        # the client encrypts to this key and a relay cannot substitute its own.
        reg = ["create-miner", a.id, addr, "eu", _stake, mypub]
        reg_flags = []
        if vpk:
            reg_flags += ["--vrf-pubkey", vpk]   # vrf_pubkey as a flag (not a 2nd optional positional)
        elif not _VRF_OMISSION_SAID:
            _VRF_OMISSION_SAID = True
            # The omission is logged, with what it costs: vrf_pubkey is anchored at registration, and without
            # it the miner serves jobs normally but can never prove availability. The command is printed
            # complete, with this miner's id and key path filled in, since a message with placeholders
            # gets pasted as is. The secret never appears on that command line: `modea.keyring vrf-pubkey`
            # computes the public key from the key file, because an operator runs the command as printed
            # and argv is visible in `ps` and /proc/<pid>/cmdline.
            print(f"[daemon] registering without a VRF key (dendra-vrf not found in this image).\n"
                  f"         Under verification_mode=1 the chain refuses availability proofs from a\n"
                  f"         miner with no anchored vrf_pubkey and pays it no availability\n"
                  f"         share. Inference, commits and payouts are unaffected.\n"
                  f"         Fix: rebuild the miner image so it ships dendra-vrf, restart, then anchor\n"
                  f"         the key -- re-registration will not do it, this id being already in the\n"
                  f"         registry:\n"
                  f"           dendrad tx jobs rotate-miner-keys {a.id} "
                  f"--new-vrf-pubkey $(python3 -m modea.keyring vrf-pubkey {a.keydir}/{a.id}.vrf)\n"
                  f"         (run inside the miner container; the key file may be encrypted, and the module\n"
                  f"         opens it with the keyring's passphrase)", flush=True)
        _out = wait_tx(tx_from(a.id, *reg, flags=reg_flags)) or ""
        time.sleep(2)
    _after, _ = registry_record(a.id)
    if _after == PRESENT:
        return REGISTERED, ""
    # The failure is logged: a miner absent from the registry receives no job, and without this line
    # nothing would tell the operator why.
    _bal = bal_token(addr)
    print(f"[daemon] ON-CHAIN REGISTRATION FAILED (stake={_stake}, min_stake={_min}, "
          f"balance={'?' if _bal is None else _bal}). This miner will receive no job until it is in the registry.")
    if _out:
        print(f"[daemon]   last response from the chain: {str(_out)[:400]}")
    if not _fok:
        return DEFERRED, _freason
    if _after == UNREAD:
        return UNREAD, "registry_unread"
    return REFUSED, "chain_refused"


def registration_step(a, addr, mypub, vpk, now=None, draining=False):
    """register_attempt, its heartbeat, and when the next one is due. Returns (state, reason, retry_at).

    A draining miner registers nothing, at start as in the loop. A restart (the process ending and
    `restart: unless-stopped` bringing it back, a reboot, the hourly run of join.sh recreating the container)
    must not ask the faucet and send create-miner again once the exit has taken the miner out of the
    registry; deploy/testnet-miner/exit-miner.sh keeps the drain on a running miner for that reason.
    `draining`: the registry is still read (a draining miner the chain still records stays registered and
    finishes what it committed to), and an absent one is deferred with the reason `draining` (no faucet, no
    transaction) and retried as soon as the drain is gone."""
    if draining:
        st, _rec = registry_record(a.id)
        if st == PRESENT:
            st, why = REGISTERED, ""
        elif st == UNREAD:
            st, why = UNREAD, "registry_unread"
        else:
            st, why = DEFERRED, "draining"
    else:
        st, why = register_attempt(a, addr, mypub, vpk)
    now = time.time() if now is None else now
    retry_at = 0 if st == REGISTERED or why == "draining" else int(now + registration_retry_delay(st, why))
    _status_write(registration={"state": st, "reason": why, "retry_at": retry_at})
    return st, why, retry_at


def registration_allows_proof(state, why) -> bool:
    """May an availability proof be attempted, given the latest registration attempt (state, why)?
      * registered: yes;
      * Known not registered -- deferred, refused, or min_stake unread after the registry answered ABSENT
        (`params_unread`): no. The chain refuses a proof from a miner it does not record, and every refused
        proof costs a test inference and a transaction, once per AVAIL_CHECK_S;
      * the registry not read (`registry_unread`): yes. An unknown is neither state, and the chain judges the
        proof. Treated as "not registered", it would silence a registered miner whose node answered late at
        start until the next attempt, REGISTER_RETRY_S later, long enough to miss an availability window. The
        cost the other way is bounded by that same attempt: a miner that turns out absent has spent, until
        then, the refused proofs described above."""
    return state == REGISTERED or (state == UNREAD and why == "registry_unread")


def vrf_key_notice(mid, vpk, onchain) -> str:
    """What to say about the VRF key at start, from the key this node holds (`vpk`, "" when none) and the one the
    chain anchors (`onchain`: miner_vrf_onchain, None when it was not read). "" when there is nothing to say --
    and always when nothing was read: an unread key is compared again later, never announced missing."""
    if onchain is None:
        return ""
    if not onchain:
        if vpk:
            # This node has the key, the chain does not: one step is missing, printed complete.
            return (f"[daemon] NO VRF KEY anchored on chain for {mid}, though this node holds one.\n"
                    f"         Availability proofs are refused under verification_mode=1 until it is\n"
                    f"         anchored, and re-registration will not do it. One command:\n"
                    f"           dendrad tx jobs rotate-miner-keys {mid} --new-vrf-pubkey {vpk}")
        return (f"[daemon] NO VRF KEY, on-chain or locally (dendra-vrf not found in this image).\n"
                f"         This miner serves jobs normally but can never prove availability, and is\n"
                f"         paid no availability share. Rebuild the image so it ships dendra-vrf, restart,\n"
                f"         then anchor the key this node will generate.")
    if vpk and onchain.strip().lower() != vpk.strip().lower():
        # The fourth combination, and the only one that looks healthy: both keys exist, so neither branch
        # above fires and nothing complains at startup. The proof is produced with the key this node holds
        # and verified against the anchored one, so every availability transaction is rejected while the
        # two differ. It is reported at start because the remedy is one command, and every epoch without it
        # costs the availability share.
        return (f"[daemon] VRF key mismatch for {mid}: the chain has one key anchored, this node holds\n"
                f"         another. Availability proofs are produced with the local key and verified\n"
                f"         against the anchored one, so every one of them is refused. One command:\n"
                f"           dendrad tx jobs rotate-miner-keys {mid} --new-vrf-pubkey {vpk}")
    return ""


# -- The encryption key this node decrypts with, compared with the one the chain anchors -------------------
# A client seals every request to the encryption key anchored on chain (client.submit_job, strict by
# default), never to the copy this node deposits at the relay. A node whose key file was regenerated (a
# passphrase lost, the account restored from its recovery phrase, the sealed key file then removed) holds a new
# key without knowing it: it proves its presence (a test inference decrypts nothing), is drawn, and fails every
# request it is handed. So a key that differs from the anchored one is reported with the command that anchors this
# node's key, kept in the heartbeat (`enc_key`), and no availability proof is sent while the two differ: a miner
# that can open nothing contributes nothing by being present. The rotation is never made here, since a wrong
# volume mounted on this node would then replace the right key on chain, and only its owner can tell which is
# right.
ENC_OK, ENC_MISMATCH, ENC_UNANCHORED = "ok", "mismatch", "unanchored"


def _keyring_flags_text() -> str:
    try:
        return " ".join(_kr())
    except Exception:  # noqa: BLE001 -- a printed hint: without its flags rather than not at all
        return ""


def enc_key_text(mid, state, anchored, local) -> str:
    rotate = (f"dendrad tx jobs rotate-miner-keys {mid} --new-enc-pubkey {local} --from {mid} "
              f"{_keyring_flags_text()}").rstrip()
    if state == ENC_MISMATCH:
        return (f"[daemon] encryption key mismatch for {mid}: the chain anchors the encryption key\n"
                f"         {anchored}\n"
                f"         and this node decrypts with\n"
                f"         {local}\n"
                f"         Every request is sealed to the anchored key, so this node can open none of them, and no\n"
                f"         availability proof is sent while the two differ. Two ways out, never taken for you:\n"
                f"         (a) the anchored key's file still exists (another volume, a backup): put it back as\n"
                f"             {mid}.sk in this node's key directory and restart;\n"
                f"         (b) it is lost for good: anchor this node's key, from inside the miner container --\n"
                f"             {rotate}")
    return (f"[daemon] no encryption key anchored on chain for {mid}: a client that requires the anchored key\n"
            f"         (the default) seals no request to this miner. One command anchors this node's key, from\n"
            f"         inside the miner container --\n"
            f"           {rotate}")


def check_enc_key(mid, mypub, said=None):
    """ENC_OK | ENC_MISMATCH | ENC_UNANCHORED from the registry record of `mid` (three states: a record read
    decides, an empty `enc_pubkey` in it is the zero value of a string -- no key anchored), or `said` unchanged when
    no record was read or the chain holds none yet: an unread key is never a mismatch, and never a match either.
    Said when the state changes, and written to the heartbeat (`enc_key`) at every reading."""
    st, rec = registry_record(mid)
    if st != PRESENT:
        return said
    anchored = str(rec.get("enc_pubkey", "") or "").strip().lower()
    local = str(mypub or "").strip().lower()
    state = ENC_UNANCHORED if not anchored else (ENC_OK if anchored == local else ENC_MISMATCH)
    _status_write(enc_key={"state": state, "anchored": anchored, "local": local})
    if state != said:
        if state in (ENC_MISMATCH, ENC_UNANCHORED):
            print(enc_key_text(mid, state, anchored, local), flush=True)
        elif said == ENC_MISMATCH:
            print(f"[daemon] {mid}: the chain anchors this node's encryption key again; availability proofs "
                  f"resume.", flush=True)
    return state


# -- Draining: a miner on its way out -------------------------------------------------------------------
# A file named DRAIN_FILE in the key directory (next to AVAIL_RECORD) puts the daemon in drain: it proves no more
# presence and takes no new request, so the chain stops drawing it, while it still finishes what it has
# committed to: a sealed response the relay refused is deposited again, a journalled commitment is anchored. The
# reveal worker, a process of its own, keeps answering the audits it sits on. The heartbeat carries
# `draining`. Removing the file resumes everything at the next pass. Nothing here creates the file.
DRAIN_FILE = "drain"


def drain_state(keydir, prev=False) -> bool:
    """True when DRAIN_FILE exists in `keydir` (a dangling link counts: its name is the signal), False when it does
    not, `prev` when that could not be told (an error other than "not found"): an unknown keeps the state it had."""
    try:
        os.lstat(os.path.join(str(keydir), DRAIN_FILE))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return prev


# -- The engine's version, in the heartbeat -------------------------------------------------------------
# Two engines of different versions may embed the same text differently and run the same model at different
# concurrency, so the version is what explains a divergence between two machines.
def engine_version(backend_name, backend_obj, timeout_s=10.0) -> dict:
    """{"engine": "ollama", "version": "<as Ollama names it>"} from GET <endpoint>/api/version; with "version"
    None and a `why` when it was not read; {"engine": <name>, "version": None, "why": ...} for any other backend.
    Never raises."""
    if backend_name != "ollama":
        return {"engine": str(backend_name), "version": None, "why": "not an Ollama engine"}
    ep = str(getattr(backend_obj, "endpoint", "") or "").rstrip("/")
    if not ep:
        return {"engine": "ollama", "version": None, "why": "the engine's address is not known"}
    try:
        with urllib.request.urlopen(ep + "/api/version", timeout=timeout_s) as r:
            d = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 -- not read, and said as such
        return {"engine": "ollama", "version": None, "why": f"not read ({type(e).__name__})"}
    v = d.get("version") if isinstance(d, dict) else None
    if not isinstance(v, str) or not v.strip():
        return {"engine": "ollama", "version": None, "why": "the answer names no version"}
    return {"engine": "ollama", "version": v.strip()[:64]}


# -- Was the answer in time for its client? ---------------------------------------------------------------
# A client waits a bounded time for an answer and pays nothing for one that comes after it stopped waiting. The
# generation time (`gen_s`) is compared with the wait the client declares in its request (`wait_s`, next to
# `max_out`), never with a bound written here: the wait belongs to the client, and a constant copied into the
# miner would be wrong at the client's next change. A request that declares no wait gets no verdict: it is
# counted as undeclared, never judged against a default.
def declared_wait_s(req):
    """The wait, in seconds, the request declares (a positive finite number in `wait_s`), or None."""
    v = req.get("wait_s") if isinstance(req, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v > 0 and math.isfinite(v) else None


def answer_timing(gen_s, wait_s):
    """"late" when the generation alone took longer than the declared wait, "on_time" otherwise, None without a
    declared wait. The generation is a lower bound of what the client waited: a "late" is certain, an "on_time"
    is not a promise that the client was still waiting."""
    if wait_s is None or gen_s is None:
        return None
    return "late" if float(gen_s) > float(wait_s) else "on_time"


# -- The availability proof comes first -------------------------------------------------------------------
# A busy miner must not prove its presence at the edge of the window: a missed window counts against it in
# the draws that read presence (chain/x/jobs/keeper/presence.go::minerPresentAt). So the challenge is read
# at start, at the head of every pass and between two requests, at most every AVAIL_CHECK_S seconds (a clock,
# not a count of passes), and a new challenge whose window is open is proven before the next request is served.
# The test inference stays the condition of every proof (prove_availability_once, presence_probe). The value is
# chosen, not measured: it bounds the reads of the challenge to one per this many seconds. The window itself is
# counted in blocks (`avail_deadline_blocks`), so the share of it this delay costs depends on the block
# interval, which the consensus does not fix.
AVAIL_CHECK_S = 30.0
# How often an empty weights_hash is read again (model_weights_hash). Chosen: one /api/tags request per this many
# seconds while the digest is unknown, none once it is known.
WHASH_RETRY_S = 300.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--faucet", default="http://127.0.0.1:4500")
    ap.add_argument("--backend", default="ollama", choices=["ollama", "mock"])
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    # The first heartbeat, before anything slow: a daemon still solving the faucet's proof of work or
    # waiting on its registration is starting, not stuck, and the file says which. It carries the kit
    # fields from the first write, null until the network has been read (kit_status).
    _kit_mine = kitv.read_kit_version()
    _kit = kit_status(_kit_mine, "")
    _status_write(phase="starting", started_at=int(time.time()), pid=os.getpid(), **_kit)
    if _kit_mine is None:
        print(f"[daemon] WARNING: this kit's version file is unreadable ({kitv.kit_version_file()}): the "
              f"deposits carry no kit_version, which the relay refuses once its floor is armed, and the "
              f"heartbeat cannot say whether this kit is current.", flush=True)
    _kit_url = os.environ.get("DENDRA_CONFIG_URL", "").strip()
    _kit_hint = os.environ.get("DENDRA_KIT_UPDATE", "").strip()
    _kit_at = 0.0

    def _kit_check():
        # Once an hour (KIT_CHECK_S): the published kit, the banner while behind, and the heartbeat fields.
        # A failed read leaves the previous reading's fields and says why; it never takes the loop down.
        nonlocal _kit, _kit_at
        _kit_at = time.time()
        try:
            _kit = kit_status(_kit_mine, _kit_url)
        except Exception as e:  # noqa: BLE001 -- read again at the next hour
            _kit = dict(_kit, kit_why=f"kit check failed ({type(e).__name__})")
        _status_note(**_kit)
        banner = kit_banner(_kit, _kit_hint)
        if banner:
            print(banner, flush=True)
        elif _kit.get("kit_behind") is None and _kit.get("kit_why"):
            print(f"[daemon] kit version not compared: {_kit['kit_why']}", flush=True)

    _kit_check()

    # Process confinement at startup (no same-user debugger, no core dump, no-new-privs; mlockall opt-in).
    # Best effort, never fatal. Off with DENDRA_CONFINE=0.
    # Strong OS confinement (routeless netns, seccomp, egress) lives in modea_confine.sh; the kit never runs it.
    if os.environ.get("DENDRA_CONFINE", "1") != "0":
        try:
            from modea import confine
            cr = confine.apply_process_confinement_report_cached()
            print(f"[daemon] confinement dumpable_off={cr['non_dumpable']} no_new_privs={cr['no_new_privs']} "
                  f"core_off={cr['core_dumps_disabled']} mlockall={cr['mlockall']} (root remains privileged)")
        except Exception as e:
            print(f"[daemon] confinement unavailable ({type(e).__name__}) -> continuing (best-effort)")

    # === Identity first: every key file below is named after it ===================================
    # The Cosmos key is obtained here, before every other key, because the identifier the chain accepts is
    # derived from its address (CreateMiner refuses any other). Resolving it after the x25519, attestation
    # or VRF key exist would leave them under a name nobody uses again, and create a second encryption
    # identity at the next start.
    Path(a.keydir).mkdir(parents=True, exist_ok=True)

    # The resolved identity is persisted, or every restart would create a new one and lose the stake.
    # `align_identity` below renames the keyring entry from the passed name to the derived `dm1...`, and
    # the passed name comes from `deploy/testnet-miner/.env`, which nothing rewrites. Without this memory
    # the next start would find no key under the old name and `keys_addr` would create one: a new address,
    # a new identifier and an unfunded account, while the registration, its stake and its held fees stay
    # on chain under an identifier this machine no longer holds (test_persistent_identity.py).
    #
    # The memory lives in `keydir`, the persisted volume the keyring itself relies on, because this is the
    # only place that knows the answer. An explicit `dm1...` on the command line still wins: this file is
    # a memory, never an override.
    # The file is `kring.RESOLVED_IDENTITY`; `migrate_resolved_identity` moves it from its earlier
    # name on a volume written by an older kit.
    _memory = Path(kring.migrate_resolved_identity(a.keydir))
    if not a.id.startswith("dm1") and _memory.exists():
        _guard = _memory.read_text(encoding="utf-8").strip()
        if _guard.startswith("dm1"):
            print(f"[daemon] identity resumed from {_memory}: {_guard} "
                  f"(the name passed in, '{a.id}', was resolved on a previous start)", flush=True)
            a.id = _guard

    # The keyring, from the state of the disk (modea/keyring.py). A keyring this process cannot open (two
    # keyrings, an encrypted one without its passphrase, a passphrase that does not open it) stops the
    # daemon here, before anything could create a key in its place.
    try:
        _kr_now = _keyring()
        for _w in _kr_now.warnings:
            print(f"[daemon] WARNING: {_w}", flush=True)
        if _kr_now.backend == kring.FILE:
            print(f"[daemon] keyring: encrypted (file backend, {_kr_now.directory}); passphrase read from "
                  f"{'the file ' + _kr_now.passphrase_file if _kr_now.source == 'file' else kring.ENV_PASSPHRASE}.",
                  flush=True)
        addr = keys_addr(a.id, a.keydir)
    except kring.KeyringError as e:
        _stop_on_keyring(e)
    # Owner mode is decided before the identity is aligned, because it decides which address the identifier
    # derives from, and a rename is the one step here that cannot be undone once the owner registers.
    try:
        owner = decide_owner(a.id, addr)
    except OwnerModeError as e:
        _stop_on_owner(e)
    a.id = align_identity(a.id, addr, owner=owner)
    # The address goes into the heartbeat, not only into the log: the kit rotates the container logs
    # (deploy/testnet-miner/docker-compose.yml, `logging:`), so the "ready  addr=..." line printed at start
    # disappears after enough traffic. `python3 -m modea.keyring address /data/keys` reads the same address
    # from the keyring itself.
    _status_write(miner_id=a.id, owner=owner, address=addr)

    # The signing name follows the rename, or every deposit would go out unsigned. `relay_client` read
    # `DENDRA_SIGN_KEY` at import (by default `MINER_ID`, the name that no longer exists), and exporting the
    # variable again would change nothing. This points it at the new name and clears the old name's cache.
    try:
        relay.set_sign_key(a.id)
    except AttributeError:
        # An older relay_client without the entry point: say it rather than sign for a dead name.
        print("[daemon] WARNING: relay_client has no set_sign_key -- deposits will be signed for "
              f"'{os.environ.get('DENDRA_SIGN_KEY', '')}', which the rename just replaced with "
              f"'{a.id}'. Under DENDRA_RELAY_SIGN=enforce they would be REFUSED.", flush=True)

    # Checked rather than assumed: a signing key that resolves to another address fails the same way, and
    # nothing shows it until enforcement is armed.
    try:
        _sa = relay_signature.address_from_key(a.id, keyring_dir=os.environ.get("DENDRA_KEYRING_DIR") or None)
    except Exception as _e:
        _sa = None
        print(f"[daemon] WARNING: cannot resolve the signing key '{a.id}': {_e}. Deposits will go out "
              "unsigned; under DENDRA_RELAY_SIGN=enforce the relay refuses them.", flush=True)
    if _sa and _sa != addr:
        print(f"[daemon] WARNING: the signing key '{a.id}' resolves to {_sa} but this miner is {addr}. "
              "The relay attributes a deposit to whoever signed it, so writes would be attributed to "
              "the wrong operator -- or refused.", flush=True)
    elif _sa:
        print(f"[daemon] signing identity: {a.id} -> {_sa} (matches this miner)", flush=True)

    # Written after alignment, so that only what the chain accepts is stored. A failed derivation leaves
    # `a.id` unchanged and not `dm1`, and storing it would make every later start resume a refused value.
    if a.id.startswith("dm1"):
        try:
            _memory.write_text(a.id + "\n", encoding="utf-8")
        except OSError as e:
            # Not fatal, since the miner works for this session, but said: the next restart would create
            # a new identity.
            print(f"[daemon] WARNING: cannot persist the resolved identity to {_memory} "
                  f"({type(e).__name__}: {e}). This miner will resolve a new identity at the next "
                  f"restart, losing this registration. Fix the volume before relying on it.", flush=True)

    skpath = Path(a.keydir) / f"{a.id}.sk"

    # --- encryption identity (X25519): persisted in the key envelope, under the keyring's passphrase ---
    # The passphrase is the keyring's (modea/keyring.py: the file mounted at /run/dendra-secrets in `file`
    # mode). A key found unencrypted while that passphrase is there is re-encrypted in place, atomically, and
    # the warning below reads the file itself, so a passphrase set after the fact cannot hide a clear key.
    _seal, _open = _kr_now.seal_with, _kr_now.open_with
    try:
        if skpath.exists():
            sk = crypto.load_sk(str(skpath), _open, seal_with=_seal)
        else:
            sk, _ = crypto.gen_keypair()
            crypto.save_sk(sk, str(skpath), _seal)
        _sk_sealed = crypto.file_is_sealed(str(skpath))
    except crypto.KeyEnvelopeError as e:
        _stop_on_keyring(kring.KeyringUnreadable(f"{skpath}: {e}"))
    if not _sk_sealed:
        print(f"[daemon] WARNING: the encryption key {skpath} is stored unencrypted (mode 0600): "
              + ("the keyring is the test keyring, so the key files are not sealed either. "
                 "bash deploy/testnet-miner/encrypt-keys.sh encrypts both." if _kr_now.backend == kring.TEST
                 else "no passphrase was available to seal it."), flush=True)
    mypub = crypto.pub_bytes(sk).hex()

    # The Cosmos key is obtained before the first deposit: the `pub` and `attest` writes must be signable
    # by the account that owns them, or a relay that requires signatures could never admit a new miner.
    # Resolving it early is safe, since `keys_addr` depends on neither `sk` nor `mypub`. The resolution
    # actually happens higher up, above the key files, because the identifier derives from this address;
    # this line only asserts that the two reads agree.
    assert addr == keys_addr(a.id), "the keyring address changed under us between the two reads"

    # The two bootstrap deposits (`pub`, `attest`) wait for the registration. A deposit is signed by this
    # miner's key (relay_client._signature), and a signature needs the account, which the chain does not
    # hold before the faucet's credit: sent earlier, both deposits would go out unsigned and be refused by
    # a relay that requires signatures (`enforce`). Once the chain records this miner its account exists,
    # so both can be signed. A miner that is not registered receives no job, so nothing waits on them.
    _enc_state = None    # check_enc_key: the anchored encryption key against this node's, read once registered
    _boot = BootstrapDeposits()

    def _bootstrap_deposits():
        # One round of the bootstrap deposits (BootstrapDeposits): the kinds still pending are sent, each
        # outcome is recorded, and a refused kind is due again after the round's delay. The first write, and
        # its result is logged: this is the only place that exercises the relay's write policy from a miner.
        # It stays non-fatal, because this key is a fallback: the source of truth is the `enc_pubkey`
        # anchored on chain (`reveal_helpers.committee_pubs`, and in strict mode the client refuses an
        # unanchored miner), so a refused deposit costs a cross-check, not the ability to mine.
        # A key that differs from the anchored one is not deposited: it would replace the relay's copy of the
        # anchored key with one no client seals to, and the self-test's cross-check would then accuse the relay.
        pending = _boot.pending()
        if "pub" in pending:
            if _enc_state == ENC_MISMATCH:
                print(f"[daemon] the encryption pubkey is not deposited at the relay for {a.id}: it differs from "
                      f"the one anchored on chain (the mismatch notice says how to fix it).", flush=True)
                _boot.record("pub", "skipped")
            else:
                got = relay.put_status(a.relay, "pub", a.id, {"pub": mypub})
                _boot.record("pub", got)
                if got in ("ok", "exists"):
                    print(f"[daemon] encryption pubkey published to the relay for {a.id}")
                else:
                    print(f"[daemon] the relay refused the pubkey deposit for {a.id}. Mining is not blocked -- the "
                          f"key anchored on chain is what peers use -- but the relay copy is missing until a "
                          f"retry is accepted, so the client-side cross-check against it is not available. If "
                          f"deposits are refused here, they will be refused for reveals too: check the relay's "
                          f"write policy before assuming this is cosmetic.", flush=True)
        # Signed software attestation (measured hash = code + model_id + weights_hash + confinement). A relay
        # with the gate active (DENDRA_ATTEST_REQUIRE=1 and an allow-list) assigns confidential jobs only to
        # attested miners. Best effort, never fatal. It is deterrence, not a proof of execution (see
        # modea/confine.py). "published" is a claim about the relay, printed only when the relay accepted.
        if "attest" in pending:
            try:
                from modea import confine as _confine
                _wh = model_weights_hash()
                _ask, _apub = _confine.load_or_create_attest_key(a.keydir, a.id, passphrase=_open, seal_with=_seal)
                _att = _confine.signed_attestation(_ask, miner_id=a.id, model_id=MODEL_ID,
                                                   weights_hash=_wh, enc_sk=sk)
            except Exception as e:  # noqa: BLE001 -- a build that cannot attest is not a relay refusal
                print(f"[daemon] attestation unavailable ({type(e).__name__}) -> continuing (best-effort)")
                _boot.record("attest", "unavailable")
            else:
                got = relay.put_status(a.relay, "attest", a.id, _att)
                _boot.record("attest", got)
                if got in ("ok", "exists"):
                    print(f"[daemon] signed attestation published  measured_hash={_att['measured_hash'][:16]}...  "
                          f"attest_pub={_apub[:16]}... (relay allow-list = DENDRA_ATTEST_ALLOW)")
                else:
                    print(f"[daemon] the relay refused the attestation deposit for {a.id} "
                          f"(measured_hash={_att['measured_hash'][:16]}...). While the relay gate is armed "
                          f"(DENDRA_ATTEST_REQUIRE=1) this miner is assigned no confidential job.", flush=True)
        _boot.close_round(time.time())
        _status_note(bootstrap_deposits=_boot.to_status())
        if not _boot.complete():
            print(f"[daemon] bootstrap deposits still refused ({', '.join(_boot.pending())}): tried again in "
                  f"{int(_boot.delay)} s, and until the relay accepts them.", flush=True)

    # --- chain identity: faucet + self-signed registration (the key was obtained above) ---
    try:
        vsk, vpk = vrf_identity(a.keydir, a.id)   # VRF identity (proof of availability)
    except crypto.KeyEnvelopeError as e:
        _stop_on_keyring(kring.KeyringUnreadable(f"{a.keydir}/{a.id}.vrf: {e}"))
    # Owner mode: the owner registers; this machine prepares, prints, and waits until the chain records its
    # key as the operator. Nothing below this block runs before that, and with --once nothing runs at all.
    if owner:
        if not owner_wait(a, addr, owner, mypub, vpk):
            print(f"[daemon] {a.id}: the owner has not registered this miner with this machine as its operator "
                  f"yet; nothing was mined.", flush=True)
            return
    # A registered identity whose operator is not this key gets every commit refused ("only the miner's
    # operator may anchor its commit"). An operator who lost the keyring (volume recreated, machine
    # reinstalled) would otherwise believe the miner registered and mine for nothing, so the daemon says
    # so. check_operator writes it to the heartbeat as well, since the kit rotates its logs.
    _op_state = check_operator(a.id, addr, os.environ.get("DENDRA_SLOT"))
    _op_at = time.time()
    # `not owner`: a miner in owner mode never registers itself, even slashed to a stake of zero: the bond
    # and the identity are the owner's, and this machine's key signing create-miner would be a second miner.
    # The attempt is register_attempt, replayed by the loop below until the chain records this miner.
    # The drain is read before the first registration attempt (registration_step, `draining`): a draining miner
    # that restarts must not register and stake again.
    _draining = drain_state(a.keydir, False)
    if owner:
        reg_state, reg_why, reg_retry_at = REGISTERED, "owner", 0
        _status_write(registration={"state": REGISTERED, "reason": "owner", "retry_at": 0})
    else:
        reg_state, reg_why, reg_retry_at = registration_step(a, addr, mypub, vpk, draining=_draining)
    # The encryption key the chain anchors, compared with this node's (check_enc_key), before the deposits,
    # which leave a differing key out of the relay.
    _enc_state = check_enc_key(a.id, mypub)
    # The bootstrap deposits, now that the account exists (see _bootstrap_deposits); otherwise the loop below
    # makes them once the registration it replays succeeds, and replays the refused ones at their due time.
    if reg_state == REGISTERED:
        _bootstrap_deposits()

    # This check lives outside the registration block on purpose. The omission notice above only fires while
    # registering, so a miner already registered without a key would never hear about it, and
    # re-registering does not help since `create-miner` is not replayed for an id in the registry. The
    # chain's state is therefore read at every startup and compared with what this node holds
    # (vrf_key_notice). A key that could not be read is compared again at the loop's half-hourly reads.
    _vrf_chain = miner_vrf_onchain(a.id)
    _vrf_text = vrf_key_notice(a.id, vpk, _vrf_chain)
    if _vrf_text:
        print(_vrf_text, flush=True)

    backend = pick_backend(a.backend)
    miner = Miner(a.id, backend=backend, hardened=True, sk=sk)
    # The engine's version (engine_version), in the heartbeat from the first pass, read again every half hour.
    _engine = engine_version(backend, getattr(miner, "backend", None))
    _status_note(engine_version=_engine)
    if _engine.get("engine") == "ollama":
        print(f"[daemon] inference engine: Ollama {_engine.get('version') or '? (' + str(_engine.get('why')) + ')'}",
              flush=True)
    whash = model_weights_hash()
    # An empty weights_hash is read again, at most every WHASH_RETRY_S, so that an engine not ready at start
    # does not leave it empty for the life of the process.
    _whash_at = time.time()
    if MODEL_ID:
        print(f"[daemon] model_id={MODEL_ID}  weights_hash={(whash[:16]+'...') if whash else '<absent>'}")
    # "ready" is printed only for a miner the chain records. A miner not registered yet says so, with the next
    # attempt, and the loop below replays the registration until it is.
    def _ready_line():
        _rs, _rec = registry_record(a.id)
        return (f"[daemon] miner {a.id} ready  addr={addr}  "
                f"stake={_rec.get('stake', 0) if _rs == PRESENT else '?'}  backend={backend}")
    if reg_state == REGISTERED:
        print(_ready_line(), flush=True)
    else:
        print(not_registered_line(a.id, reg_why, reg_retry_at - time.time()), flush=True)

    # The final testnet season's payout address (DENDRA_PAYOUT_ADDRESS, written by join.sh
    # --payout-address): declared once the miner is registered (the programme attributes a declaration to the
    # operator the chain records), and again only when the setting changes. Without it the season pays
    # this machine's own key, which every copy of this machine's keys can spend. Registered is read in the
    # registry (three states), never inferred from a stake: a miner slashed to zero is still registered.
    def _payout_try():
        if registry_record(a.id)[0] != PRESENT:
            return False, "the payout declaration waits for the on-chain registration"
        try:
            return maybe_declare_payout(a.keydir, a.id, addr, owner=owner)
        except Exception as e:  # noqa: BLE001 -- a declaration that fails is retried, never fatal
            return False, f"payout declaration skipped ({type(e).__name__}: {e}); retried in 30 minutes"
    _payout_settled, _payout_msg = (_payout_try() if PAYOUT_ADDRESS else (True, ""))
    if _payout_msg:
        print(f"[daemon] {_payout_msg}", flush=True)
    _payout_at = time.time()
    print(f"[daemon] waiting for jobs at the relay {a.relay} ...")

    done = set()
    suffix = "__" + a.id
    _last_chal = ""
    # When the challenge was last read (AVAIL_CHECK_S). The clock starts with the proof made at start, below: the
    # first pass then reads it again only once AVAIL_CHECK_S has passed.
    _avail_at = time.time()
    _subsidy_at = time.time() - 1500      # first claim attempt about five minutes after start
    # The final testnet season asks the miner nothing: it pays the work served, the verdicts and
    # the availability windows proven on chain. A miner with no VRF key proves no window, and says so, once
    # an hour.
    _novrf_said = 0.0
    _stale_seen = set()                   # requests counted stale by this process (requests_stale, once each)
    # Every test inference leaves its outcome as the engine's latest reading (recorded_probe): the one an
    # availability proof waits for, and the one a failed request asks for. It is a generation and the embedding a
    # request needs (serving_probe): both readers ask "can this engine serve a request now?".
    _probe = recorded_probe(probe=lambda: serving_probe(probe=lambda: presence_probe(miner.backend),
                                                        embed=lambda text: answer_embedding(text, miner.backend)))
    if _draining:
        print(f"[daemon] {a.id} starts draining ({a.keydir}/{DRAIN_FILE} exists): no availability proof and no "
              f"new request; what this miner committed to is finished, and the reveal worker keeps running. "
              f"Remove the file to resume.", flush=True)
    # The chain's epoch (EpochProbe), this kit's dendrad (KitCli) and the judge declaration (JudgeSeat): what a proof
    # carries and whether a judge is declared depend on the first two. The judge role is this container's, fixed for
    # its life: judge.sh on and off recreate the container.
    _kit_cli = KitCli()
    _epoch = EpochProbe()
    _seat = JudgeSeat(a.id, _kit_cli)
    _judge_role = judge_role_on()

    def _drain_check():
        """Reads DRAIN_FILE again; a change is logged once, and the judge declaration is read at the next turn."""
        nonlocal _draining
        now_d = drain_state(a.keydir, _draining)
        if now_d != _draining:
            print(f"[daemon] {a.id} " + (
                f"draining ({a.keydir}/{DRAIN_FILE} appeared): no availability proof and no new request from "
                f"now on; what this miner committed to is finished, and the reveal worker keeps running."
                if now_d else f"no longer draining ({a.keydir}/{DRAIN_FILE} is gone): proofs and requests "
                              f"resume."), flush=True)
            _seat.wake()
        _draining = now_d
        return now_d

    def _kit_flags():
        """proof_kit_flags for the next proof; the flag of this dendrad is read only on a chain known to run epoch 15."""
        if not _epoch.applied():
            return ()
        has = _kit_cli.has(("tx", "jobs"), "prove-availability", "--kit-version")
        if has is False:
            _kit_cli.predates(a.id, "--kit-version on `tx jobs prove-availability` (proofs go out without it)")
        return proof_kit_flags(_epoch.acting, _kit_mine, has)

    def _judge_turn():
        """The epoch read on its period, then the judge declaration (JudgeSeat.turn). Never stops the pass."""
        try:
            if _epoch.tick():
                _seat.wake()
            _seat.turn(_epoch.acting, _judge_role, _draining,
                       reg_state == REGISTERED and _op_state != "mismatch")
        except Exception as e:  # noqa: BLE001 -- tried again at its next turn, never fatal
            print(f"[daemon] {a.id} judge declaration skipped ({type(e).__name__}: {e})", flush=True)

    def _presence_turn(force=False):
        """The availability proof first (AVAIL_CHECK_S): the challenge read on its clock -- at start (`force`), at
        the head of every pass, between two requests -- and a new one proven before anything else is served,
        always behind a test inference that answered (prove_availability_once). Never while draining, never while
        the encryption key differs from the anchored one, never for a miner the chain is known not to record
        (registration_allows_proof: a registry that could not be read is not a miner that is absent)."""
        nonlocal _last_chal, _avail_at
        if not vsk or not registration_allows_proof(reg_state, reg_why):
            return
        if _draining or _enc_state == ENC_MISMATCH:
            _status_note(presence={"at": int(time.time()), "result": "not attempted",
                                   "why": "draining" if _draining else "the encryption key differs from the "
                                                                       "anchored one"})
            return
        now_p = time.time()
        if not force and now_p - _avail_at < AVAIL_CHECK_S:
            return
        _avail_at = now_p
        _last_chal = prove_availability_once(a.id, vsk, _last_chal, keydir=a.keydir, probe=_probe,
                                             kit_flags=_kit_flags())

    # At start, the epoch is read before the first proof, which it decides the form of, and the proof comes before
    # the first pass: a challenge open since the miner went down is answered first.
    _epoch.tick(force=True)
    _presence_turn(force=True)
    _status_write(phase="loop", draining=_draining)
    while True:
        try:
            _drain_check()
            _judge_turn()
            _presence_turn()
            # This miner's slice only (`suffix`, relay_client.listing): the relay answers the keys ending with
            # `__<this id>` (every key this loop reads), and a relay that does not serve the filter is read in
            # full and filtered by the client, so the same jobs are seen either way.
            lst = relay.listing(a.relay, suffix)
            # The relay names every one of its kinds in this answer, even when they are empty, so an
            # empty dict is the client's own failure value (relay_client.listing): only a non-empty
            # answer is a queue that was read.
            _listing_read = isinstance(lst, dict) and bool(lst)
            if _listing_read:
                _status_note(relay_listing_ok_at=int(time.time()))
            ress = set(lst.get("res", []))
            # The journal and the failures are read once per pass, not once per key of the whole relay.
            _journal = _read_journal(a.keydir)
            _fails = _read_failures(a.keydir)
            _mine = newest_first(lst.get("req", []), suffix)
            if _listing_read:
                _listed = set(_mine)
                _gone = [k for k in _fails if k not in _listed]
                if _gone:
                    for k in _gone:
                        _fails.pop(k, None)
                    _write_failures(a.keydir, _fails)
            for key in _mine:
                # A job whose response is deposited but whose commitment is still pending is
                # not skipped: it is the one to resume, and the resumption recomputes nothing, it replays the
                # anchoring with the journalled commitment.
                _pending = _journal.get(key) or {}
                _fe = _fails.get(key) or {}
                if key in done or (key in ress and not _pending):
                    continue
                if _pending and key in ress:
                    # Resumption. The response is already at the relay and its commitment is journalled:
                    # only the anchoring is replayed. Re-running inference would produce a different
                    # answer, and the deposit is write-once and sealed to the client.
                    _c, _pc = _pending.get("commit", ""), _pending.get("pcommit", "")
                    if _c and _c in query("get-commit", key):
                        # Counted only when this process broadcast it (a late inclusion of an earlier pass).
                        _commit_anchored(key)
                        _forget_commitment(a.keydir, key)
                        if _fails.pop(key, None) is not None:
                            _write_failures(a.keydir, _fails)
                        done.add(key)
                        continue
                    # A refused resumption waits its pause (JOB_FAILURES), instead of a transaction per pass.
                    if _fe.get("kind") == "anchor" and not job_due(_fe, time.time()):
                        continue
                    # The flags journalled with the commitment (_journal_flags): the model that answered.
                    _out = tx_from(a.id, "create-commit", key, _pc or _c, _c, "infer",
                                   flags=_journal_flags(_pending))
                    _COMMITS_SENT.add(key)
                    _rh, _rc = tx_fate(_out)
                    if _rc is not None and _rc != 0:
                        _commit_count("commits_refused")
                    if _rh > 0 and _rc == 0 and _c in query("get-commit", key):
                        _commit_anchored(key)
                        _forget_commitment(a.keydir, key)
                        if _fails.pop(key, None) is not None:
                            _write_failures(a.keydir, _fails)
                        done.add(key)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "anchored (resumed from the journal)"})
                        print(f"[daemon] {a.id} job {key}: anchoring resumed from the journal (the inference is not run again)")
                    else:
                        _fe = note_job_failure(_fails, key, "anchor", _tx_err(_out), time.time())
                        _write_failures(a.keydir, _fails)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "commit not anchored", "why": _tx_err(_out),
                                           "next_at": int(_fe["next_at"])})
                        print(f"[daemon] {a.id} job {key}: anchoring still refused -> the commitment stays"
                              f" in the journal; retried without recomputing in "
                              f"{int(_fe['next_at'] - _fe['last_at'])} s")
                    continue
                # A deposit refused earlier: its sealed response was kept (JOB_FAILURES) and its commitment
                # journalled with it. Only the deposit is replayed, with that response -- never a new inference,
                # whose answer the journalled commitment would not describe. Once it is accepted the request shows
                # in the relay's `res` listing, and the resumption above anchors it at the next pass.
                if _pending and _fe.get("kind") == "deposit" and isinstance(_fe.get("sealed"), dict):
                    if not job_due(_fe, time.time()):
                        continue
                    if relay.put(a.relay, "res", key, _fe["sealed"]):
                        _fails.pop(key, None)
                        _write_failures(a.keydir, _fails)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "response deposited (kept from a refused deposit)"})
                        print(f"[daemon] {a.id} job {key}: the sealed response kept after a refused deposit is now "
                              f"deposited (the inference is not run again); its journalled commitment is anchored next.")
                    else:
                        _job_count("deposit_refused")
                        _fe = note_job_failure(_fails, key, "deposit", "the relay refused the sealed response",
                                               time.time(), sealed=_fe["sealed"])
                        _write_failures(a.keydir, _fails)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "response refused by the relay", "attempt": _fe["failures"],
                                           "next_at": int(_fe["next_at"])})
                        print(f"[daemon] {a.id} job {key}: the relay refused the kept sealed response again "
                              f"(deposit {_fe['failures']}); the same response is offered again in "
                              f"{int(_fe['next_at'] - _fe['last_at'])} s, never a new inference.")
                    continue
                if not _RE_KEY.match(key):       # unsafe relay key: ignored (no dendrad argument injection)
                    continue
                jid = key[: -len(suffix)]
                # A request given up, or waiting its pause (JOB_FAILURES), is not generated now.
                if _fe.get("abandoned") or not job_due(_fe, time.time()):
                    continue
                # A new request nobody waits for any more (request_max_age_s) is not served, by this machine's clock
                # and the chain's (request_stale_age). Never applied to a request whose commitment is journalled:
                # that one was served already.
                if not _pending:
                    _age = request_stale_age(jid, time.time(), chain_now)
                    if _age is not None:
                        if key not in _stale_seen:
                            _stale_seen.add(key)
                            _job_count("requests_stale")
                            print(f"[daemon] {a.id} job {jid}: not served, made {int(_age)} s ago, past "
                                  f"{request_max_age_s():.0f} s (DENDRA_REQUEST_MAX_AGE_S): its client no longer "
                                  f"waits for it. Said once.", flush=True)
                        continue
                # Draining: no new request (drain_state, read again here: it may have appeared mid-pass).
                if _drain_check():
                    continue
                # The availability proof before the next request, when a new challenge is open (AVAIL_CHECK_S).
                _presence_turn()
                req = relay.get(a.relay, "req", key)
                if not req:
                    continue
                # One malformed deposit must not starve every job behind it, so the decoding of the request
                # body is caught here rather than by the loop's own `except`. Caught at the loop level, a
                # `req` missing `client_eph_pk` or carrying non-hex values would end the whole `for`, and
                # since the keys are walked in the same order on every pass and the key is write-once, the
                # same deposit would stop every pass at the same place until the relay's retention expires
                # it. A body that cannot be parsed is one job skipped, not a pass abandoned.
                try:
                    eph = bytes.fromhex(req["client_eph_pk"])
                    sealed = Sealed(bytes.fromhex(req["nonce"]), bytes.fromhex(req["ct"]))
                except Exception as e:
                    print(f"[daemon] {a.id} job {jid}: unusable request body "
                          f"({type(e).__name__}) -> skipped, the other jobs continue")
                    continue
                # How long the answer took, measured on every job. The client waits a bounded time (the Final
                # Testnet Season generator: client.quick_metered, its `timeout`), and a judge on the CPU
                # (deploy/hw_probe.sh --role: judge) runs a model whose speed depends on the machine's memory,
                # which its RAM floor does not measure. A slow answer shows here, in the log and the heartbeat,
                # rather than later as a request that was never paid.
                _t0 = time.monotonic()
                try:
                    res = miner.handle_job(jid, eph, sealed, max_out=int(req.get("max_out", 0)))  # requested cap
                except Exception as e:
                    _gen_f = round(time.monotonic() - _t0, 1)
                    _job_count("inference_failed")
                    # The engine is asked a test request right after (engine_answered_since): only a failure the
                    # engine answered around counts towards giving this request up.
                    _engine_ok = engine_answered_since(time.time(), _probe)
                    _fe = note_job_failure(_fails, key, "inference", type(e).__name__, time.time(),
                                           counted=_engine_ok)
                    _write_failures(a.keydir, _fails)
                    if _fe.get("abandoned"):
                        _job_count("abandoned")
                    _status_write(job={"at": int(time.time()), "job": jid,
                                       "result": "abandoned" if _fe.get("abandoned") else "inference failed",
                                       "why": type(e).__name__, "gen_s": _gen_f, "attempt": _fe["failures"],
                                       "engine_answered": _engine_ok,
                                       "next_at": None if _fe.get("abandoned") else int(_fe["next_at"])})
                    if _fe.get("abandoned"):
                        print(f"[daemon] {a.id} job {jid}: abandoned after {_fe['counted']} failed inferences "
                              f"({type(e).__name__}), each followed by an engine that answered a test request and "
                              f"embedded it: the request itself fails, and it is not generated again "
                              f"(DENDRA_JOB_MAX_ATTEMPTS={job_max_attempts()}).", flush=True)
                    else:
                        print(f"[daemon] {a.id} inference failed for job {jid}: {type(e).__name__} (attempt "
                              f"{_fe['failures']}; the engine "
                              f"{'answered and embedded' if _engine_ok else 'did not answer and embed'} a test "
                              f"request right after"
                              f"{'' if _engine_ok else ', so this failure does not count against the request'}"
                              f"); next attempt in {int(_fe['next_at'] - _fe['last_at'])} s.", flush=True)
                    continue
                _gen_s = round(time.monotonic() - _t0, 1)
                # Was it in time for its client? Against the wait the client declared (declared_wait_s), never a
                # bound written here; no declared wait, no verdict.
                _wait_s = declared_wait_s(req)
                _timing = answer_timing(_gen_s, _wait_s)
                _job_count({"late": "answers_late", "on_time": "answers_on_time"}.get(_timing, "answers_wait_undeclared"))
                if _timing == "late":
                    print(f"[daemon] {a.id} job {jid}: answered in {_gen_s} s, past the {_wait_s:g} s its client "
                          f"declared it waits: the answer is deposited, but its client has most likely stopped "
                          f"waiting for it.", flush=True)
                # The generation succeeded: a failure recorded for it is over (a deposit refused below starts anew).
                if _fails.pop(key, None) is not None:
                    _write_failures(a.keydir, _fails)
                # The deposit is the only copy of the answer, so its refusal gates everything after it.
                # The relay can decline a write in production: unsigned or unauthorised (401), over the
                # store's limit (413), store saturated or full (503/507). A commit anchored for a response
                # the client can never fetch leaves a job that cannot be verified and an escrow that never
                # settles, so a refused deposit anchors nothing, marks nothing, and waits for the next pass.
                # The commitment is computed and made durable before the deposit. It cannot
                # be recomputed later (an LLM does not answer the same way twice, and this process cannot
                # read back the sealed deposit), and memory does not survive a restart, so the step that
                # cannot be repeated is journalled first; only the deposit and the anchoring are retried.
                # tests/test_miner_journal.py covers the resumption.
                commit = res.content_embed   # embedding: the semantic mode tolerates free-form LLM output
                # The question gets its own commitment, computed by `handle_job` while the plaintext was still
                # inside its locked buffer. It must differ from `commit`: a juror receives the prompt from the
                # reveal, that is from the party being audited, so only a commitment made before the answer
                # was known ties the grading to the question the client actually asked. An empty value (a
                # miner build that cannot compute it) falls back to the answer commitment, which a juror
                # recognises and abstains on rather than slashing: a miner is not punished for an old build.
                pcommit = res.prompt_commit or commit
                # An empty weights_hash is read again (WHASH_RETRY_S) before it is journalled and anchored.
                if MODEL_ID and not whash and time.time() - _whash_at >= WHASH_RETRY_S:
                    _whash_at = time.time()
                    whash = model_weights_hash()
                # The model that answered is journalled with the commitment: a resumption anchors it with these.
                _journal_commitment(a.keydir, key, commit, pcommit, model_id=MODEL_ID, weights_hash=whash)
                _res_body = {"nonce": res.sealed_result.nonce.hex(), "ct": res.sealed_result.ct.hex(),
                             "in_tok": res.in_tok, "out_tok": res.out_tok}   # real tokens (per-token pricing)
                if not relay.put(a.relay, "res", key, _res_body):
                    # The sealed response is kept (JOB_FAILURES): the next attempt deposits it again, never a new
                    # inference. It is sealed to the client's key: this process keeps bytes it cannot read.
                    _job_count("deposit_refused")
                    _fe = note_job_failure(_fails, key, "deposit", "the relay refused the sealed response",
                                           time.time(), sealed=_res_body)
                    _write_failures(a.keydir, _fails)
                    _status_write(job={"at": int(time.time()), "job": jid,
                                       "result": "response refused by the relay", "gen_s": _gen_s,
                                       "attempt": _fe["failures"], "next_at": int(_fe["next_at"])})
                    print(f"[daemon] {a.id} job {jid}: the relay refused the sealed response -> commit not "
                          f"anchored, job not marked done; the same sealed response is offered again in "
                          f"{int(_fe['next_at'] - _fe['last_at'])} s (kept in {JOB_FAILURES}), never a new "
                          f"inference")
                    continue
                flags = (["--model-id", MODEL_ID] if MODEL_ID else []) + (["--weights-hash", whash] if whash else [])
                anchored = bool(commit) and (commit in query("get-commit", key))
                for _ in range(3):
                    if anchored:
                        break
                    out = tx_from(a.id, "create-commit", key, pcommit, commit, "infer", flags=flags)
                    _COMMITS_SENT.add(key)
                    _rh, _rc = tx_fate(out)
                    if _rc is not None and _rc != 0:
                        _commit_count("commits_refused")
                    if not (_rh > 0 and _rc == 0):
                        _nc = (commit.count(",") + 1) if commit else 0
                        _anchor_why = _tx_err(out)
                        print(f"[daemon] {a.id} create-commit job {jid} failed ({_nc}c): {_anchor_why}")
                        print(f"[daemon]   commit[:90]={commit[:90]!r}")
                        print(f"[daemon]   out[:450]={out[:450]!r}")
                        # A step of the loop: the heartbeat moves with it, so a slow chain reads as slow
                        # and not as a loop that stopped.
                        _status_write(job={"at": int(time.time()), "job": jid,
                                           "result": "anchoring attempt failed", "why": _anchor_why})
                    time.sleep(2)
                    anchored = commit in query("get-commit", key)
                if anchored:
                    # Counted once, and only when this pass (or an earlier pass of this process) broadcast
                    # it: a commitment already on the chain before any broadcast here is not this process's.
                    _commit_anchored(key)
                    done.add(key)
                    # The commitment is on the chain, so the journal may forget it: it is cleared only once the
                    # chain is read, never on a hope.
                    _forget_commitment(a.keydir, key)
                    _status_write(job={"at": int(time.time()), "job": jid, "result": "anchored",
                                       "gen_s": _gen_s, "out_tok": getattr(res, "out_tok", 0),
                                       "wait_s": _wait_s, "late": None if _timing is None else _timing == "late"})
                    _bal = bal_token(addr)
                    print(f"[daemon] {a.id} processed job {jid} -> sealed response + proof anchored "
                          f"(answered in {_gen_s} s, {getattr(res, 'out_tok', 0)} tokens; balance: "
                          f"{'?' if _bal is None else _bal} token)")
                else:
                    _status_write(job={"at": int(time.time()), "job": jid, "result": "commit not anchored",
                                       "why": "three anchoring attempts refused; the commitment is journalled",
                                       "gen_s": _gen_s, "wait_s": _wait_s,
                                       "late": None if _timing is None else _timing == "late"})
                    # The inference is over, and the message says so. The sealed response is deposited, so the
                    # key appears in the relay's `res` listing, and the loop never generates a listed key again:
                    # a new answer would differ and its commitment would describe an answer the client can never
                    # fetch, and this process cannot read back the sealed one. What is retried is the anchoring
                    # of the journalled commitment (the resumption).
                    print(f"[daemon] {a.id} job {jid}: response posted, commit not anchored -> the commitment is "
                          f"journalled; the next pass retries the anchoring only, never the inference (an LLM "
                          f"does not answer twice the same, and the deposit is sealed to the client). Fix what "
                          f"refused the transaction; nothing is lost.")
        except Exception as e:
            print(f"[daemon] {a.id} loop: {type(e).__name__}: {e}")
            # Recorded, and the pass still ends with the write below: a loop that raises on every pass is
            # still turning, and its heartbeat says both that it turns and what it keeps hitting.
            _status_write(loop_error={"at": int(time.time()), "what": f"{type(e).__name__}: {e}"[:300]})
        # The availability proof is not tried here: it comes first (_presence_turn, at the head of the pass and
        # between two requests, on its own clock).
        if not vsk and time.time() - _novrf_said > 3600:
            _novrf_said = time.time()
            _status_note(presence={"at": int(time.time()), "result": "no VRF key",
                                   "why": "this node holds no VRF key, so it proves no availability window"})
            print(f"[daemon] {a.id} has no VRF key: it proves no availability window. Past the grace a "
                  f"newcomer gets, the chain draws only present miners for work, so it gets no "
                  f"more work and earns nothing from the Final Testnet Season. See the VRF message "
                  f"printed at start-up for what anchoring a key takes for this identity.", flush=True)
        if time.time() - _subsidy_at > 1800:
            _subsidy_at = time.time()
            try:
                print(f"[daemon] {a.id} {maybe_claim_subsidy(a.id)}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[daemon] {a.id} subsidy claim skipped ({type(e).__name__})", flush=True)
        # The operator, read again (check_operator): a mismatch fixed on chain clears from the heartbeat, one that
        # appears is logged. Same cadence as the subsidy: one registry read per half hour.
        if time.time() - _op_at > 1800:
            _op_at = time.time()
            try:
                _op_state = check_operator(a.id, addr, os.environ.get("DENDRA_SLOT"), _op_state)
            except Exception as e:  # noqa: BLE001 -- a read that fails is read again, never fatal
                print(f"[daemon] {a.id} operator check skipped ({type(e).__name__})", flush=True)
            # Same cadence: the encryption key anchored (a mismatch fixed on chain resumes the proofs, one that
            # appears stops them), the VRF key while it has not been read yet, and the engine's version.
            try:
                _enc_state = check_enc_key(a.id, mypub, _enc_state)
                if _vrf_chain is None:
                    _vrf_chain = miner_vrf_onchain(a.id)
                    _vrf_text = vrf_key_notice(a.id, vpk, _vrf_chain)
                    if _vrf_text:
                        print(_vrf_text, flush=True)
                _engine_now = engine_version(backend, getattr(miner, "backend", None))
                if _engine_now.get("version") and _engine_now.get("version") != _engine.get("version"):
                    print(f"[daemon] inference engine: Ollama {_engine_now['version']} "
                          f"(was {_engine.get('version') or 'not read'})", flush=True)
                _engine = _engine_now
                _status_note(engine_version=_engine)
            except Exception as e:  # noqa: BLE001 -- read again at the next half hour, never fatal
                print(f"[daemon] {a.id} key and engine checks skipped ({type(e).__name__})", flush=True)
        if not _payout_settled and time.time() - _payout_at > 1800:
            _payout_at = time.time()
            _payout_settled, _payout_msg = _payout_try()
            if _payout_msg:
                print(f"[daemon] {_payout_msg}", flush=True)
        # The published kit, once an hour (kit_status): the banner while behind, the heartbeat fields always.
        if time.time() - _kit_at >= KIT_CHECK_S:
            _kit_check()
        # The bootstrap deposits the relay refused, at their due time (BootstrapDeposits), for a registered
        # miner only: before the registration the account does not exist and nothing can be signed.
        if reg_state == REGISTERED and _boot.due(time.time()):
            try:
                _bootstrap_deposits()
            except Exception as e:  # noqa: BLE001 -- tried again at the next due time, never fatal
                _boot.close_round(time.time())
                print(f"[daemon] {a.id} bootstrap deposits skipped ({type(e).__name__}); next try in "
                      f"{int(_boot.delay)} s.", flush=True)
        # The registration is replayed until the chain records this miner (see register_attempt). It has its own
        # try: an attempt that raises is retried at the next due time and never stops the loop. A draining miner
        # is on its way out and does not register.
        if not owner and reg_state != REGISTERED and not _draining and time.time() >= reg_retry_at:
            try:
                reg_state, reg_why, reg_retry_at = registration_step(a, addr, mypub, vpk)
                if reg_state == REGISTERED:
                    print(_ready_line(), flush=True)
                    _enc_state = check_enc_key(a.id, mypub, _enc_state)
                    if _boot.due(time.time()):
                        _bootstrap_deposits()
                else:
                    print(not_registered_line(a.id, reg_why, reg_retry_at - time.time()), flush=True)
            except Exception as e:  # noqa: BLE001 -- the attempt is replayed; the loop goes on
                reg_retry_at = time.time() + REGISTER_RETRY_S
                print(f"[daemon] {a.id} registration attempt skipped ({type(e).__name__}); next one in "
                      f"{int(REGISTER_RETRY_S)} s.", flush=True)
        # The pass is over, whatever happened: one write per pass, after the error above if there was one.
        _status_write(loop_at=int(time.time()), phase="loop", draining=_draining)
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
