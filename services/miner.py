#!/usr/bin/env python3
"""Persistent LOCAL miner (the real Dendra miner client).

At startup: creates its encryption key (X25519, local) + publishes its pub to the relay; creates its
CHAIN key, funds itself at the FAUCET, and registers on-chain by SIGNING itself -- or, in OWNER MODE
(DENDRA_MINER_OWNER, deploy/join.sh --owner), prepares the registration its owner signs elsewhere and waits
for it (see OWNER below).
Then LOOPS: fetches from the relay the jobs assigned to it -> decrypts in locked memory
-> infers on OLLAMA (GPU) -> returns the SEALED response -> anchors its content_commit on-chain.
Content stays encrypted; only metadata + hash go on-chain. It earns `token` on each
honest verdict (payout from the client's escrow).

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
from modea import owner_tx
from modea.crypto import Sealed
from modea.miner import Miner, answer_embedding
import relay_client as relay
# The faucet PoW contract (digest, verifier, solver) is defined ONCE, in the service that enforces it.
# The solver is imported rather than reimplemented as a twin: a twin that drifts on a separator or an
# encoding makes the faucet unreachable, and neither side reports it.
import faucet as faucet_pow

# ADR-048 item 8: the chain id is READ (DENDRA_CHAIN_ID, cross-checked against the node), never written
# here. See modea/chain_id.py for why there is no default.
NODE = os.environ.get("DENDRA_NODE", "")  # e.g. "tcp://chain:26657" in a container; "" = local node
# Model registry: if set, the miner DECLARES this model_id in its commits (--model-id flag).
# REQUIRED when enforce_model_registry=ON; empty (default) = no flag -> historical behavior intact.
MODEL_ID = os.environ.get("DENDRA_MODEL_ID", "")

# PERSISTENT KEYRING. Empty = the historical behaviour (keyring under the process HOME).
# In a container that HOME is NOT a volume: `docker compose up --build` recreates the container, the
# Cosmos key DISAPPEARS, and the miner restarts on a NEW address — with no funds, no registration and
# no stake, while its logical identity (MINER_ID) has not moved. "Do not delete the miner-keys volume,
# or you re-stake" is therefore misleading: that volume only carried the X25519/VRF keys, never the
# on-chain signing key. Pointing DENDRA_KEYRING_DIR into the volume fixes the real cause.
KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "")

# Time bound on the faucet PoW. 20 bits is ~1M digests; the bound exists so that a network configured
# too hard produces a NAMED failure instead of an endless loop at startup.
POW_MAX_S = float(os.environ.get("DENDRA_FAUCET_POW_MAX_S", "300"))


# THE KEYRING IS RESOLVED ONCE, FROM THE STATE OF THE DISK (modea/keyring.py): `test` or `file`, and in
# `file` mode the passphrase, which reaches dendrad on stdin. Resolving raises a named KeyringError (two
# keyrings, passphrase missing or unreadable): main() says why and STOPS -- a keyring this process cannot
# open is never replaced by a new one.
_KR = None


def _keyring():
    global _KR
    if _KR is None:
        _KR = kring.resolve(KEYRING_DIR or None)
    return _KR


def _kr():
    """Keyring flags common to EVERY command that signs or reads a key."""
    return _keyring().flags()


def _krun(c, t=hb.DENDRAD_CALL_BOUND_S):
    """`run` for a command that opens the keyring: the passphrase goes on stdin, never in argv."""
    return run(c, t, stdin=_keyring().stdin())
_RE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # sane relay key (anti dendrad injection). Dash allowed (IDs like "miner-A") but NEVER leading -> a key cannot be taken for a dendrad flag.


def _node():
    return ["--node", NODE] if NODE else []


def ollama_ref(name) -> str:
    """An Ollama model reference WITH its tag. Ollama reads a name without a tag as `<name>:latest`, and lists
    it so in /api/tags: compared as written, `mistral-nemo` never matches `mistral-nemo:latest`, and a miner
    serving an untagged name anchors its commits with an EMPTY weights_hash. The tag is what follows the last ':'
    after the last '/', since a registry host in front of the name may carry a port of its own."""
    n = str(name or "").strip()
    if not n:
        return ""
    return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"


def model_weights_hash():
    """Digest of the served model's weights (Ollama manifest) -> binds the commit to
    the artifact. Best-effort: "" if Ollama unreachable/model absent (the on-chain check only bites if
    enforce_model_registry=ON AND the registry has a non-empty weights anchor). The served model is found
    in /api/tags by its reference with its tag (ollama_ref); the commit keeps naming MODEL_ID as written."""
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
    # stdin is ALWAYS given (empty when nothing is to be read): an inherited stdin would leave a prompt
    # nobody answers waiting until the call's time bound.
    r = subprocess.run(c, capture_output=True, text=True, timeout=t, input=stdin)
    return (r.stdout or "") + (r.stderr or "")


# ── THE HEARTBEAT (modea/heartbeat.py) ────────────────────────────────────────────────────────────
# One JSON file in the container's /tmp, rewritten as the loop moves: when the loop last went round,
# its last error, the last time the relay answered the work queue, what the last availability proof came
# to (the height `wait_tx` read, or the chain's refusal), and what happened to the last job. Two readers
# only: the container's healthcheck and miner_selftest.py. It is diagnosis, never a verdict on the chain.
# ⛔ A WRITE THAT FAILS NEVER STOPS THE LOOP. The heartbeat is the least important thing this process
# does; a full /tmp or a read-only filesystem must cost a missing file, never a miner that stops serving.
# The failure is said ONCE, because a message per pass would bury the log it is meant to complement.
_STATUS: dict = {}
_STATUS_WARNED = False

# >>> COMMIT COUNTERS (chantier 2) -- read by the HiveOS stats as `ar` (deploy/hiveos/dendra/h-stats.sh).
# TWO COUNTS OF create-commit TRANSACTIONS, SINCE THIS PROCESS STARTED: a restart sets both back to zero,
# like the heartbeat file itself, which describes this process and dies with it.
#   commits_anchored  transactions this process broadcast whose commitment it then READ on the chain
#                     (`get-commit`): at most one per job, since a job is done once its commitment is read;
#   commits_refused   transactions this process broadcast that the chain answered with a NON-ZERO code,
#                     at broadcast or once included (`tx_fate`). A job refused three times counts three.
# A transaction whose fate was not read -- no transaction response (among them a refusal while the node
# estimates its gas, which is never broadcast), not found in time, a node that did not answer -- counts in
# NEITHER: unknown is not refused. A commitment found on the chain that this process did not broadcast (an
# earlier run anchored it) is not counted either: these are this process's counts.
# Both exist from the FIRST heartbeat on (zero is then a reading: nothing counted yet), are copied into every
# write by _status_write, and are incremented by _commit_count, which never raises.
_COMMITS = {"commits_anchored": 0, "commits_refused": 0}
_COMMITS_SENT: set = set()   # relay keys whose create-commit THIS process broadcast and not yet counted anchored


def _commit_count(name: str) -> None:
    try:
        _COMMITS[name] += 1
    except Exception:  # noqa: BLE001 -- counting is diagnosis, like the heartbeat: it never takes the loop down
        pass


def _commit_anchored(key: str) -> None:
    """Counts the job `key` anchored once, and only when THIS process broadcast its create-commit."""
    if key in _COMMITS_SENT:
        _COMMITS_SENT.discard(key)
        _commit_count("commits_anchored")
# <<< COMMIT COUNTERS

# THE REQUEST COUNTERS, SINCE THIS PROCESS STARTED, like the commit counters above (a restart sets them back to
# zero), in every heartbeat from the first one (zero is then a reading):
#   inference_failed         inferences that raised (each attempt counts, see JOB_FAILURES);
#   deposit_refused          sealed responses the relay refused, at the first deposit or at a replay of it;
#   abandoned                requests given up after job_max_attempts() failed inferences, each followed by an
#                            engine that answered a test request (a failure while the engine is silent is the
#                            engine's, and gives nothing up);
#   requests_stale           NEW requests not served because older than request_max_age_s() by this machine's
#                            clock and, once read, the chain's (request_stale_age), once per request;
#   answers_on_time          answers generated within the wait their client DECLARED in its request (wait_s);
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
    """Records fields for the NEXT write without writing: the loop writes once per pass, not once per
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
            print(f"[daemon] heartbeat NOT written to {hb.status_path()} ({type(e).__name__}: {e}). Mining "
                  f"continues; the container's healthcheck and miner_selftest.py will report the loop as "
                  f"not measured until the file can be written. Said once.", flush=True)
        return False


def tx_from(frm, sub, *positionals, flags=()):
    # robust NONCE: a miner-judge shares 1 account across 3 processes (commit/reveal/verdict)
    # -> 2 close tx pull the SAME sequence -> "account sequence mismatch". We RETRY: dendrad re-fetches the
    # sequence each attempt (online), so once the in-flight tx is included, the retry passes. Bounded backoff (~14 s max).
    # ⛔ POSITIONALS GO AFTER `--`. The content commitment is an embedding vector whose first number is
    # negative about half the time; spliced in front of the terminator, pflag reads it as options and
    # the commit is NEVER anchored. See modea/dendrad_argv.py for the measurement and the precedent.
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

    ⛔ THE USAGE ERROR IS CHECKED FIRST, AND THE FALLBACK READS THE HEAD, NOT THE TAIL. A cobra usage
    failure carries neither `raw_log` nor `code`, so the old `o[-200:]` printed the END of the output —
    which, for an argument-parsing error, is the echoed argument itself. This log showed the embedding
    vector where the reason belonged, and that is why a permanent anchoring failure read as noise.
    """
    usage = da.cli_usage_error(o)
    if usage:
        return usage
    m = re.search(r'raw_log:\s*"?([^\n"]+)', o) or re.search(r'(code:\s*\d+[^\n]*)', o)
    return (m.group(1) if m else (o or "")[:200]).strip()[:200]


def query(sub, *positionals, flags=()):
    return run(da.dendrad_argv(("dendrad", "query", "jobs"), sub, positionals, [*flags, *_node()]))


def _tx_code(t):
    """The code a transaction response carries, in THREE states, never two:
      * the number on its `code:` line when it has one;
      * 0 when the text IS a transaction response (it names its `txhash:`) and carries no `code:` line --
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
    """(height, code) of the transaction broadcast in `o`. The height is the one it was INCLUDED at, 0 when
    it was not seen included; the code is the one READ -- at broadcast when the node refused it there, else
    the one of the included transaction -- and None when no code was read (no transaction response, or not
    found within `timeout` polls). Only a non-zero code READ is a refusal: an unknown is never one."""
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
    """The height at which the transaction broadcast in `o` was INCLUDED and accepted; 0 otherwise (not
    accepted at broadcast, not found, not included in time, or included and failed). The heartbeat keeps
    this height for an availability proof: it is what makes "proven" a reading rather than a hope."""
    h, c = tx_fate(o, timeout)
    return h if c == 0 else 0


def wait_tx(o, timeout=24):
    return wait_tx_height(o, timeout) > 0


RECOVERY_FILE = "recovery-phrase.json"


def keep_recovery_phrase(keydir, created_output: str, seal_with: str = "") -> bool:
    """Keep the recovery phrase of a key this process just CREATED, for the owner to write down.

    WHY IT IS WRITTEN AT ALL. `dendrad keys add` prints the phrase once, into this container's output,
    which nobody reads: the account and its rewards then exist with no way to recover them. The phrase is
    written next to the keyring it can rebuild, in the same volume, mode 0600 from its creation. With an
    encrypted keyring it is SEALED under the same passphrase (`seal_with`), so it never sits in clear next
    to a key that does not; with the `test` keyring it is written in clear, which adds no exposure there:
    that keyring already holds the private key in clear in the volume. The Dendra application shows it
    once, asks the owner to type three of its words back, and only then removes it from the machine.
    Nothing is written for a key that already existed: its phrase is not known here, and inventing a
    file for it would be worse than having none.

    ⛔ AN EXISTING FILE IS NEVER REPLACED. It is the phrase of an earlier key, not yet confirmed (confirming
    removes it), and that key may hold the stake: replacing it would delete the only copy of its words.
    `keys_addr` refuses to create a key while one is kept; this is the last line of that rule. The file is
    written under a temporary name, then LINKED to its name -- a link refuses an existing name."""
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
        print(f"[daemon] WARNING: {path} already holds the recovery phrase of an earlier key; it is KEPT. The "
              f"phrase of the new key {d.get('address', '')} is NOT kept: that key has no recovery phrase on "
              f"this machine.", flush=True)
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"[daemon] recovery phrase of the new key kept in {path} (mode 0600, "
          f"{'encrypted with the keyring passphrase' if seal_with else 'in clear, like the test keyring'}): "
          f"the Dendra application shows it once, then removes it once three of its words are typed back. "
          f"Write it down; it is the only way to recover this account.", flush=True)
    return True


def keys_addr(name, keydir=None):
    """The address of the key `name`, creating the key ONLY when it is ABSENT.

    ⛔ THREE STATES, NEVER TWO. An encrypted keyring answers a missing or wrong passphrase with "<name> is
    not a valid name or address" -- the words of an absent key. Reading "no address" as "no key" would
    create a NEW key in silence: a new address, a new identity, an unfunded account, while the stake stays
    on chain under the old one. `modea.keyring.key_state` calls a key absent only when dendrad says so
    WITHOUT a passphrase refusal AND the disk holds no `<name>.info`; anything else raises, and nothing
    is created."""
    kr = _keyring()
    state, val = kring.key_state(kr, name)
    if state == kring.PRESENT:
        return val
    if state != kring.ABSENT:
        raise kring.KeyringUnreadable(f"the key '{name}' cannot be read ({val}). No new key was created.")
    # ⛔ A KEPT PHRASE BLOCKS A CREATION. It is the phrase of an earlier key, never confirmed as written down,
    # and the file has ONE name: creating now would replace the only copy of words whose key may still hold
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
    # ⛔ NEVER FROM ITS STDOUT. On success `keys add --output json` prints ONE line whose last field is the
    # mnemonic; a message built from that line would carry the words into `docker logs`, the application's
    # log view and the heartbeat. The reason is read from stderr only.
    why = kring.explain(err or "") or kring.last_line(err or "") or "no message on stderr"
    raise kring.KeyringUnreadable(f"the key '{name}' could not be created and read back (dendrad exited "
                                  f"{rc}: {why}; then: {val})")


def align_identity(name, addr, owner=""):
    """Return the identifier the CHAIN will accept, renaming the keyring key to match.

    The chain derives it from the key that SIGNS create-miner: this machine's key `addr`, or in owner mode
    the OWNER's address `owner` (the key that registers the miner, which is not on this machine). The
    keyring entry still holds this machine's key; only its NAME follows the identifier.

    ⛔ WHY IT IS NOT OPTIONAL. Since the third consensus epoch, CreateMiner derives the miner id from
    the signer's address and refuses any other value. Nothing else in this kit derives it: `join.sh`
    invents an `m-<hash>` for a fresh operator, so without this alignment `create-miner` is refused,
    the node runs outside the registry, and the only symptom is "this miner will receive no job" — a
    documented install path that cannot produce a registered miner.

    ⚠️ IT RUNS BEFORE ANY FILE IS NAMED AFTER THE IDENTIFIER. The x25519 key, the attestation key and the
    VRF key all live at `<keydir>/<id>.*`. Aligning after they exist would orphan them and silently mint
    a second encryption identity — the same class of bug one layer down.

    ⚠️ AND IT NEVER GUESSES. If the derivation cannot be computed, or if the target key name is already
    taken by a DIFFERENT address, the function keeps the current name and says why: registering under a
    refused identifier is recoverable, adopting someone else's key name is not."""
    try:
        from modea.miner_id import miner_id_for_account
        attendu = miner_id_for_account(owner or addr)
    except Exception as e:
        print(f"[daemon] cannot derive the on-chain identifier ({type(e).__name__}: {e}).\n"
              f"         Keeping '{name}'. If the chain refuses the registration, this is why.", flush=True)
        return name
    if name == attendu:
        return name

    # Is the target name ALREADY taken, and by whom? Adopting a key that is not ours would hand us an
    # identity signing with a different address — every commit refused, and nothing saying why.
    show = _krun(["dendrad", "keys", "show", attendu, "-a", *_kr()])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    if m and m.group(1) != addr:
        print(f"[daemon] REFUSING to adopt the derived identifier {attendu}: a key of that name already\n"
              f"         exists in this keyring and holds a DIFFERENT address ({m.group(1)}).\n"
              f"         Keeping '{name}'. Resolve the keyring by hand before restarting.", flush=True)
        return name
    if m:
        print(f"[daemon] on-chain identity is {attendu} (key already present); adopting it.", flush=True)
        return attendu

    out = _krun(["dendrad", "keys", "rename", name, attendu, "-y", *_kr()])
    show = _krun(["dendrad", "keys", "show", attendu, "-a", *_kr()])
    if re.search(r'(dendra1[0-9a-z]+)', show or ""):
        print(f"[daemon] identity aligned with the chain: '{name}' -> {attendu}\n"
              f"         (the chain DERIVES the miner id from the signer address and refuses any other"
              + (f"; the signer of create-miner is the OWNER {owner})" if owner else ")"),
              flush=True)
        return attendu
    print(f"[daemon] could not rename the keyring key '{name}' -> '{attendu}': {str(out)[:200]}\n"
          f"         Keeping '{name}'; the registration will be refused until this is fixed.", flush=True)
    return name


_JOURNAL = "engagements-en-attente.json"


def _journal_path(keydir) -> Path:
    return Path(keydir) / _JOURNAL


def _read_journal(keydir) -> dict:
    """The register of commitments computed but not yet anchored.

    It lives next to the KEYS because it has the same lifetime as a miner identity: losing either means
    the same thing. An unreadable file yields {} -- a commitment is never guessed, and losing the
    journal falls back to the previous behaviour: degraded, never wrong."""
    try:
        with io.open(_journal_path(keydir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _journal_commitment(keydir, key: str, commit: str, pcommit: str, model_id: str = "",
                        weights_hash: str = "") -> None:
    """Makes the commitment durable BEFORE the deposit and BEFORE the anchoring.

    ATOMIC WRITE. A half-written journal would be worse than none: the resumption would read a
    truncated commitment and anchor something other than what the client can open.

    THE MODEL IS JOURNALLED WITH IT (`model_id`, `weights_hash`: the flags of the first anchoring attempt), so a
    resumption anchors the commitment with the model that ANSWERED, never with the one this process serves when
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
    except Exception as e:
        print(f"[daemon] commitment journal NOT written ({type(e).__name__}: {e}) -> an anchoring"
              f" failure on {key} would become permanent again", flush=True)


def _pending_commitment(keydir, key: str) -> dict:
    return _read_journal(keydir).get(key) or {}


def _journal_flags(entry) -> list:
    """The --model-id / --weights-hash flags a JOURNALLED commitment is anchored with: the ones journalled next
    to it, in the order the first attempt used. The resumption used to send NONE: under an armed model registry
    (enforce_model_registry) every resumed commit is refused for good, and otherwise it lands with an empty
    model_id. An entry journalled before these fields existed carries neither, and is anchored without them,
    exactly as it was then -- never with this process's MODEL_ID, which may have changed since."""
    flags = []
    if isinstance(entry, dict):
        if entry.get("model_id"):
            flags += ["--model-id", str(entry["model_id"])]
        if entry.get("weights_hash"):
            flags += ["--weights-hash", str(entry["weights_hash"])]
    return flags


def _forget_commitment(keydir, key: str) -> None:
    """Call ONLY once the chain carries the commitment: the journal is never cleared on a hope, it is
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
    except Exception:
        pass


# ── A REQUEST THAT FAILS IS REMEMBERED, NEVER REPLAYED AT EVERY PASS ──────────────────────────────────────
# The loop used to retry a request whose inference failed, or whose sealed response the relay refused, at every
# pass -- every few seconds -- for as long as the relay kept the request: a request that fails for good cost one
# generation per pass, and every request behind it waited that long more. Each failure is now written to
# JOB_FAILURES, a file of its own next to the keys. NOT the commitment journal: an entry there promises an
# anchoring, and a retry schedule must never be read as one, nor cleared with one.
#   * the next attempt waits JOB_RETRY_PAUSES_S: the first pause after the first failure, the second after the
#     second, the last after the third and every one after it;
#   * a failed INFERENCE counts towards giving the request up only when the engine answered a test request
#     AND embedded it (serving_probe) after it: a failure while the engine cannot do both is the engine's, and says nothing
#     about the request. After job_max_attempts() such failures the request is ABANDONED and never generated
#     again;
#   * a DEPOSIT the relay refused keeps its sealed response in the file, and the next attempt deposits THAT
#     response again, never a new inference: the commitment journalled with it describes that response;
#   * a refused ANCHORING on the resumption path waits the same pauses. Neither of these two is ever abandoned:
#     a journalled commitment is an obligation, kept until the relay stops listing the request.
# Entries for the requests the relay no longer lists are dropped. An unreadable file reads as empty: every request
# is then retried as it was before the file existed -- degraded, never wrong.
JOB_FAILURES = "job-failures.json"
# CHOSEN, not measured: short enough that a passing hiccup costs a minute, long enough that a request failing
# for good costs a handful of generations an hour instead of one per pass.
JOB_RETRY_PAUSES_S = (60.0, 300.0, 1800.0)
JOB_MAX_ATTEMPTS_DEFAULT = 3
_FAILURES_WARNED = {"read": False, "write": False}
_ENV_SAID: set = set()


def _env_positive(name, default, cast):
    """A positive number read from the environment; empty or unset is `default`. A value that is not a positive
    number is SAID once, and `default` is used: a typo must not silently disable what the setting tunes."""
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
    """Atomic and private (crypto.write_private). A write that fails is said once and never stops the loop: its
    cost is a request retried sooner than it should be after a restart."""
    try:
        crypto.write_private(os.path.join(keydir, JOB_FAILURES), (json.dumps(fails) + "\n").encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if not _FAILURES_WARNED["write"]:
            _FAILURES_WARNED["write"] = True
            print(f"[daemon] {keydir}/{JOB_FAILURES} could not be written ({type(e).__name__}): after a restart, "
                  f"a paused request may be retried before its pause is over. Said once.", flush=True)


def note_job_failure(fails, key, kind, why, now, counted=False, sealed=None) -> dict:
    """Records ONE failure of `kind` -- "inference" | "deposit" | "anchor" -- for the request `key` in `fails`
    (the file's content; the caller writes it) and returns the entry. A failure of another kind than the
    entry's starts the entry over: an inference that now succeeds and a deposit that fails are a new story.
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


# ── THE ENGINE'S LATEST ANSWER TO A TEST REQUEST ─────────────────────────────────────────────────────────
# Every test inference this process runs -- the one an availability proof waits for, the one a failed request
# asks for -- leaves its time here, as answered or not. A failed inference counts against its REQUEST only when
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
    otherwise ONE test request is run now. A probe that cannot run is not an answer."""
    if _ENGINE["answered_at"] >= t:
        return True
    if _ENGINE["unanswered_at"] >= t:
        return False
    st, _, _ = recorded_probe(probe)()
    return st == PROBE_ANSWERED


# ── A REQUEST NOBODY WAITS FOR ANY MORE IS NOT SERVED, AND THE NEWEST ARE SERVED FIRST ───────────────────────
# After an outage the loop used to serve the queue in the relay's insertion order, oldest first: answers landed
# thousands of blocks after their client had given up, while the fresh requests waited behind them. The keys are
# now walked newest first, and a NEW request older than request_max_age_s() is not served. Its age is read in its
# identifier, `job<milliseconds since the epoch>` (client.submit_job); an identifier of another form has
# no age that can be read, and an unknown age is SERVED -- after the requests whose age is known. The filter never
# applies to a request whose commitment is journalled: that request was served, and only its anchoring is left.
REQUEST_MAX_AGE_S_DEFAULT = 600.0
_RE_JOB_MS = re.compile(r"^job(\d{13})$")


def request_max_age_s() -> float:
    """The age past which a NEW request is not served (DENDRA_REQUEST_MAX_AGE_S). The default is CHOSEN, not
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


# ── TWO CLOCKS JUDGE A REQUEST STALE, NEVER ONE ─────────────────────────────────────────────────────────────────
# The age compares the client's clock (the identifier) with THIS machine's. A machine whose clock runs ahead of the
# real time by more than request_max_age_s() would call every fresh request stale -- and still prove its presence,
# so the chain would draw a miner that serves nothing. A request this machine's clock calls stale is therefore
# judged again against the CHAIN's clock (chain_now): the time of the latest block, carried forward between two
# readings by the monotonic clock, which nothing sets. The younger age decides, so a second clock can only make
# the filter serve MORE: a chain that stalls, or a node that is catching up, reads as an old time and lets every
# request through, as before the filter existed. While no block time was ever read, this machine's clock decides
# alone. NOT covered: a CLIENT clock behind the real time -- its requests carry the time it wrote in them.
CHAIN_CLOCK_READ_S = 30.0   # CHOSEN: at most one `dendrad status` per this many seconds, only while a request is judged
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
    """The age at which the NEW request `jid` is judged STALE -- not served -- or None when it is served: older than
    request_max_age_s() by this machine's clock (`now`) AND, once the chain's clock has been read (`chain_clock`,
    chain_now, asked only here), by the chain's. A chain's clock that calls it fresh is SAID once, with the gap."""
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
    """udndr held by `addr`: the amount READ; 0 when the answer was read and names no udndr (proto3 omits an empty
    balance); None when nothing was read. A query that failed is never an empty account: read as one, it asked
    the faucet again for an address whose balance only could not be read."""
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
    """Address of the OPERATOR registered for this miner_id ("" if the miner does not exist)."""
    m = re.search(r'operator:\s*"?(dendra1[0-9a-z]+)"?', query("get-miner", mid))
    return m.group(1) if m else ""


PRESENT, ABSENT, UNREAD = "present", "absent", "unread"


def registry_record(mid):
    """(state, record) of `mid` in the chain's miner registry, three states, never two:
      ("present", {"creator", "operator", "region", "stake", "enc_pubkey", "vrf_pubkey"}) -- a record was READ;
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
    """The vrf_pubkey ANCHORED on-chain for this miner: the key READ, "" when the record carries none (the zero
    value of a string, which proto3 omits), or None when no record was read -- the registry did not answer, or
    does not hold this miner yet.

    ⚠️ Read from the chain, never assumed from what this process holds locally. A key generated here is
    not a key the chain knows: anchoring happens at registration, and a miner already in the registry
    keeps whatever it registered with until an explicit rotation.
    ⛔ NONE IS NEVER "NO KEY". This used to search the text of a query and return "" when it found nothing --
    a node that did not answer included -- and main() then announced "NO VRF KEY ANCHORED ON-CHAIN" for a key
    nobody had read."""
    st, rec = registry_record(mid)
    return rec.get("vrf_pubkey", "") if st == PRESENT else None


SUBSIDY_MIN = int(os.environ.get("DENDRA_SUBSIDY_MIN", "50000"))     # claim from 0.05 DNDR up


def claimable_subsidy(mid):
    """udndr this miner may claim now, or None when it could not be read.

    The cap is the chain's (`msg_server_claim_subsidy.go`): demand x work_gate_bps / 10000, minus what was
    already claimed. Rule of zero: inside an answer that was READ, an absent field is 0; a query that
    failed is None, and None never becomes a claim."""
    try:
        m = json.loads(query("get-miner", mid, flags=("--output", "json")))
        p = json.loads(query("params", flags=("--output", "json")))
    except (ValueError, TypeError):
        return None
    rec, par = m.get("miner"), p.get("params")
    if not isinstance(rec, dict) or not isinstance(par, dict):
        return None
    cap = int(rec.get("demand", 0) or 0) * int(par.get("work_gate_bps", 0) or 0) // 10000
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
    return f"subsidy claim of up to {c} udndr {'confirmed' if ok else 'NOT confirmed: ' + _tx_err(out)}"


def _params_doc(out):
    """The jobs params document dendrad printed, as a dict, or None when `out` is not one. It is read in the
    form asked for (`--output json`), or in the CLI's text form of the same document: its FIRST line is
    `params:` and EVERY line after it one indented `key: value`. Anything else -- a nested field the text form
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
    """min_stake READ FROM THE CHAIN, never a local constant: the value read, 0 when the params document was read
    and carries none (proto3 omits a zero), None when it was not read.

    `min_stake` is a GOVERNABLE parameter: any value hardcoded here diverges as soon as a vote changes
    it. A local default below the real min_stake makes `create-miner` REJECTED on every attempt; if the
    failure is not printed, the miner announces "ready / waiting for jobs" while NOT existing in the
    registry, and the participant has no way to understand — the GPU runs, the chain ignores it.
    ⛔ AND THERE IS NO DEFAULT AT ALL. One used to stand in for a failed read, twenty times below the chain's
    own value: a registration sent with it was refused, and in owner mode the owner was handed a create-miner to
    sign offline for nothing. Unread is "send nothing, read again later" (register_attempt, owner_wait).
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
    """PoW difficulty ANNOUNCED by the faucet (GET /), or None if the probe fails.

    None is not 0: `0` means "the faucet declares the PoW disarmed", None means "unknown". Conflating
    the two would post a request without a token while believing the contract was honoured.
    """
    try:
        with urllib.request.urlopen(faucet, timeout=10) as r:
            return int(json.loads(r.read()).get("pow_bits", 0))
    except Exception:
        return None


# WHY THE FAUCET REFUSED, AS ONE WORD. A refusal of the faucet is one of three anti-abuse caps (a 429 whose
# JSON `info` is one of faucet's REFUSAL_* constants), a proof of work it did not accept (a 400 that
# names `pow_bits`), another HTTP answer, or no answer at all. The `info` is PARSED and compared with the
# faucet's OWN constants (this module imports it), never matched as text and never copied here: a server
# whose `info` matches none of them is `unknown` -- never `ip_quota` by default, since that word stops
# deploy/join.sh from starting the next identities of a rig.
FAUCET_REASONS = {"ip_quota", "addr_cooldown", "global_cap", "pow", "transport", "unknown"}  # plus http_<code>


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
    if code == 400 and "pow_bits" in doc:
        return "pow"
    return f"http_{int(code)}" if isinstance(code, int) else "unknown"


def _faucet_post(faucet, addr, nonce):
    """Post the request. Returns (ok, detail, required_bits, reason) — required_bits = 0 when the server
    names none; reason is "" on success, else a word of faucet_reason.

    The body of a refusal is READ: the faucet names the cause and, on a PoW refusal, the expected
    difficulty. Discarding it would make the failure undiagnosable from the miner's machine.
    """
    body = {"address": addr}
    if nonce:
        body["pow"] = nonce
    req = urllib.request.Request(faucet, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True, "", 0, ""
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        bits = 0
        try:
            bits = int(json.loads(raw).get("pow_bits", 0))
        except Exception:
            pass
        return False, f"HTTP {e.code} {raw}".strip(), bits, faucet_reason(e.code, raw)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", 0, "transport"


def faucet_fund_classified(faucet, addr):
    """Fund the address at the faucet. Returns (ok, detail, reason) — a failure is ALWAYS named, and classed.

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
    def _tick(essais, elapsed):
        print(f"[daemon] faucet PoW ({bits} bits): {essais} attempts, {elapsed:.0f} s ...", flush=True)
    print(f"[daemon] faucet PoW required: {bits} bits bound to {addr} -> solving "
          f"(bounded at {POW_MAX_S:.0f} s, tunable via DENDRA_FAUCET_POW_MAX_S).", flush=True)
    t0 = time.time()
    nonce = faucet_pow.solve_pow(addr, bits, deadline_s=POW_MAX_S, progress=_tick)
    if nonce:
        print(f"[daemon] PoW solved in {time.time() - t0:.1f} s.", flush=True)
    return nonce


def pick_backend(want):
    """Chooses the backend. We NEVER serve a mock SILENTLY in prod.
    A miner without a GPU would collect rewards for bogus text, anchored on-chain as real
    (and N deterministic mocks agree -> majority -> evict/slash the real miners).
    Mock fallback/use is allowed ONLY if DENDRA_ALLOW_MOCK=1 (tests); otherwise HARD FAILURE."""
    allow_mock = os.environ.get("DENDRA_ALLOW_MOCK", "0") == "1"
    if want == "mock":
        if not allow_mock:
            print("[daemon] FATAL: backend 'mock' requested but DENDRA_ALLOW_MOCK!=1 (anti fake-miner guard).")
            sys.exit(3)
        print("[daemon] explicit MOCK backend (tests) -> NOT intended for production")
        return "mock"
    if want == "ollama":
        try:
            m = Miner("probe", backend="ollama")
            m.backend.generate("ok")
            # Where it runs (a GPU, or the CPU for the judge role only) is measured before this process starts:
            # serve_guard.py, called by docker/entrypoint-services.sh. Reachable says nothing about it.
            print(f"[daemon] Ollama reachable ({m.backend.endpoint}) -> REAL LLM")
            return "ollama"
        except Exception as e:
            if allow_mock:
                print(f"[daemon] Ollama unreachable ({type(e).__name__}) -> MOCK fallback (DENDRA_ALLOW_MOCK=1)")
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

    ⚠️ ("","") IS NOT A GRACEFUL DEGRADATION. Under verification_mode=1 -- the incentivised configuration
    -- ADR-022 closes the GPU-less echo: a miner with no anchored vrf_pubkey has ProveAvailability refused
    (ErrUnauthorized) and is skipped at payout. The echo survives only under verification_mode=0. So
    ("","") means "this miner will never prove availability", and the caller must SAY so rather than
    register quietly without a key.

    THE SECRET IS WRITTEN IN THE KEY ENVELOPE (crypto.store_secret): sealed under the keyring's passphrase
    in `file` mode, created 0600 by os.open -- it used to be written under the process umask and chmod'ed
    afterwards, readable by others for that instant. A secret found in clear while a passphrase is there to
    seal it is re-encrypted in place. A sealed secret that does not open RAISES: a VRF key that exists and
    cannot be read is not a missing one, and registering as if it were would anchor nothing."""
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
            # THE KEY GOES THROUGH THE ENVIRONMENT, NEVER argv: /proc/<pid>/cmdline and `ps` are
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
              f"This miner will register WITHOUT a vrf_pubkey.")
        return "", ""
    # Reached when the binary RAN but produced key material of the wrong size. Returning here without a
    # word is the worse of the two failures: an operator who sees no message assumes there was nothing to
    # see. A component that degrades must say it degraded.
    print("[daemon] VRF key material has an unexpected size -> registering WITHOUT a vrf_pubkey.")
    return "", ""


# ── THE AVAILABILITY WINDOW: settled once, said once, remembered across restarts ───────────────────────
# A challenge is answered within `avail_deadline_blocks` of its epoch's first block (chain/x/jobs/keeper/
# msg_server_prove_availability.go, ProveAvailability: refused when h > rollH + avail_deadline_blocks, rollH
# the epoch's first block). The daemon used to send its proof whatever the height, read "too late", keep the
# challenge as NOT proven, and send it again at the next tick -- one "REFUSED ... too late" line every few
# seconds until the next epoch, and again after every restart, since the challenge it had proven or missed
# lived in memory only. Now: the height is compared with the deadline BEFORE sending, a closed window is said
# in ONE line naming the block of the next challenge, and the challenge it settled (proven, or its window
# closed) is written to the key directory, so a restart neither proves it again nor repeats the line.
AVAIL_RECORD = "availability-last.json"
_AVAIL_RECORD_WARNED = False


def avail_window(epoch, eb, deadline, height):
    """(state, next_challenge_block). THREE answers and one more, never a guess:
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
    NEVER raises."""
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
    NEVER raises: a reading that fails is a value, "not read", and the proof is then left to the chain."""
    si = _node_sync_info()
    try:
        return int(si["latest_block_height"]) if si is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _avail_deadline():
    """avail_deadline_blocks READ from the jobs params: 0 when the answer omits it (proto3, zero value: no
    deadline), None when the params were not read. NEVER raises."""
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
    """True when the node shows the transaction `txhash` INCLUDED (a height) and ACCEPTED (code 0) on the chain
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
    """Writes the settled challenge. A failed write is SAID once and never stops the loop: its cost is a
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
    print(f"[daemon] {mid} availability window CLOSED for challenge {chal[:12]}... ({why}): no proof is sent "
          f"for it; next challenge at block {nxt if nxt is not None else '? (avail_epoch_blocks not read)'}.",
          flush=True)


# ── PRESENCE IS PROVEN BY A MODEL THAT ANSWERS ──────────────────────────────────────────────────────────
# The chain draws a request's primary among the miners PRESENT (an accepted availability proof), and a proof
# says nothing about the model: a miner whose engine lost its model, or never pulled it, proved its presence
# all the same, was drawn, and the request expired unanswered. So a proof is sent only right after a TEST
# INFERENCE on the served model answered, within a bound. Three states, and only the first proves anything:
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
# THE BOUND. A test answer is a few tokens, so it is given much less time than a request: the client of the
# Final Testnet Season waits for a WHOLE answer (client.py::quick_metered), and a model that cannot
# return a few tokens within this bound cannot serve that request either. DENDRA_PRESENCE_PROBE_S sets it; a
# value that is not a positive number is said and replaced by the default.
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
    """One TEST INFERENCE on the served model: (state, why, seconds). Never raises. Nothing of the prompt or of
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
    """`probe` (presence_probe's triple), then ONE embedding of PRESENCE_PROBE_PROMPT through `embed` -- the
    function a request's answer is embedded with (modea/miner.py::answer_embedding, on the served backend). A
    request is answered AND embedded before its commitment exists (modea/miner.py::Miner.handle_job), so an engine
    that answers but cannot embed serves nothing: a generation alone used to count as "the engine answered", and
    with the embedder down every request was given up as failing by itself, while availability went on being
    proven by a miner that could commit nothing. An embedding that raises or comes back empty is SILENT. Its time
    is bounded by the engine's own embedding call, not by the probe's bound. Never raises."""
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


def prove_availability_once(mid, vsk, last_chal="", keydir=None, probe=None):
    """Best-effort: if availability is ON (challenge present) and new, proves presence with a VRF PROOF --
    only once a test inference on the served model has just answered (`probe`, see presence_probe; without one
    nothing is sent). Never raises (must not disturb the inference loop). Returns the SETTLED challenge --
    proven, or whose window is closed -- or last_chal. `keydir` holds the record that survives a restart
    (AVAIL_RECORD)."""
    if not vsk:
        return last_chal
    try:
        out = query("get-avail-challenge", flags=("--output", "json"))
        # THREE STATES, NOT TWO. A query that FAILS (node down, wrong --node, subcommand renamed)
        # prints text no JSON parser accepts; a pattern search over that text finds nothing and yields
        # the same empty string as a chain that answers with availability disarmed. One of the two
        # needs an operator, the other needs nothing, so they are told apart before anything is read.
        try:
            reponse = json.loads(out)
        except Exception:
            print(f"[daemon] {mid} availability challenge UNREADABLE (query failed): "
                  f"{str(out).strip()[:200]}", flush=True)
            _status_write(presence={"at": int(time.time()), "result": "challenge unreadable",
                                    "why": str(out).strip()[:200]})
            return last_chal
        # proto3 omits a string at its zero value, so an absent `challenge` IS the empty challenge:
        # availability is off. That default is only safe because the failing query is ruled out above.
        chal = reponse.get("challenge", "")
        if not chal or chal == last_chal:
            return last_chal   # availability OFF, or challenge already settled by this process
        # SETTLED BEFORE THIS PROCESS (the record in the key directory): proven, or its window closed and
        # already said. Neither is sent again nor said again.
        # ⛔ "PROVEN" IS TRUSTED ONLY WHILE ITS TRANSACTION IS ON THIS CHAIN. The record is keyed by the
        # challenge's text, and a rescue by export (dendra_chain_rescue.sh: same chain-id, same numbering)
        # carries AvailChallenge over but deliberately not `Available` (keeper/genesis.go): the same challenge,
        # and no proof recorded for it. Trusted on its text alone, the record made every miner skip its proof
        # for that epoch. A proof sent twice costs one transaction (Available is a set), a proof skipped costs
        # the epoch: so a record whose transaction this chain does not show is proven again.
        rec = _avail_record(keydir)
        if rec.get("challenge") == chal:
            if rec.get("state") == "window_closed":
                return chal
            if rec.get("state") == "proven" and _tx_on_chain(rec.get("txhash")):
                return chal
        # Read, so zero is a value (proto3 omits it): epoch 0 is the first epoch, avail_epoch_blocks 0 is off.
        try:
            epoch = int(reponse.get("epoch") or 0)
            eb = int(reponse.get("avail_epoch_blocks") or 0)
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
        # THE MODEL ANSWERS, OR NO PRESENCE (see presence_probe). Right before the proof, never cached: a proof
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
                print(f"[daemon] {mid} availability NOT proven for challenge {chal[:12]}...: the served model did "
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
            # THE PROOF IS WHAT THE CHAIN RECORDED, NOT WHAT WE SENT, so the transaction is judged
            # before anything is announced. The caller stores whatever this returns as `_last_chal`,
            # and the guard above treats a challenge equal to it as already proven: announcing success
            # without reading the answer would cost the availability share for the whole challenge
            # window, with nothing left to retry and a log asserting the opposite. On failure the OLD
            # challenge is kept, so the next tick tries again. This is the same judgement the file
            # applies elsewhere (`wait_tx(tx_from(...))` for create-miner, `wait_tx(out)` for
            # create-commit).
            out = tx_from(mid, "prove-availability", mid, chal, pi)
            # THE HEARTBEAT FOLLOWS THE SAME JUDGEMENT: it records "proven" only with the height
            # `wait_tx` read, so the file never says more than the chain answered.
            h = wait_tx_height(out)
            if not h:
                why = _tx_err(out)
                # THE CHAIN'S OWN "too late" (ProveAvailability, see AVAIL_RECORD): reached when the height or
                # the deadline could not be read before sending. The window is closed for this challenge:
                # retrying it every tick only repeats the refusal until the next epoch.
                if "proof too late" in why:
                    _window_closed(mid, keydir, chal, epoch, nxt if eb > 0 else None, f"the chain said: {why}")
                    return chal
                _status_write(presence={"at": int(time.time()), "result": "refused",
                                        "challenge": chal[:12], "why": why})
                print(f"[daemon] {mid} availability proof REFUSED for challenge {chal[:12]}... "
                      f"({why}) -> NOT marked proven, will retry")
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
    """Says why the keys cannot be opened, records it in the heartbeat, and EXITS (4). The container's
    restart policy brings the daemon back, which reads the keyring again: once the cause is fixed, it
    starts; until then, it says the same thing and creates nothing."""
    hint = getattr(e, "hint", "")
    print(f"[daemon] KEYS NOT OPENED -- {e}\n"
          f"         The miner stops here and creates NO new key: a new key would be a new identity, with no\n"
          f"         funds and no registration, while the stake stays on chain under the one it cannot open.\n"
          + (f"         {hint}\n" if hint else ""), flush=True)
    _status_write(phase="keys not opened", keys_error=str(e)[:300])
    sys.exit(4)


# ── OWNER MODE (deploy/join.sh --owner) ─────────────────────────────────────────────────────────────
# The miner is registered by its OWNER, a key that is not on this machine: the identifier derives from the
# owner's address, the bond is the owner's, and only the owner can update or delete the miner and get the
# bond back. This machine's key OPERATES it -- commits, availability proofs, subsidy claims, key rotations --
# and is the one the chain pays (chain/x/jobs/keeper/owner_operator_split_test.go fixes both halves).
# ⚠️ WHAT IT PROTECTS: the CAPITAL. A copy of this machine's key still takes the income (payments and subsidy
# go to the operator) and can get the bond slashed by bad commits; it can no longer withdraw the bond.
# The daemon NEVER sends create-miner in this mode: it prepares it, prints the three commands that register
# the miner (modea/owner_tx.py), and mines only once the chain records this machine's key as the operator.
OWNER = os.environ.get("DENDRA_MINER_OWNER", "").strip()
OWNER_POLL_S = float(os.environ.get("DENDRA_OWNER_POLL_S", "30"))
OWNER_REPRINT_S = float(os.environ.get("DENDRA_OWNER_REPRINT_S", "1800"))
OWNER_TX_FILE = {"create-miner": "owner-create-miner.json", "update-miner": "owner-update-miner.json"}


class OwnerModeError(RuntimeError):
    """Owner mode cannot run as configured. Said, recorded, and the daemon stops (exit 5) BEFORE renaming,
    creating or registering anything: a wrong owner is another identity."""


def _stop_on_owner(e) -> None:
    print(f"[daemon] OWNER MODE NOT STARTED -- {e}", flush=True)
    _status_write(phase="owner mode not started", owner_error=str(e)[:300])
    sys.exit(5)


def decide_owner(mid, addr, configured=None, read=None) -> str:
    """The owner this miner runs under ("" = it registers itself), or OwnerModeError.

    `mid` is the identity this start resumed (a `dm1...` read from the volume) or the name passed in, `addr`
    this machine's key. The REGISTRY decides when the volume and the setting disagree, because a miner that
    exists on chain is a fact and a setting is a wish:
      · DENDRA_MINER_OWNER set, and the volume already holds ANOTHER identity that the chain registers: the
        owner would be a new identity -- refused, with the way out (leave, or drop the setting);
      · DENDRA_MINER_OWNER missing, and the volume's identity is registered on chain BY ANOTHER KEY with this
        machine's key as operator: that IS an owner-mode miner whose setting was lost -- kept, and said;
      · DENDRA_MINER_OWNER missing, and the volume's identity -- not derived from this machine's key -- is NOT
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
                f"this volume's miner {mid} is REGISTERED (owner {rec['creator'] or '?'}, operator "
                f"{rec['operator'] or '?'}), and DENDRA_MINER_OWNER={configured} is ANOTHER identity ({want}). "
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
        # ⛔ ABSENT IS NOT "MINE". An identity that is not derived from this machine's key and that the chain
        # does not register is, as far as anything here can tell, an owner-mode miner whose owner has not
        # signed the registration yet (or has left) and whose setting was lost -- a re-run of join.sh after
        # uninstall, a hand-edited .env. Going on would RENAME the key to this machine's own identity and
        # register it, staking with the very key owner mode keeps the stake off; getting the owner's identity
        # back would then take an exit. Nothing is renamed or registered: the two ways out are named.
        if state == ABSENT:
            raise OwnerModeError(
                f"this volume's identity {mid} is not derived from this machine's key {addr}, and the chain does "
                f"not register it -- the identity of an owner-mode miner its owner has not registered yet, with "
                f"DENDRA_MINER_OWNER missing from deploy/testnet-miner/.env, reads exactly like this. Nothing was "
                f"renamed, created or registered. Either write the owner back (DENDRA_MINER_OWNER=<owner address> in "
                f"deploy/testnet-miner/.env, or bash deploy/join.sh --owner <owner address>), or, to give that "
                f"identity up and let this machine's key register a miner of its own, remove the file "
                f"identite-resolue from the miner's key directory and start again.")
        cr, op = rec.get("creator", ""), rec.get("operator", "")
        if state == PRESENT and op.lower() == str(addr).lower() and cr and cr.lower() != str(addr).lower():
            print(f"[daemon] OWNER MODE, READ FROM THE CHAIN: {mid} is owned by {cr} and operated by this "
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
    which this machine mines. NEVER sends create-miner. While the miner is absent it funds what the faucet
    can fund (this machine's key needs an account to sign anything; the owner needs the stake), prepares the
    registration and prints its three commands; while another operator is recorded it prints the owner's
    update-miner. With --once it returns False after one pass instead of waiting.

    A READING THAT FAILED (the registry, or min_stake) prepares nothing, so it does not move `printed_at`: the next
    pass, OWNER_POLL_S later, reads again and prepares as soon as both are read. It used to move it, and a single
    failed reading put the preparation off by OWNER_REPRINT_S while the log announced OWNER_POLL_S. What a failed
    reading prints is said at most once per OWNER_REPRINT_S (`unread_said_at`), not at every pass."""
    printed_at, funded, prepared, unread_said_at = 0.0, set(), None, 0.0
    while True:
        state, rec = registry_record(a.id)
        if state == PRESENT and rec["operator"].lower() == addr.lower():
            if rec["creator"].lower() != owner.lower():
                print(f"[daemon] NOTE: {a.id} is registered by {rec['creator']}, not by DENDRA_MINER_OWNER={owner}. "
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
            # min_stake NOT READ: nothing is funded and nothing is prepared. A create-miner prepared with a stake
            # nobody read is a transaction the owner signs offline for nothing. `printed_at` stays: read again at
            # the next pass.
            if not unread_said_at or time.time() - unread_said_at >= OWNER_REPRINT_S:
                unread_said_at = time.time()
                print(f"[daemon] OWNER MODE -- the chain's min_stake could not be read: the registration of {a.id} "
                      f"is not prepared; read again in {OWNER_POLL_S:.0f} s.", flush=True)
        elif state == ABSENT and due:
            printed_at = time.time()
            # `stake` was read by the branch above (min_stake read).
            # Each address is asked for ONCE per start: the faucet has a per-address cooldown, and a refusal
            # repeated every pass would only fill the log. A balance that could not be READ (None) asks nothing:
            # it is not an empty account, and the address is asked about again at the next due pass.
            for who, need in ((addr, 1), (owner, stake)):
                held = bal_token(who)
                if who in funded or held is None or held >= need:
                    continue
                funded.add(who)
                fok, fwhy = faucet_fund(a.faucet, who)
                if not fok:
                    print(f"[daemon] FAUCET REFUSED ({a.faucet}) for {who}: {fwhy}", flush=True)
                for _ in range(20):
                    held = bal_token(who)
                    if not fok or (held is not None and held >= need):
                        break
                    sleep(2)
            # The VRF key is anchored AT REGISTRATION, as a flag: without it the miner can never prove its
            # availability, and the omission is said -- as the self-registering path says it.
            ok, text = owner_prepare(a.keydir, "create-miner", (a.id, addr, "eu", stake, mypub), owner, a.id,
                                     extra=("--vrf-pubkey", vpk) if vpk else ())
            if not vpk:
                text += ("\n         WARNING: prepared WITHOUT a VRF key (dendra-vrf not found in this image): this miner will"
                         "\n         prove no availability until a key is anchored with rotate-miner-keys.")
            print(f"[daemon] OWNER MODE -- {a.id} is registered by its OWNER, not by this machine.\n"
                  f"         owner    : {owner}  (pays the stake, alone updates or deletes the miner, gets the stake back)\n"
                  f"         operator : {addr}  (this machine's key: signs the commits, proofs and claims, and is paid)\n"
                  f"         This machine does not hold the owner key, so it does not send create-miner.\n{text}\n"
                  f"         This miner WAITS: it mines once the chain records {addr} as the operator of {a.id}.",
                  flush=True)
            if bal_token(addr) == 0:
                print(f"[daemon] this machine's key {addr} has no funds, so no account on chain: it cannot sign a "
                      f"commit until it receives some (any amount) -- the faucet above, or a transfer.", flush=True)
            prepared = ok
        elif state == UNREAD and (not unread_said_at or time.time() - unread_said_at >= OWNER_REPRINT_S):
            # NOT READ: nothing is prepared, and `printed_at` stays -- read again at the next pass.
            unread_said_at = time.time()
            print(f"[daemon] OWNER MODE -- the registration of {a.id} could not be read ({rec.get('why')}); "
                  f"read again in {OWNER_POLL_S:.0f} s.", flush=True)
        # THE HEARTBEAT MOVES ON EVERY PASS: a miner waiting for its owner is waiting, not stuck, and the
        # container's healthcheck reads only the age of this file.
        _status_write(phase={PRESENT: "owner mode: operator mismatch", ABSENT: "awaiting the owner's registration",
                             UNREAD: "owner mode: registry unread"}[state],
                      owner=owner, operator=rec.get("operator", ""), prepared=prepared)
        if a.once:
            return False
        sleep(OWNER_POLL_S)


def _registration_stake():
    """The stake a registration carries: DENDRA_MINER_STAKE when set and not below the chain's min_stake,
    else min_stake READ FROM THE CHAIN (a governed parameter). None when min_stake was not read: no stake is
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
              f"would be REJECTED. Using {_min}.", flush=True)
        want = _min
    return want


PAYOUT_ADDRESS = os.environ.get("DENDRA_PAYOUT_ADDRESS", "").strip()
PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "").strip().rstrip("/")


def maybe_declare_payout(keydir, mid, operator="", owner="") -> tuple:
    """(settled, message). Declares DENDRA_PAYOUT_ADDRESS to the Final Testnet Season programme once this
    miner is registered, and again only when that address CHANGES: the record of what was declared, and
    from which setting, lives in the miner's volume (modea.keyring.PAYOUT_RECORD), next to the identity
    it is about. A declaration made since from the Dendra application is not overwritten while the
    setting stays the same. `settled` is True when nothing is left to do.

    In OWNER MODE the programme accepts the declaration from the owner's key only, which is not here: the
    daemon does not sign it, it prints the three steps (final_season_miner.py payout-prepare, the owner's
    signature, payout-submit) once per start, until the record shows the setting declared."""
    addr = PAYOUT_ADDRESS
    if not addr:
        return True, ""
    import final_season_miner as fsm
    from final_season_address import payable_address
    bad = payable_address(addr)
    if bad:
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is NOT declared: {bad}. The season pays this machine's own "
                      f"key until a payable address is declared (deploy/join.sh --payout-address).")
    if not PROGRAMME:
        return True, ("DENDRA_PAYOUT_ADDRESS is set but DENDRA_FINAL_SEASON_URL is empty: there is no programme "
                      "to declare it to.")
    rec = kring.payout_record(keydir) or {}
    # DENDRA_PAYOUT_LOCK=1 (set by docker/cloud-start.sh on a rented pod): the declaration asks the programme to
    # LOCK the address, so a host that reads this machine's key cannot redirect the rewards afterwards.
    lock = os.environ.get("DENDRA_PAYOUT_LOCK", "") == "1"
    if rec.get("miner_id") == mid and str(rec.get("setting") or "").lower() == addr.lower():
        # With the lock asked for, only a record of THIS address that the programme answered LOCKED is settled.
        # A record without it -- an earlier declaration made without the lock, by a programme that does not
        # lock, or on a volume that comes from another machine -- is declared again, with the lock. A programme
        # that locks only an identity's FIRST declaration refuses it (409, "lock_refused"), said below.
        if not lock or (rec.get("locked") is True and str(rec.get("address") or "").lower() == addr.lower()):
            return True, ""
    if owner and owner.lower() != str(operator).lower():
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is NOT declared yet, and this machine cannot declare it: "
                      f"{mid} is owned by {owner}, and the programme accepts a declaration from the owner's key only.\n"
                      f"         1. docker compose -p dendra-miner exec -T miner python3 final_season_miner.py "
                      f"payout-prepare --address {addr} > payout.json\n"
                      f"         2. on the owner's machine, the dendrad tx sign command payout-prepare prints\n"
                      f"         3. docker compose -p dendra-miner exec -T miner python3 final_season_miner.py "
                      f"payout-submit < payout.signed.json\n"
                      f"         Until then the season pays this machine's key, the miner's operator.")
    if lock and operator and addr.lower() == str(operator).lower():
        # The lock would hold, for good, the address of the very key a host can read: nothing is sent.
        return True, (f"DENDRA_PAYOUT_ADDRESS={addr} is NOT declared and NOT locked: it is this machine's OWN key "
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
        # The record says what the programme ANSWERED: an accepted declaration without "locked": true is
        # recorded without it, never as locked on the strength of the request.
        fsm.record_declaration(keydir, mid, addr, source="daemon", setting=addr, locked=locked)
        if lock and not locked:
            # Asked for the lock and answered without it: a programme that does not lock (one deployed before
            # the lock existed). The address is filed, and a copy of this machine's key could still change it,
            # so this is not settled: it is said, and the declaration is made again, with the lock.
            return False, (f"payout declaration of {addr} for {mid} accepted WITHOUT the lock it asked for (the "
                           f"programme's answer carries no \"locked\": true): a copy of this machine's key could still "
                           f"change the address. Declared again with the lock in 30 minutes (a programme that locks "
                           f"only an identity's first declaration will refuse it, this one being filed).")
        return True, f"Final Testnet Season rewards of {mid} declared to {addr}{' and LOCKED' if locked else ''}{note}."
    if code == 409:
        # FINAL, in both of its forms: retrying every half hour would change nothing, so it is said once, loudly,
        # and not retried. The answer's own field says which form it is (read, never matched as text).
        if (r or {}).get("lock_refused") is True:
            # The lock is granted only on an identity's FIRST declaration (or from its owner's key), and this
            # identity has declared before without it. Nothing was filed: the declaration before stands. The
            # refusal is RECORDED, as the programme answered it: the payout record says nothing (nothing was
            # filed), and a pod's watch (docker/cloud-start.sh) that read only that record would wait for good.
            fsm.record_lock_refusal(keydir, mid, addr)
            return True, (f"payout declaration of {addr} for {mid} REFUSED for good WITH THE LOCK: the programme locks "
                          f"an address only on an identity's first declaration, and {mid} has declared before without "
                          f"the lock. Nothing was filed: the season pays the address declared before (this machine's "
                          f"key if none), and a copy of this machine's key can still change it. Only a new identity, "
                          f"whose first declaration carries the lock, is paid at a locked address "
                          f"({str((r or {}).get('error') or r)[:200]}).")
        # The programme holds a locked address for this identity and refuses any other.
        return True, (f"payout declaration of {addr} REFUSED for good: the programme holds a LOCKED payout address "
                      f"for {mid} and refuses any other ({str((r or {}).get('error') or r)[:200]}). The season pays "
                      f"the locked address.")
    return False, (f"payout declaration of {addr} NOT accepted yet ({code or 'no answer'}: "
                   f"{str((r or {}).get('error') or r)[:200]}); retried in 30 minutes.")


# ── THE REGISTRATION, AS AN ATTEMPT THAT IS REPLAYED ─────────────────────────────────────────────────
# It used to be tried ONCE, at start: a faucet that refused (its daily quota per IP is reached fast when
# several identities share one address -- one identity per card, a rented box behind a shared IP) left the
# miner running, unregistered, for good, while it printed "ready". Now the attempt is a function the loop
# replays, guarded by registry_record -- three states, never stake_of: stake_of reads -1 both when the query
# fails and when the stake is omitted (proto3, a miner slashed to zero), and it would send the faucet and a
# create-miner to a node that does not answer, or for a miner that exists.
#   PRESENT -> nothing to do (even at stake 0: that miner EXISTS, and create-miner would be refused);
#   ABSENT  -> the faucet, then create-miner, as at start before;
#   UNREAD  -> NOTHING: a node that does not answer is never "not registered". Read again later.
# The state goes into the heartbeat (`registration`), which deploy/join.sh reads to stop starting the next
# identities of a rig at the first IP-quota refusal, and which miner_selftest.py and the rig tools show.
REGISTERED, DEFERRED, REFUSED = "registered", "deferred", "refused"
# CHOSEN, not measured: each attempt costs a proof of work (bounded by DENDRA_FAUCET_POW_MAX_S) and one
# request, and the faucet records only a drip it PAID (faucet._rate_ok), so a refused attempt spends
# no quota. Ten minutes keeps that cost small and still registers within the hour a quota frees up.
# An EMPTY value is unset (deploy/testnet-miner/docker-compose.yml forwards it empty when the .env names none).
REGISTER_RETRY_S = float(os.environ.get("DENDRA_REGISTER_RETRY_S", "").strip() or "600")
# An address in cooldown is refused until the faucet's own cooldown (DENDRA_FAUCET_ADDR_COOLDOWN, on the
# faucet's side, not readable here) has passed: this daemon waits its cadence times this factor instead of
# asking every ten minutes for a drip it cannot get. A chosen factor, written as one.
ADDR_COOLDOWN_RETRY_FACTOR = 6
# The ONE human line a deferred registration prints. Neither "create-miner" nor "ready" may appear in what a
# not-registered miner prints: deploy/join.sh::wait_healthy reads those words as a registration.
REGISTRATION_DEFERRED_LINE = "[daemon] REGISTRATION DEFERRED (faucet: {reason})"
_VRF_OMISSION_SAID = False


def registration_retry_delay(state, reason) -> float:
    return REGISTER_RETRY_S * ADDR_COOLDOWN_RETRY_FACTOR if (state, reason) == (DEFERRED, "addr_cooldown") \
        else REGISTER_RETRY_S


def not_registered_line(mid, reason, delay_s) -> str:
    if reason == "draining":
        return (f"[daemon] {mid} is NOT registered, and it is DRAINING (the file {DRAIN_FILE} in its key "
                f"directory): no registration is attempted while that file is there. Removing it registers this "
                f"miner again at the next pass. It receives no job until then.")
    return (f"[daemon] {mid} is NOT registered yet ({reason or 'no reason given'}); next attempt in "
            f"{max(0, int(delay_s))} s. It receives no job until then.")


def identity_mismatch_text(mid, operator, addr, slot, creator="", region="") -> str:
    """The IDENTITY MISMATCH notice: the chain records ANOTHER key as the operator of this identity.

    ⛔ "A NEW IDENTIFIER" IS NOT A WAY OUT, AND THIS NOTICE USED TO GIVE IT. The chain DERIVES the identifier
    from the key that registers it (align_identity), and main() resumes `identite-resolue` for any name that
    is not a `dm1...`: `join.sh --id <new-id>` (slot 0) or MINER_ID=<new-id> in a slot's env (slot k) gave
    back the SAME identifier, with the same key and the same mismatch. The two ways that change something
    are on the chain (the creator names this key the operator: update-miner, which keeps the bond) or in
    the keyring (the operator's own key restored). `slot` (DENDRA_SLOT as compose hands it, 0 when the kit
    runs one identity) names WHICH keyring: one volume per card (deploy/join.sh --gpus). A value that is
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
    mine = (f"\n             That creator is THIS machine's key: this machine can sign it."
            if creator and creator.lower() == str(addr).lower() else "")
    return (f"[daemon] IDENTITY MISMATCH: '{mid}' is registered on-chain for the operator\n"
            f"         {operator}\n"
            f"         but this machine signs with\n"
            f"         {addr}\n"
            f"         -> the chain will REFUSE every commit (unauthorized). Two ways out:\n"
            f"         (a) the key that registered {mid} ({who}) names this machine's key the operator:\n"
            f"             dendrad tx jobs update-miner {mid} {addr} {region or '<its region>'} 0 --from <that key>\n"
            f"             (a stake of 0 leaves the bond as it is){mine}\n"
            f"         (b) restore the original key of {operator} into {where}.\n"
            f"         A NEW identifier is NOT a way out: the chain derives the identifier from the key, so the\n"
            f"         key in {where} keeps '{mid}'.")


def check_operator(mid, addr, slot, said=None):
    """The operator the chain records for `mid`, compared with this machine's key -> "mismatch" | "ok", or
    `said` unchanged when nothing was read (a registry that does not answer, or no record yet).

    ⛔ IN THE HEARTBEAT, NOT ONLY IN THE LOG. The notice is printed once, at start; the kit ROTATES the container
    logs (deploy/testnet-miner/docker-compose.yml, `logging:`), and the registration in the heartbeat says
    "registered" whatever the operator (register_attempt reads PRESENT). So a long-lived container whose
    notice had rotated away read HEALTHY to deploy/join.sh::wait_healthy. The state is written as
    `identity_mismatch` (null once the chain records this key), read by slots.sh::slot_registration, and
    re-read by the loop: a mismatch fixed on chain (update-miner) clears without a restart, and is said."""
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
    """ONE registration attempt. Returns (state, reason): registered | deferred (reason: the faucet's word,
    see faucet_reason) | refused (the chain did not confirm create-miner) | unread (the registry could not be
    read: nothing was sent). Self-registering miners only; owner mode never calls it."""
    global _VRF_OMISSION_SAID
    state, rec = registry_record(a.id)
    if state == PRESENT:
        return REGISTERED, ""
    if state == UNREAD:
        return UNREAD, "registry_unread"
    # THE STAKE IS READ BEFORE ANYTHING IS ASKED OF ANYONE. min_stake not read: nothing is sent, the faucet
    # included -- a registration with a stake nobody read is refused, and a drip asked for it is spent.
    _min = chain_min_stake()
    _stake_n = _registration_stake() if _min is not None else None
    if _min is None or _stake_n is None:
        print(f"[daemon] {a.id}: the chain's min_stake could not be read, so no registration was attempted and "
              f"the faucet was not asked; tried again at the next attempt.", flush=True)
        return UNREAD, "params_unread"
    # A FAUCET REFUSAL MUST BE SAID. Without funds, `create-miner` is rejected and the miner runs
    # outside the registry: the diagnostic must name the faucet, not suggest a chain problem.
    _fok, _fwhy, _freason = faucet_fund_classified(a.faucet, addr)
    if not _fok:
        print(f"[daemon] FAUCET REFUSED ({a.faucet}) for {addr}: {_fwhy}\n"
              f"         Without funds the on-chain registration is REJECTED and this miner will "
              f"receive no job. Check the faucet URL, or ask the operator for funding.",
              flush=True)
        print(REGISTRATION_DEFERRED_LINE.format(reason=_freason), flush=True)
    else:
        # Wait for the credit only if the request was ACCEPTED: waiting after a refusal burns 40 s
        # at every startup to watch a balance that will not move.
        for _ in range(20):
            if (bal_token(addr) or 0) > 0:     # not read (None) is not a credit: keep waiting
                break
            time.sleep(2)
    _stake = str(_stake_n)
    _out = ""
    for _ in range(3):
        if registry_record(a.id)[0] == PRESENT:
            break
        # we ANCHOR the X25519 pub on-chain (5th arg), signed by the miner's
        # Cosmos key -> the client will encrypt to THIS pub (anti relay-MITM).
        reg = ["create-miner", a.id, addr, "eu", _stake, mypub]
        reg_flags = []
        if vpk:
            reg_flags += ["--vrf-pubkey", vpk]   # vrf_pubkey as a FLAG (not a 2nd optional positional)
        elif not _VRF_OMISSION_SAID:
            _VRF_OMISSION_SAID = True
            # THE OMISSION MUST BE SAID, and it must name what it costs. vrf_pubkey is anchored AT
            # REGISTRATION; skipping it here is not deferred, it is decided. A miner then serves jobs
            # perfectly while being permanently unable to prove availability, and nothing connects the
            # two facts. The command is printed COMPLETE, with this miner's real id and key path
            # already substituted: a message carrying `<placeholders>` gets pasted verbatim.
            # AND IT HANDS THE KEY OVER THE SAME CHANNEL THE CALL SITES DO -- the environment,
            # never argv. A command we print is a command the operator RUNS: handing the secret
            # as a POSITIONAL would put it in `ps` and /proc/<pid>/cmdline, world-readable, on
            # that operator's machine. Advice is delivered code; it obeys the rule code obeys.
            print(f"[daemon] REGISTERING WITHOUT A VRF KEY (dendra-vrf not found in this image).\n"
                  f"         Under verification_mode=1 the chain REFUSES availability proofs from a\n"
                  f"         miner with no anchored vrf_pubkey (ADR-022) and pays it no availability\n"
                  f"         share. Inference, commits and payouts are UNAFFECTED.\n"
                  f"         Fix: rebuild the miner image so it ships dendra-vrf, restart, then anchor\n"
                  f"         the key -- re-registration will NOT do it, this id being already in the\n"
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
    # THE FAILURE MUST BE VISIBLE. Without this block, `create-miner` can fail three times in
    # silence while the daemon still prints "miner ready / waiting for jobs": a miner absent from
    # the registry receives NO job, and nothing says so. A component that fails must say it,
    # otherwise the user pays for the diagnosis.
    _bal = bal_token(addr)
    print(f"[daemon] ON-CHAIN REGISTRATION FAILED (stake={_stake}, min_stake={_min}, "
          f"balance={'?' if _bal is None else _bal}). This miner will receive NO job until it is in the registry.")
    if _out:
        print(f"[daemon]   last response from the chain: {str(_out)[:400]}")
    if not _fok:
        return DEFERRED, _freason
    if _after == UNREAD:
        return UNREAD, "registry_unread"
    return REFUSED, "chain_refused"


def registration_step(a, addr, mypub, vpk, now=None, draining=False):
    """register_attempt, its heartbeat, and when the next one is due. Returns (state, reason, retry_at).

    ⛔ A DRAINING MINER REGISTERS NOTHING, AT START AS IN THE LOOP. The loop already skipped its replay while
    draining, but main() made the FIRST attempt before reading the drain: a draining miner restarted -- its
    process ended and `restart: unless-stopped` brought it back, the machine rebooted, the hourly re-run of
    join.sh recreated the container -- asked the faucet and sent create-miner again once its exit had taken
    it out of the registry, and staked again. deploy/testnet-miner/exit-miner.sh keeps the drain on a running
    miner precisely so that it does not. `draining`: the registry is still READ (a draining miner the chain
    still records stays REGISTERED and finishes what it committed to), and an absent one is DEFERRED with the
    reason `draining` -- no faucet, no transaction -- and retried as soon as the drain is gone."""
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
      * KNOWN not registered -- deferred, refused, or min_stake unread after the registry answered ABSENT
        (`params_unread`): no. The chain refuses a proof from a miner it does not record, and every refused
        proof costs a test inference and a transaction, once per AVAIL_CHECK_S;
      * the registry NOT READ (`registry_unread`): yes. An unknown is neither state, and the chain judges the
        proof. Folded into "not registered", it silenced a REGISTERED miner whose node answered late at start
        until the next attempt, REGISTER_RETRY_S later -- long enough to miss an availability window. The cost
        the other way is bounded by that same attempt: a miner that turns out absent has spent, until then, the
        refused proofs described above."""
    return state == REGISTERED or (state == UNREAD and why == "registry_unread")


def vrf_key_notice(mid, vpk, onchain) -> str:
    """What to say about the VRF key at start, from the key this node holds (`vpk`, "" when none) and the one the
    chain anchors (`onchain`: miner_vrf_onchain, None when it was NOT READ). "" when there is nothing to say --
    and always when nothing was read: an unread key is compared again later, never announced missing."""
    if onchain is None:
        return ""
    if not onchain:
        if vpk:
            # This node has the key, the chain does not: one gesture is missing, printed COMPLETE.
            return (f"[daemon] NO VRF KEY ANCHORED ON-CHAIN for {mid}, though this node holds one.\n"
                    f"         Availability proofs are refused under verification_mode=1 until it is\n"
                    f"         anchored, and re-registration will not do it. One command:\n"
                    f"           dendrad tx jobs rotate-miner-keys {mid} --new-vrf-pubkey {vpk}")
        return (f"[daemon] NO VRF KEY, on-chain or locally (dendra-vrf not found in this image).\n"
                f"         This miner serves jobs normally but can never prove availability, and is\n"
                f"         paid no availability share. Rebuild the image so it ships dendra-vrf, restart,\n"
                f"         then anchor the key this node will generate.")
    if vpk and onchain.strip().lower() != vpk.strip().lower():
        # THE FOURTH COMBINATION, AND THE ONLY ONE THAT LOOKS HEALTHY. Both keys exist, so neither
        # branch above fires; the chain refuses only an ABSENT key, so nothing complains at startup
        # either. What fails is the PROOF: it is produced with the key this node holds and verified
        # against the one the chain anchored, so every availability transaction is rejected for as
        # long as the two differ. Reading a value and testing only whether it is EMPTY is not
        # comparing it. The diagnosis is worth more here than at proof time: the remedy is one
        # command, and the miner loses its availability share for every epoch it runs without it.
        return (f"[daemon] VRF KEY MISMATCH for {mid}: the chain has one key anchored, this node holds\n"
                f"         another. Availability proofs are produced with the local key and verified\n"
                f"         against the anchored one, so every one of them is refused. One command:\n"
                f"           dendrad tx jobs rotate-miner-keys {mid} --new-vrf-pubkey {vpk}")
    return ""


# ── THE ENCRYPTION KEY THIS NODE DECRYPTS WITH, CONFRONTED WITH THE ONE THE CHAIN ANCHORS ─────────────────
# A client seals every request to the encryption key ANCHORED on chain (client.submit_job, strict by
# default), never to the copy this node deposits at the relay. A node whose key file was regenerated -- a
# passphrase lost, the account restored from its recovery phrase, the sealed key file then removed -- holds a
# NEW key in silence: it proves its presence (a test inference decrypts nothing), is drawn, and fails every
# request it is handed. The VRF key was compared at start; this one never was. Now: a key that differs from the
# anchored one is SAID with the command that anchors this node's key, kept in the heartbeat (`enc_key`), and NO
# availability proof is sent while the two differ -- a present miner that can open nothing is present for
# nothing (the rule of the useless present). The rotation is NEVER made here: a wrong volume mounted on this
# node would otherwise replace the right key on chain, and only its owner can tell which one is right.
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
        return (f"[daemon] ENCRYPTION KEY MISMATCH for {mid}: the chain anchors the encryption key\n"
                f"         {anchored}\n"
                f"         and this node decrypts with\n"
                f"         {local}\n"
                f"         Every request is sealed to the ANCHORED key, so this node can open none of them, and NO\n"
                f"         availability proof is sent while the two differ. Two ways out, never taken for you:\n"
                f"         (a) the anchored key's file still exists (another volume, a backup): put it back as\n"
                f"             {mid}.sk in this node's key directory and restart;\n"
                f"         (b) it is lost for good: anchor this node's key, from inside the miner container --\n"
                f"             {rotate}")
    return (f"[daemon] NO ENCRYPTION KEY ANCHORED on chain for {mid}: a client that requires the anchored key\n"
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


# ── DRAINING: A MINER ON ITS WAY OUT ───────────────────────────────────────────────────────────────────
# A file named DRAIN_FILE in the key directory (next to AVAIL_RECORD) puts the daemon in DRAIN: it proves no more
# presence and takes no NEW request -- so the chain stops drawing it -- while it still finishes what it has
# committed to: a sealed response the relay refused is deposited again, a journalled commitment is anchored. The
# reveal worker, a process of its own, keeps answering the audits it sits on. The heartbeat carries
# `draining`. Removing the file resumes everything at the next pass. Nothing here creates the file.
DRAIN_FILE = "drain"


def drain_state(keydir, prev=False) -> bool:
    """True when DRAIN_FILE exists in `keydir` (a dangling link counts: its NAME is the signal), False when it does
    not, `prev` when that could not be told (an error other than "not found"): an unknown keeps the state it had."""
    try:
        os.lstat(os.path.join(str(keydir), DRAIN_FILE))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return prev


# ── THE ENGINE'S VERSION, IN THE HEARTBEAT ─────────────────────────────────────────────────────────────
# Two engines of different versions may embed the same text differently, and run the same model at different
# concurrency: the version is what explains a divergence between two machines, and it was written nowhere.
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


# ── WAS THE ANSWER IN TIME FOR ITS CLIENT? ───────────────────────────────────────────────────────────────
# A client waits a bounded time for an answer and pays nothing for one that comes after it stopped waiting; the
# miner measured its generation (`gen_s`) and nobody compared it with anything. The comparison is made with the
# wait the CLIENT DECLARES in its request (`wait_s`, next to `max_out`), never with a bound written here: a
# client's wait is the client's, and a constant copied into the miner is wrong at the client's next change.
# A request that declares no wait gets NO verdict -- counted as undeclared, never judged against a default.
def declared_wait_s(req):
    """The wait, in seconds, the request declares (a positive finite number in `wait_s`), or None."""
    v = req.get("wait_s") if isinstance(req, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v > 0 and math.isfinite(v) else None


def answer_timing(gen_s, wait_s):
    """"late" when the generation alone took longer than the declared wait, "on_time" otherwise, None without a
    declared wait. The generation is a LOWER bound of what the client waited: a "late" is certain, an "on_time"
    is not a promise that the client was still waiting."""
    if wait_s is None or gen_s is None:
        return None
    return "late" if float(gen_s) > float(wait_s) else "on_time"


# ── THE AVAILABILITY PROOF COMES FIRST ───────────────────────────────────────────────────────────────────
# The proof used to be tried after every request of a pass, one pass in four: a busy miner proved its presence
# at the edge of the window, and a window missed counts against it in the draws that read presence
# (chain/x/jobs/keeper/presence.go::minerPresentAt). Now the challenge is read at the
# start, at the head of every pass and between two requests, at most every AVAIL_CHECK_S seconds -- a clock,
# not a count of passes -- and a new challenge whose window is open is proven BEFORE the next request is served.
# The test inference stays the condition of every proof (prove_availability_once, presence_probe). CHOSEN, not
# measured: it bounds the reads of the challenge to one per this many seconds. The window itself is counted in
# BLOCKS (`avail_deadline_blocks`), so the share of it this delay costs depends on the block interval, which the
# consensus does not fix.
AVAIL_CHECK_S = 30.0
# How often an EMPTY weights_hash is read again (model_weights_hash). Chosen: one /api/tags request per this many
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
    # waiting on its registration is starting, not stuck, and the file says which.
    _status_write(phase="starting", started_at=int(time.time()), pid=os.getpid())

    # PROCESS confinement at startup (anti same-user debugger, no core
    # dump, no-new-privs; mlockall opt-in). Best-effort, NEVER fatal. OFF via DENDRA_CONFINE=0.
    # STRONG OS confinement (routeless netns, seccomp, egress) lives in modea_confine.sh; the kit never runs it.
    if os.environ.get("DENDRA_CONFINE", "1") != "0":
        try:
            from modea import confine
            cr = confine.apply_process_confinement_report_cached()
            print(f"[daemon] confinement dumpable_off={cr['non_dumpable']} no_new_privs={cr['no_new_privs']} "
                  f"core_off={cr['core_dumps_disabled']} mlockall={cr['mlockall']} (root remains privileged)")
        except Exception as e:
            print(f"[daemon] confinement unavailable ({type(e).__name__}) -> continuing (best-effort)")

    # ═══ IDENTITY FIRST — every key file below is NAMED after it ═══════════════════════════════════
    # The cosmos key is obtained here, before every other key, because the identifier the chain will
    # accept is DERIVED from its address (CreateMiner refuses any other). Resolving it after the x25519
    # key, the attestation key or the VRF key exist would leave them under a name nobody uses again, and
    # a second encryption identity would be minted in silence at the next start.
    Path(a.keydir).mkdir(parents=True, exist_ok=True)

    # ⛔ THE RESOLVED IDENTITY MUST BE PERSISTED, OR EVERY RESTART MINTS A NEW ONE — AND DROPS THE STAKE.
    # `align_identity` below renames the keyring entry from the passed name to the derived `dm1…`.
    # The passed name comes from `deploy/testnet-miner/.env`, which nothing rewrites, so without this
    # memory the NEXT start calls keys_addr() with the OLD name, finds no key under it (it has been
    # renamed), and `keys_addr` CREATES one when the name is unknown. That happens in silence: a
    # brand-new address, a brand-new derived identifier, an unfunded account — while the previous
    # registration, its stake and its held fees stay on-chain under an identifier this machine no
    # longer holds. The only visible symptom is a miner that "does not receive jobs", with nothing
    # pointing at the cause.
    #
    # The memory belongs HERE and not in the .env, because this is the only place that KNOWS the
    # answer, and `keydir` is already the persisted volume — the same durability the keyring itself
    # relies on. An explicit `dm1…` on the command line still wins: this file is a memory, never an
    # override.
    _memory = Path(a.keydir) / "identite-resolue"
    if not a.id.startswith("dm1") and _memory.exists():
        _guard = _memory.read_text(encoding="utf-8").strip()
        if _guard.startswith("dm1"):
            print(f"[daemon] identity RESUMED from {_memory}: {_guard} "
                  f"(the name passed in, '{a.id}', was resolved on a previous start)", flush=True)
            a.id = _guard

    # THE KEYRING, FROM THE STATE OF THE DISK (modea/keyring.py). A keyring this process cannot open --
    # two keyrings, an encrypted one without its passphrase, a passphrase that does not open it -- STOPS
    # the daemon here, before anything could create a key in its place.
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
    # OWNER MODE IS DECIDED BEFORE THE IDENTITY IS ALIGNED, because it decides which address the identifier
    # derives from -- and a rename is the one step here that cannot be taken back once the owner registers.
    try:
        owner = decide_owner(a.id, addr)
    except OwnerModeError as e:
        _stop_on_owner(e)
    a.id = align_identity(a.id, addr, owner=owner)
    # THE ADDRESS GOES INTO THE HEARTBEAT, NOT ONLY INTO THE LOG. The kit rotates the container logs
    # (deploy/testnet-miner/docker-compose.yml, `logging:`): the one "ready  addr=..." line printed at start
    # is gone after enough traffic. The heartbeat is rewritten as the loop turns, and
    # `python3 -m modea.keyring address /data/keys` reads the same address from the keyring itself.
    _status_write(miner_id=a.id, owner=owner, address=addr)

    # THE SIGNING NAME FOLLOWS THE RENAME, OR EVERY DEPOSIT GOES OUT UNSIGNED.
    # `align_identity` renames the keyring entry to the derived identity. `relay_client` read
    # `DENDRA_SIGN_KEY` at IMPORT -- by default `MINER_ID`, the name that no longer exists -- so from
    # here on it would sign for nothing. Re-exporting the variable is useless: the module has already
    # read it. This re-points it and clears what was cached for the old name.
    try:
        relay.set_sign_key(a.id)
    except AttributeError:
        # An older relay_client without the entry point: say it rather than sign for a dead name.
        print("[daemon] WARNING: relay_client has no set_sign_key -- deposits will be signed for "
              f"'{os.environ.get('DENDRA_SIGN_KEY', '')}', which the rename just replaced with "
              f"'{a.id}'. Under DENDRA_RELAY_SIGN=enforce they would be REFUSED.", flush=True)

    # AND IT IS CHECKED, NOT ASSUMED. A signing key that resolves to another address is the same
    # failure one step later, and it is invisible until enforcement is armed.
    try:
        _sa = relay_signature.address_from_key(a.id, keyring_dir=os.environ.get("DENDRA_KEYRING_DIR") or None)
    except Exception as _e:
        _sa = None
        print(f"[daemon] WARNING: cannot resolve the signing key '{a.id}': {_e}. Deposits will go out "
              "UNSIGNED; under DENDRA_RELAY_SIGN=enforce the relay refuses them.", flush=True)
    if _sa and _sa != addr:
        print(f"[daemon] WARNING: the signing key '{a.id}' resolves to {_sa} but this miner is {addr}. "
              "The relay attributes a deposit to whoever SIGNED it, so writes would be attributed to "
              "the wrong operator -- or refused.", flush=True)
    elif _sa:
        print(f"[daemon] signing identity: {a.id} -> {_sa} (matches this miner)", flush=True)

    # Written AFTER alignment, so what is stored is what the chain accepts — and only that. A failed
    # derivation leaves `a.id` unchanged and non-`dm1`, and storing it would make the next start
    # resume a value the chain refuses, turning one bad run into a permanent one.
    if a.id.startswith("dm1"):
        try:
            _memory.write_text(a.id + "\n", encoding="utf-8")
        except OSError as e:
            # Not fatal: the miner works this session. But it is said out loud, because silence is
            # what makes this defect invisible — the next restart mints a new identity again.
            print(f"[daemon] WARNING: cannot persist the resolved identity to {_memory} "
                  f"({type(e).__name__}: {e}). This miner will resolve a NEW identity at the next "
                  f"restart, losing this registration. Fix the volume before relying on it.", flush=True)

    skpath = Path(a.keydir) / f"{a.id}.sk"

    # --- encryption identity (X25519): persisted in the key envelope, under the keyring's passphrase ---
    # The passphrase is the KEYRING's (modea/keyring.py: the file mounted at /run/dendra-secrets in `file`
    # mode). A key found in clear while that passphrase is there is re-encrypted in place, atomically --
    # setting a passphrase after the fact used to leave the key in clear and silence the warning anyway,
    # because the warning tested the variable and not the file. It now reads the FILE.
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
        print(f"[daemon] WARNING: the encryption key {skpath} is stored IN CLEAR (mode 0600): "
              + ("the keyring is the test keyring, so the key files are not sealed either. "
                 "bash deploy/testnet-miner/encrypt-keys.sh encrypts both." if _kr_now.backend == kring.TEST
                 else "no passphrase was available to seal it."), flush=True)
    mypub = crypto.pub_bytes(sk).hex()

    # THE COSMOS KEY IS OBTAINED BEFORE THE FIRST DEPOSIT, NOT AFTER.
    #
    # Calling `keys_addr` further down, past the `pub` and `attest` deposits, means that on a FIRST
    # start the two bootstrap writes leave before the account that owns them exists — nothing can
    # sign them, and the relay cannot attribute them to anyone. That costs nothing for as long as the
    # relay attributes nothing either; it becomes the reason a new miner cannot join the day writes
    # have to be signed.
    #
    # Resolving it early is safe: `keys_addr` depends on neither `sk` nor `mypub`, and the attestation
    # below keeps its own `enc_sk=sk`. The order follows the dependency, which is the only order that
    # survives a change of policy.
    #
    # ⭐ THE RESOLUTION ITSELF SITS HIGHER STILL, above the key files, because the IDENTIFIER derives
    # from this address and every key file is named after the identifier. `addr` and `a.id` are already
    # resolved at this point; this line is a no-op assertion that the two agree.
    assert addr == keys_addr(a.id), "the keyring address changed under us between the two reads"

    # ⛔ THE TWO BOOTSTRAP DEPOSITS (`pub`, `attest`) WAIT FOR THE REGISTRATION, AND THIS IS NOT COSMETIC.
    # They used to leave here, before the faucet's credit. A deposit is SIGNED by this miner's key
    # (relay_client._signature), and a signature needs the ACCOUNT: on a first start the chain holds none
    # for an address no credit has reached, so both deposits went out UNSIGNED -- refused by a relay that
    # requires signatures (`enforce`), for every new miner -- and the chain's answer ("account ... not found:
    # key not found") landed in the log, where deploy/join.sh::wait_healthy read "key not found" as a keyring
    # failure and declared a healthy first start NOT HEALTHY. Registered means credited: once the chain
    # records this miner its account exists, both deposits can be signed, and none is refused for that.
    # A miner that is not registered receives no job, so nothing waits on these deposits meanwhile.
    _enc_state = None    # check_enc_key: the anchored encryption key against this node's, read once registered

    def _bootstrap_deposits():
        # THE FIRST WRITE, AND ITS RESULT IS SAID. A relay that refused the key used to answer into a void:
        # it is what makes the relay's WRITE policy unmeasurable from the only place that exercises it.
        # It stays non-fatal. This key is a FALLBACK: the source of truth is the `enc_pubkey` anchored on
        # chain (`reveal_helpers.committee_pubs`, and the client refuses an unanchored miner outright in
        # strict mode), so a refused deposit costs a cross-check, not the ability to mine.
        # A key that DIFFERS from the anchored one is not deposited: it would replace the relay's copy of the
        # anchored key with one no client seals to, and the self-test's cross-check would then accuse the relay.
        if _enc_state == ENC_MISMATCH:
            print(f"[daemon] the encryption pubkey is NOT deposited at the relay for {a.id}: it differs from the "
                  f"one anchored on chain (see ENCRYPTION KEY MISMATCH).", flush=True)
        elif relay.put(a.relay, "pub", a.id, {"pub": mypub}):
            print(f"[daemon] encryption pubkey published to the relay for {a.id}")
        else:
            print(f"[daemon] the relay REFUSED the pubkey deposit for {a.id}. Mining is NOT blocked -- the "
                  f"key anchored on chain is what peers use -- but the relay copy will be missing, so the "
                  f"client-side cross-check against it is not available. If deposits are refused here, they "
                  f"will be refused for reveals too: check the relay's write policy before assuming this is "
                  f"cosmetic.", flush=True)
        # SIGNED software attestation (measured hash = code + model_id + weights_hash + confinement). A relay
        # with the gate active (DENDRA_ATTEST_REQUIRE=1 + allow-list) only assigns a confidential job to
        # attested miners. Best-effort (never fatal). HONEST: deterrence, not a proof of execution (cf.
        # modea/confine.py). "published" is a claim about the RELAY, printed only when the relay accepted.
        try:
            from modea import confine as _confine
            _wh = model_weights_hash()
            _ask, _apub = _confine.load_or_create_attest_key(a.keydir, a.id, passphrase=_open, seal_with=_seal)
            _att = _confine.signed_attestation(_ask, miner_id=a.id, model_id=MODEL_ID,
                                               weights_hash=_wh, enc_sk=sk)
            if relay.put(a.relay, "attest", a.id, _att):
                print(f"[daemon] signed attestation published  measured_hash={_att['measured_hash'][:16]}...  "
                      f"attest_pub={_apub[:16]}... (relay allow-list = DENDRA_ATTEST_ALLOW)")
            else:
                print(f"[daemon] the relay REFUSED the attestation deposit for {a.id} "
                      f"(measured_hash={_att['measured_hash'][:16]}...). While the relay gate is armed "
                      f"(DENDRA_ATTEST_REQUIRE=1) this miner is assigned no confidential job.", flush=True)
        except Exception as e:
            print(f"[daemon] attestation unavailable ({type(e).__name__}) -> continuing (best-effort)")

    # --- chain identity: faucet + self-signed registration (the key was obtained above) ---
    try:
        vsk, vpk = vrf_identity(a.keydir, a.id)   # VRF identity (proof of availability)
    except crypto.KeyEnvelopeError as e:
        _stop_on_keyring(kring.KeyringUnreadable(f"{a.keydir}/{a.id}.vrf: {e}"))
    # OWNER MODE: the owner registers; this machine prepares, prints, and WAITS until the chain records its
    # key as the operator. Nothing below this block runs before that -- with --once, nothing runs at all.
    if owner:
        if not owner_wait(a, addr, owner, mypub, vpk):
            print(f"[daemon] {a.id}: the owner has not registered this miner with this machine as its operator "
                  f"yet; nothing was mined.", flush=True)
            return
    # REGISTERED IDENTITY != CURRENT KEY -> every commit will be refused.
    # `stake_of()` only checks that the miner_id EXISTS, not WHO owns it: an operator who lost their
    # keyring (volume recreated, machine reinstalled) believes they are registered — stake present,
    # "miner ready" — and then every commit is rejected by the chain with "only the miner's operator may
    # anchor its commit". The GPU runs, jobs are processed, nothing is ever paid. The chain is right to
    # refuse; it is the daemon's job to SAY so before mining for nothing.
    # The notice AND the heartbeat (check_operator): the kit rotates its logs, and a notice printed once at
    # start is gone from a long-lived container's log while the mismatch still holds.
    _op_state = check_operator(a.id, addr, os.environ.get("DENDRA_SLOT"))
    _op_at = time.time()
    # `not owner`: a miner in owner mode NEVER registers itself, even slashed to a stake of zero -- the bond
    # and the identity are the owner's, and this machine's key signing create-miner would be a second miner.
    # The attempt is register_attempt, replayed by the loop below until the chain records this miner.
    # THE DRAIN IS READ BEFORE THE FIRST REGISTRATION ATTEMPT (registration_step, `draining`): a draining miner
    # that restarts must not register and stake again.
    _draining = drain_state(a.keydir, False)
    if owner:
        reg_state, reg_why, reg_retry_at = REGISTERED, "owner", 0
        _status_write(registration={"state": REGISTERED, "reason": "owner", "retry_at": 0})
    else:
        reg_state, reg_why, reg_retry_at = registration_step(a, addr, mypub, vpk, draining=_draining)
    # The encryption key the chain anchors, against this node's (check_enc_key) -- before the deposits, which
    # leave a differing key out of the relay.
    _enc_state = check_enc_key(a.id, mypub)
    # The bootstrap deposits, now that the account exists (see _bootstrap_deposits); otherwise the loop below
    # makes them once the registration it replays succeeds.
    _deposited = False
    if reg_state == REGISTERED:
        _bootstrap_deposits()
        _deposited = True

    # ⚠️ THIS CHECK LIVES OUTSIDE THE REGISTRATION BLOCK, AND THAT IS THE WHOLE POINT.
    # The omission notice above only fires while REGISTERING. A miner ALREADY registered without a key
    # never goes through it again -- which is precisely the population that has the problem, and the only
    # one that never heard about it. Re-registering does not fix it either: the id being in the registry,
    # `create-miner` is not even replayed.
    # So the chain's REAL state is read at every startup and compared with what this node holds
    # (vrf_key_notice). A key that could NOT be read is compared again at the loop's half-hourly reads.
    _vrf_chain = miner_vrf_onchain(a.id)
    _vrf_text = vrf_key_notice(a.id, vpk, _vrf_chain)
    if _vrf_text:
        print(_vrf_text, flush=True)

    backend = pick_backend(a.backend)
    miner = Miner(a.id, backend=backend, hardened=True, sk=sk)
    # THE ENGINE'S VERSION (engine_version), in the heartbeat from the first pass, read again every half hour.
    _engine = engine_version(backend, getattr(miner, "backend", None))
    _status_note(engine_version=_engine)
    if _engine.get("engine") == "ollama":
        print(f"[daemon] inference engine: Ollama {_engine.get('version') or '? (' + str(_engine.get('why')) + ')'}",
              flush=True)
    whash = model_weights_hash()
    # AN EMPTY weights_hash IS READ AGAIN, at most every WHASH_RETRY_S: an engine not ready at start used to leave
    # it empty for the whole life of the process.
    _whash_at = time.time()
    if MODEL_ID:
        print(f"[daemon] model_id={MODEL_ID}  weights_hash={(whash[:16]+'...') if whash else '<absent>'}")
    # "ready" IS SAID ONLY OF A MINER THE CHAIN RECORDS. A miner not registered yet says so, with the next
    # attempt, and the loop below replays the registration until it is.
    def _ready_line():
        _rs, _rec = registry_record(a.id)
        return (f"[daemon] miner {a.id} ready  addr={addr}  "
                f"stake={_rec.get('stake', 0) if _rs == PRESENT else '?'}  backend={backend}")
    if reg_state == REGISTERED:
        print(_ready_line(), flush=True)
    else:
        print(not_registered_line(a.id, reg_why, reg_retry_at - time.time()), flush=True)

    # THE FINAL TESTNET SEASON'S PAYOUT ADDRESS (DENDRA_PAYOUT_ADDRESS, written by join.sh
    # --payout-address): declared once the miner is REGISTERED -- the programme attributes a declaration to
    # the operator the chain records -- and again only when the setting changes. Without it the season pays
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
    # THE FINAL TESTNET SEASON (ADR-047) asks the miner nothing: it pays the work served, the verdicts and
    # the availability windows proven on chain. A miner with no VRF key proves no window, and says so, once
    # an hour.
    _novrf_said = 0.0
    _stale_seen = set()                   # requests counted stale by this process (requests_stale, once each)
    # EVERY TEST INFERENCE leaves its outcome as the engine's latest reading (recorded_probe): the one an
    # availability proof waits for, and the one a failed request asks for. It is a generation AND the embedding a
    # request needs (serving_probe): both readers ask "can this engine serve a request now?".
    _probe = recorded_probe(probe=lambda: serving_probe(probe=lambda: presence_probe(miner.backend),
                                                        embed=lambda text: answer_embedding(text, miner.backend)))
    if _draining:
        print(f"[daemon] {a.id} starts DRAINING ({a.keydir}/{DRAIN_FILE} exists): no availability proof and no "
              f"new request; what this miner committed to is finished, and the reveal worker keeps running. "
              f"Remove the file to resume.", flush=True)

    def _drain_check():
        """Reads DRAIN_FILE again; a change is said once."""
        nonlocal _draining
        now_d = drain_state(a.keydir, _draining)
        if now_d != _draining:
            print(f"[daemon] {a.id} " + (
                f"DRAINING ({a.keydir}/{DRAIN_FILE} appeared): no availability proof and no new request from "
                f"now on; what this miner committed to is finished, and the reveal worker keeps running."
                if now_d else f"no longer draining ({a.keydir}/{DRAIN_FILE} is gone): proofs and requests "
                              f"resume."), flush=True)
        _draining = now_d
        return now_d

    def _presence_turn(force=False):
        """THE AVAILABILITY PROOF FIRST (AVAIL_CHECK_S): the challenge read on its clock -- at start (`force`), at
        the head of every pass, between two requests -- and a new one proven before anything else is served,
        always behind a test inference that answered (prove_availability_once). Never while draining, never while
        the encryption key differs from the anchored one, never for a miner the chain is KNOWN not to record
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
        _last_chal = prove_availability_once(a.id, vsk, _last_chal, keydir=a.keydir, probe=_probe)

    # AT START, THE PROOF BEFORE THE FIRST PASS: a challenge open since the miner went down is answered first.
    _presence_turn(force=True)
    _status_write(phase="loop", draining=_draining)
    while True:
        try:
            _drain_check()
            _presence_turn()
            # THIS MINER'S SLICE ONLY (`suffix`, relay_client.listing): the relay answers the keys ending with
            # `__<this id>` -- every key this loop reads -- and a relay that does not serve the filter is read in
            # full and filtered by the client, so the same jobs are seen either way.
            lst = relay.listing(a.relay, suffix)
            # The relay names every one of its kinds in this answer, even when they are empty, so an
            # empty dict is the client's own failure value (relay_client.listing): only a non-empty
            # answer is a queue that was READ.
            _listing_read = isinstance(lst, dict) and bool(lst)
            if _listing_read:
                _status_note(relay_listing_ok_at=int(time.time()))
            ress = set(lst.get("res", []))
            # The journal and the failures, ONCE per pass (they used to be read once per key of the whole relay).
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
                # 17: a job whose response is deposited BUT whose commitment is still pending is no
                # longer skipped -- it is exactly the one to resume, and the resumption recomputes
                # nothing: it replays the anchoring with the journalled commitment.
                _pending = _journal.get(key) or {}
                _fe = _fails.get(key) or {}
                if key in done or (key in ress and not _pending):
                    continue
                if _pending and key in ress:
                    # RESUMPTION. The response is already at the relay and its commitment is journalled:
                    # only the anchoring is replayed. Re-running inference would produce a different
                    # answer, and the deposit is write-once and sealed to the client.
                    _c, _pc = _pending.get("commit", ""), _pending.get("pcommit", "")
                    if _c and _c in query("get-commit", key):
                        # Counted only when THIS process broadcast it (a late inclusion of an earlier pass).
                        _commit_anchored(key)
                        _forget_commitment(a.keydir, key)
                        if _fails.pop(key, None) is not None:
                            _write_failures(a.keydir, _fails)
                        done.add(key)
                        continue
                    # A REFUSED RESUMPTION WAITS ITS PAUSE (JOB_FAILURES), instead of a transaction per pass.
                    if _fe.get("kind") == "anchor" and not job_due(_fe, time.time()):
                        continue
                    # The flags JOURNALLED with the commitment (_journal_flags): the model that answered.
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
                        print(f"[daemon] {a.id} job {key}: anchoring RESUMED from the journal (inference NOT replayed)")
                    else:
                        _fe = note_job_failure(_fails, key, "anchor", _tx_err(_out), time.time())
                        _write_failures(a.keydir, _fails)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "commit not anchored", "why": _tx_err(_out),
                                           "next_at": int(_fe["next_at"])})
                        print(f"[daemon] {a.id} job {key}: anchoring still refused -> the commitment STAYS"
                              f" in the journal; retried without recomputing in "
                              f"{int(_fe['next_at'] - _fe['last_at'])} s")
                    continue
                # A DEPOSIT REFUSED EARLIER: its sealed response was KEPT (JOB_FAILURES) and its commitment
                # journalled with it. Only the deposit is replayed, with THAT response -- never a new inference,
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
                        print(f"[daemon] {a.id} job {key}: the sealed response KEPT after a refused deposit is now "
                              f"deposited (inference NOT replayed); its journalled commitment is anchored next.")
                    else:
                        _job_count("deposit_refused")
                        _fe = note_job_failure(_fails, key, "deposit", "the relay refused the sealed response",
                                               time.time(), sealed=_fe["sealed"])
                        _write_failures(a.keydir, _fails)
                        _status_write(job={"at": int(time.time()), "job": key[: -len(suffix)],
                                           "result": "response refused by the relay", "attempt": _fe["failures"],
                                           "next_at": int(_fe["next_at"])})
                        print(f"[daemon] {a.id} job {key}: the relay REFUSED the kept sealed response again "
                              f"(deposit {_fe['failures']}); the same response is offered again in "
                              f"{int(_fe['next_at'] - _fe['last_at'])} s, never a new inference.")
                    continue
                if not _RE_KEY.match(key):       # unsafe relay key -> ignore (anti dendrad injection)
                    continue
                jid = key[: -len(suffix)]
                # A REQUEST GIVEN UP, OR WAITING ITS PAUSE (JOB_FAILURES), is not generated now.
                if _fe.get("abandoned") or not job_due(_fe, time.time()):
                    continue
                # A NEW REQUEST NOBODY WAITS FOR ANY MORE (request_max_age_s) is not served, by this machine's clock
                # and the chain's (request_stale_age). Never applied to a request whose commitment is journalled:
                # that one was served already.
                if not _pending:
                    _age = request_stale_age(jid, time.time(), chain_now)
                    if _age is not None:
                        if key not in _stale_seen:
                            _stale_seen.add(key)
                            _job_count("requests_stale")
                            print(f"[daemon] {a.id} job {jid}: NOT served, made {int(_age)} s ago, past "
                                  f"{request_max_age_s():.0f} s (DENDRA_REQUEST_MAX_AGE_S): its client no longer "
                                  f"waits for it. Said once.", flush=True)
                        continue
                # DRAINING: no new request (drain_state, read again here: it may have appeared mid-pass).
                if _drain_check():
                    continue
                # THE AVAILABILITY PROOF BEFORE THE NEXT REQUEST, when a new challenge is open (AVAIL_CHECK_S).
                _presence_turn()
                req = relay.get(a.relay, "req", key)
                if not req:
                    continue
                # ONE MALFORMED DEPOSIT MUST NOT STARVE EVERY JOB BEHIND IT, so the decoding of the
                # request body is caught HERE rather than left to the loop's own `except`. A `req`
                # missing `client_eph_pk`, or carrying a value that is not hex, raises: caught at the
                # loop level it would end the whole `for`, and since the keys are walked in the same
                # order on every pass (newest_first) and the key is WRITE_ONCE, the same deposit would
                # stop the same turn at the same position on every pass -- starving every job walked
                # after it until the relay's retention expires it, with no message naming any of them.
                # A body we cannot parse is one job we skip, not a turn we abandon.
                try:
                    eph = bytes.fromhex(req["client_eph_pk"])
                    sealed = Sealed(bytes.fromhex(req["nonce"]), bytes.fromhex(req["ct"]))
                except Exception as e:
                    print(f"[daemon] {a.id} job {jid}: unusable request body "
                          f"({type(e).__name__}) -> skipped, the other jobs continue")
                    continue
                # HOW LONG THE ANSWER TOOK, measured on every job. The client waits for it a bounded time (the
                # Final Testnet Season generator: client.quick_metered, its `timeout`), and a judge on the
                # CPU (deploy/hw_probe.sh --role: judge) answers with a model whose speed depends on the machine's
                # memory, which its RAM floor does not measure. A slow answer is READ here, in the log and the
                # heartbeat, rather than inferred later from a request that was never paid.
                _t0 = time.monotonic()
                try:
                    res = miner.handle_job(jid, eph, sealed, max_out=int(req.get("max_out", 0)))  # requested cap
                except Exception as e:
                    _gen_f = round(time.monotonic() - _t0, 1)
                    _job_count("inference_failed")
                    # THE ENGINE IS ASKED A TEST REQUEST RIGHT AFTER (engine_answered_since): only a failure the
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
                        print(f"[daemon] {a.id} job {jid}: ABANDONED after {_fe['counted']} failed inferences "
                              f"({type(e).__name__}), each followed by an engine that answered a test request and "
                              f"embedded it: the request itself fails, and it is not generated again "
                              f"(DENDRA_JOB_MAX_ATTEMPTS={job_max_attempts()}).", flush=True)
                    else:
                        print(f"[daemon] {a.id} inference failed for job {jid}: {type(e).__name__} (attempt "
                              f"{_fe['failures']}; the engine "
                              f"{'answered and embedded' if _engine_ok else 'did NOT answer and embed'} a test "
                              f"request right after"
                              f"{'' if _engine_ok else ', so this failure does not count against the request'}"
                              f"); next attempt in {int(_fe['next_at'] - _fe['last_at'])} s.", flush=True)
                    continue
                _gen_s = round(time.monotonic() - _t0, 1)
                # WAS IT IN TIME FOR ITS CLIENT? Against the wait the client DECLARED (declared_wait_s), never a
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
                # THE DEPOSIT IS THE ONLY COPY OF THE ANSWER, so its refusal gates everything after it.
                # The relay declines for reasons that occur in production -- an unsigned or unauthorised
                # write (401), a body over the store's limit (413), a saturated or full store (503/507).
                # Anchoring a commit for a response the client can never fetch produces a job that is
                # unverifiable and an escrow that never settles, and marking it done means it is never
                # retried. So a refused deposit anchors nothing, marks nothing, and falls through to the
                # next pass -- which is safe precisely because nothing has been anchored yet.
                # 17 (ADR-045) — L ENGAGEMENT EST CALCULE ET RENDU DURABLE AVANT LE DEPOT.
                #
                # The defect: the loop skipped a job as soon as its RESPONSE appeared in the relay
                # listing, while the response is deposited BEFORE the commitment is anchored. The three
                # attempts failed (misaligned identity, gas, dropped RPC), the GPU had run, the answer
                # was sealed at the relay, and NO commit existed.
                #
                # Why not de-duplicating on the chain: a later pass would re-run INFERENCE, an LLM does
                # not answer twice the same, and the deposit is sealed to the CLIENT key -- this
                # process cannot read back what it stored. Any recovery must CARRY THE ORIGINAL
                # COMMITMENT FORWARD, never recompute one. Why not keeping it in memory: it does not
                # survive a restart, which is exactly the case a daemon exists to survive.
                # The IRREPEATABLE step therefore becomes durable first; only the deposit and the
                # anchoring stay retryable, and those genuinely are.
                commit = res.content_embed   # embedding (semantic mode: robust to free-form LLM)
                # THE QUESTION GETS ITS OWN COMMITMENT, computed by `handle_job` while the plaintext
                # was still inside its locked buffer. It must differ from `commit`: a juror receives
                # the prompt from the reveal, i.e. from the party being audited, so only a commitment
                # made BEFORE the answer was known ties the grading to the question the client
                # actually asked. Anchoring the answer commitment twice would name a commitment
                # without making one. An empty value -- a miner build that cannot compute it -- falls
                # back to that duplicate, which a juror recognises and ABSTAINS on rather than
                # slashing: a miner is not punished for the age of its build.
                pcommit = res.prompt_commit or commit
                # An EMPTY weights_hash is read again (WHASH_RETRY_S) before it is journalled and anchored.
                if MODEL_ID and not whash and time.time() - _whash_at >= WHASH_RETRY_S:
                    _whash_at = time.time()
                    whash = model_weights_hash()
                # The model that answered is journalled WITH the commitment: a resumption anchors it with these.
                _journal_commitment(a.keydir, key, commit, pcommit, model_id=MODEL_ID, weights_hash=whash)
                _res_body = {"nonce": res.sealed_result.nonce.hex(), "ct": res.sealed_result.ct.hex(),
                             "in_tok": res.in_tok, "out_tok": res.out_tok}   # real tokens (per-token pricing)
                if not relay.put(a.relay, "res", key, _res_body):
                    # THE SEALED RESPONSE IS KEPT (JOB_FAILURES): the next attempt deposits it again, never a new
                    # inference. It is sealed to the client's key: this process keeps bytes it cannot read.
                    _job_count("deposit_refused")
                    _fe = note_job_failure(_fails, key, "deposit", "the relay refused the sealed response",
                                           time.time(), sealed=_res_body)
                    _write_failures(a.keydir, _fails)
                    _status_write(job={"at": int(time.time()), "job": jid,
                                       "result": "response refused by the relay", "gen_s": _gen_s,
                                       "attempt": _fe["failures"], "next_at": int(_fe["next_at"])})
                    print(f"[daemon] {a.id} job {jid}: the relay REFUSED the sealed response -> commit NOT "
                          f"anchored, job NOT marked done; the SAME sealed response is offered again in "
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
                        print(f"[daemon] {a.id} create-commit job {jid} FAILED ({_nc}c): {_anchor_why}")
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
                    # The commitment is ON THE CHAIN: the journal may forget it. It is never cleared on
                    # a hope, only on a measurement.
                    _forget_commitment(a.keydir, key)
                    _status_write(job={"at": int(time.time()), "job": jid, "result": "anchored",
                                       "gen_s": _gen_s, "out_tok": getattr(res, "out_tok", 0),
                                       "wait_s": _wait_s, "late": None if _timing is None else _timing == "late"})
                    _bal = bal_token(addr)
                    print(f"[daemon] {a.id} processed job {jid} -> sealed response + proof ANCHORED "
                          f"(answered in {_gen_s} s, {getattr(res, 'out_tok', 0)} tokens; balance: "
                          f"{'?' if _bal is None else _bal} token)")
                else:
                    _status_write(job={"at": int(time.time()), "job": jid, "result": "commit not anchored",
                                       "why": "three anchoring attempts refused; the commitment is journalled",
                                       "gen_s": _gen_s, "wait_s": _wait_s,
                                       "late": None if _timing is None else _timing == "late"})
                    # ⛔ THE INFERENCE IS OVER, AND SAYING OTHERWISE COSTS THE OPERATOR THE DIAGNOSIS. The
                    # sealed response IS deposited, so the key appears in the relay's `res` listing, and the
                    # top of this loop never generates again a key that appears there: a later inference
                    # would differ, and an LLM does not return the same answer twice, so it would anchor a
                    # commitment over an answer the client can never fetch. The response is sealed to the
                    # client's key, so this process cannot read back the one it stored in order to commit to
                    # THAT. What is retried is the ANCHORING of the journalled commitment (the resumption).
                    print(f"[daemon] {a.id} job {jid}: response posted, COMMIT NOT anchored -> the commitment is "
                          f"JOURNALLED; the next pass retries the ANCHORING ONLY, never the inference (an LLM "
                          f"does not answer twice the same, and the deposit is sealed to the client). Fix what "
                          f"refused the transaction; nothing is lost.")
        except Exception as e:
            print(f"[daemon] {a.id} loop: {type(e).__name__}: {e}")
            # Recorded, and the pass still ends with the write below: a loop that raises on every pass
            # is turning, and its heartbeat says both that it turns and what it keeps hitting.
            _status_write(loop_error={"at": int(time.time()), "what": f"{type(e).__name__}: {e}"[:300]})
        # The availability proof is no longer tried here: it comes FIRST (_presence_turn, at the head of the pass
        # and between two requests, on its clock).
        if not vsk and time.time() - _novrf_said > 3600:
            _novrf_said = time.time()
            _status_note(presence={"at": int(time.time()), "result": "no VRF key",
                                   "why": "this node holds no VRF key, so it proves no availability window"})
            print(f"[daemon] {a.id} has NO VRF key: it proves no availability window. Past the grace a "
                  f"newcomer gets, the chain draws only present miners for work (ADR-048), so it gets no "
                  f"more work and earns nothing from the Final Testnet Season. See the VRF message "
                  f"printed at start-up for what anchoring a key takes for this identity.", flush=True)
        if time.time() - _subsidy_at > 1800:
            _subsidy_at = time.time()
            try:
                print(f"[daemon] {a.id} {maybe_claim_subsidy(a.id)}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[daemon] {a.id} subsidy claim skipped ({type(e).__name__})", flush=True)
        # THE OPERATOR, READ AGAIN (check_operator): a mismatch fixed on chain clears from the heartbeat, one that
        # appears is said. Same cadence as the subsidy: one registry read per half hour.
        if time.time() - _op_at > 1800:
            _op_at = time.time()
            try:
                _op_state = check_operator(a.id, addr, os.environ.get("DENDRA_SLOT"), _op_state)
            except Exception as e:  # noqa: BLE001 -- a read that fails is read again, never fatal
                print(f"[daemon] {a.id} operator check skipped ({type(e).__name__})", flush=True)
            # Same cadence: the ENCRYPTION KEY anchored (a mismatch fixed on chain resumes the proofs, one that
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
        # THE REGISTRATION IS REPLAYED until the chain records this miner (see register_attempt). Its own try:
        # an attempt that raises is retried at the next due time, never a loop that stops. A DRAINING miner is on
        # its way out: it does not register.
        if not owner and reg_state != REGISTERED and not _draining and time.time() >= reg_retry_at:
            try:
                reg_state, reg_why, reg_retry_at = registration_step(a, addr, mypub, vpk)
                if reg_state == REGISTERED:
                    print(_ready_line(), flush=True)
                    _enc_state = check_enc_key(a.id, mypub, _enc_state)
                    if not _deposited:
                        _bootstrap_deposits()
                        _deposited = True
                else:
                    print(not_registered_line(a.id, reg_why, reg_retry_at - time.time()), flush=True)
            except Exception as e:  # noqa: BLE001 -- the attempt is replayed; the loop goes on
                reg_retry_at = time.time() + REGISTER_RETRY_S
                print(f"[daemon] {a.id} registration attempt skipped ({type(e).__name__}); next one in "
                      f"{int(REGISTER_RETRY_S)} s.", flush=True)
        # THE PASS IS OVER, WHATEVER IT MET: one write per pass, after the error above if there was one.
        _status_write(loop_at=int(time.time()), phase="loop", draining=_draining)
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
