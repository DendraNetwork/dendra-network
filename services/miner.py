#!/usr/bin/env python3
"""Persistent LOCAL miner (the real Dendra miner client).

At startup: creates its encryption key (X25519, local) + publishes its pub to the relay; creates its
CHAIN key, funds itself at the FAUCET, and registers on-chain by SIGNING itself.
Then LOOPS: fetches from the relay the jobs assigned to it -> decrypts in locked memory
-> infers on OLLAMA (GPU) -> returns the SEALED response -> anchors its content_commit on-chain.
Content stays encrypted; only metadata + hash go on-chain. It earns `token` on each
honest verdict (payout from the client's escrow).

Usage: python3 miner.py --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import io
import json
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
from modea import relay_signature
from modea import dendrad_argv as da
from modea.crypto import Sealed
from modea.miner import Miner
import relay_client as relay
# The faucet PoW contract (digest, verifier, solver) is defined ONCE, in the service that enforces it.
# The solver is imported rather than reimplemented as a twin: a twin that drifts on a separator or an
# encoding makes the faucet unreachable, and neither side reports it.
import faucet as faucet_pow

CHAIN = "dendra"
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


def _kr():
    """Keyring flags common to EVERY command that signs or reads a key."""
    f = ["--keyring-backend", "test"]
    if KEYRING_DIR:
        f += ["--keyring-dir", KEYRING_DIR]
    return f
_RE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # sane relay key (anti dendrad injection). Dash allowed (IDs like "miner-A") but NEVER leading -> a key cannot be taken for a dendrad flag.


def _node():
    return ["--node", NODE] if NODE else []


def model_weights_hash():
    """Digest of the served model's weights (Ollama manifest) -> binds the commit to
    the artifact. Best-effort: "" if Ollama unreachable/model absent (the on-chain check only bites if
    enforce_model_registry=ON AND the registry has a non-empty weights anchor)."""
    if not MODEL_ID:
        return ""
    ep = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(ep + "/api/tags", timeout=10) as r:
            data = json.loads(r.read())
        for m in data.get("models", []):
            if m.get("name") == MODEL_ID or m.get("model") == MODEL_ID:
                dg = m.get("digest", "")
                return dg.split(":")[-1] if dg else ""
    except Exception:
        return ""
    return ""


def run(c, t=600):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def tx_from(frm, sub, *positionals, flags=()):
    # robust NONCE: a miner-judge shares 1 account across 3 processes (commit/reveal/verdict)
    # -> 2 close tx pull the SAME sequence -> "account sequence mismatch". We RETRY: dendrad re-fetches the
    # sequence each attempt (online), so once the in-flight tx is included, the retry passes. Bounded backoff (~14 s max).
    # ⛔ POSITIONALS GO AFTER `--`. The content commitment is an embedding vector whose first number is
    # negative about half the time; spliced in front of the terminator, pflag reads it as options and
    # the commit is NEVER anchored. See modea/dendrad_argv.py for the measurement and the precedent.
    cmd = da.dendrad_argv(
        ("dendrad", "tx", "jobs"), sub, positionals,
        [*flags, "--from", frm, *_kr(), "--chain-id", CHAIN,
         "--gas", "auto", "--gas-adjustment", "1.6", "--yes", *_node()])
    o = ""
    for attempt in range(6):
        o = run(cmd)
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


def _ok(t):
    m = re.search(r'(^|\n)code: (\d+)', t)
    return bool(m) and m.group(2) == "0"


def wait_tx(o, timeout=24):
    if not _ok(o):
        return False
    h = re.search(r'txhash:\s*([A-Fa-f0-9]{64})', o)
    if not h:
        return False
    for _ in range(timeout):
        q = run(["dendrad", "query", "tx", h.group(1), *_node()])
        m = re.search(r'(^|\n)height:\s*"?(\d+)"?', q)
        if m and int(m.group(2)) > 0:
            return _ok(q)
        time.sleep(2)
    return False


def keys_addr(name):
    show = run(["dendrad", "keys", "show", name, "-a", *_kr()])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    if m:
        return m.group(1)
    run(["dendrad", "keys", "add", name, *_kr()])
    show = run(["dendrad", "keys", "show", name, "-a", *_kr()])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    return m.group(1) if m else ""


def align_identity(name, addr):
    """Return the identifier the CHAIN will accept for `addr`, renaming the keyring key to match.

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
        attendu = miner_id_for_account(addr)
    except Exception as e:
        print(f"[daemon] cannot derive the on-chain identifier ({type(e).__name__}: {e}).\n"
              f"         Keeping '{name}'. If the chain refuses the registration, this is why.", flush=True)
        return name
    if name == attendu:
        return name

    # Is the target name ALREADY taken, and by whom? Adopting a key that is not ours would hand us an
    # identity signing with a different address — every commit refused, and nothing saying why.
    show = run(["dendrad", "keys", "show", attendu, "-a", *_kr()])
    m = re.search(r'(dendra1[0-9a-z]+)', show)
    if m and m.group(1) != addr:
        print(f"[daemon] REFUSING to adopt the derived identifier {attendu}: a key of that name already\n"
              f"         exists in this keyring and holds a DIFFERENT address ({m.group(1)}).\n"
              f"         Keeping '{name}'. Resolve the keyring by hand before restarting.", flush=True)
        return name
    if m:
        print(f"[daemon] on-chain identity is {attendu} (key already present); adopting it.", flush=True)
        return attendu

    out = run(["dendrad", "keys", "rename", name, attendu, "-y", *_kr()])
    show = run(["dendrad", "keys", "show", attendu, "-a", *_kr()])
    if re.search(r'(dendra1[0-9a-z]+)', show or ""):
        print(f"[daemon] identity aligned with the chain: '{name}' -> {attendu}\n"
              f"         (the chain DERIVES the miner id from the signer address and refuses any other)",
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


def _journal_commitment(keydir, key: str, commit: str, pcommit: str) -> None:
    """Makes the commitment durable BEFORE the deposit and BEFORE the anchoring.

    ATOMIC WRITE. A half-written journal would be worse than none: the resumption would read a
    truncated commitment and anchor something other than what the client can open."""
    if not commit:
        return
    d = _read_journal(keydir)
    d[key] = {"commit": commit, "pcommit": pcommit or commit, "ts": int(time.time())}
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


def bal_token(addr):
    try:
        d = json.loads(run(["dendrad", "query", "bank", "balances", addr, "--output", "json", *_node()]))
        for c in d.get("balances", []):
            if c.get("denom") == "udndr":
                return int(c["amount"])
    except Exception:
        return 0
    return 0


def stake_of(mid):
    m = re.search(r'stake:\s*"?(\d+)"?', query("get-miner", mid))
    return int(m.group(1)) if m else -1


def miner_operator(mid):
    """Address of the OPERATOR registered for this miner_id ("" if the miner does not exist)."""
    m = re.search(r'operator:\s*"?(dendra1[0-9a-z]+)"?', query("get-miner", mid))
    return m.group(1) if m else ""


def miner_vrf_onchain(mid):
    """The vrf_pubkey ANCHORED on-chain for this miner ("" if none, which is the zero value of a string).

    ⚠️ Read from the chain, never assumed from what this process holds locally. A key generated here is
    not a key the chain knows: anchoring happens at registration, and a miner already in the registry
    keeps whatever it registered with until an explicit rotation."""
    m = re.search(r'vrf_pubkey:\s*"?([0-9a-fA-F]*)"?', query("get-miner", mid))
    return m.group(1) if m else ""


def chain_min_stake(default=50000):
    """min_stake READ FROM THE CHAIN, never a local constant.

    `min_stake` is a GOVERNABLE parameter: any value hardcoded here diverges as soon as a vote changes
    it. A local default below the real min_stake makes `create-miner` REJECTED on every attempt; if the
    failure is not printed, the miner announces "ready / waiting for jobs" while NOT existing in the
    registry, and the participant has no way to understand — the GPU runs, the chain ignores it.
    """
    try:
        m = re.search(r'min_stake:\s*"?(\d+)"?', query("params"))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return default


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


def _faucet_post(faucet, addr, nonce):
    """Post the request. Returns (ok, detail, required_bits) — required_bits = 0 when the server names none.

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
        return True, "", 0
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
        return False, f"HTTP {e.code} {raw}".strip(), bits
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", 0


def faucet_fund(faucet, addr):
    """Fund the address at the faucet. Returns (ok, detail) — a failure is ALWAYS named.

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
            return False, f"faucet PoW unsolved within {POW_MAX_S:.0f} s ({bits} bits)"
    ok, detail, want_bits = _faucet_post(faucet, addr, nonce)
    if ok:
        return True, ""
    # The server names a difficulty that was not honoured (GET probe unavailable, or difficulty raised
    # between the probe and the request) -> a single retry, with the required token.
    if want_bits > 0 and not nonce:
        nonce = _solve_faucet_pow(addr, want_bits)
        if not nonce:
            return False, f"faucet PoW unsolved within {POW_MAX_S:.0f} s ({want_bits} bits)"
        ok, detail, _ = _faucet_post(faucet, addr, nonce)
        if ok:
            return True, ""
    return False, detail


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
            print(f"[daemon] Ollama reachable ({m.backend.endpoint}) -> REAL LLM (GPU)")
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
    """Path of the dendra-vrf binary (built by dendra_modea_vrf_avail.sh). "" if absent."""
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
    register quietly without a key."""
    vbin = _vrf_bin()
    vpath = Path(keydir) / f"{mid}.vrf"
    try:
        if vpath.exists():
            sk = vpath.read_text().strip()
            # THE KEY GOES THROUGH THE ENVIRONMENT, NEVER argv: /proc/<pid>/cmdline and `ps` are
            # world-readable, and none of the confinement this daemon applies covers that channel.
            pk = subprocess.run([vbin, "pubkey"], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "DENDRA_VRF_SK": sk}).stdout.strip()
        else:
            out = subprocess.run([vbin, "keygen"], capture_output=True, text=True, timeout=10).stdout.strip()
            sk, pk = out.split()
            vpath.write_text(sk)
            try:
                os.chmod(vpath, 0o600)
            except Exception:
                pass
        if len(sk) == 128 and len(pk) == 64:   # 64-byte sk / 32-byte pk, hex-encoded
            return sk, pk
    except Exception as e:
        print(f"[daemon] VRF unavailable ({type(e).__name__}): dendra-vrf could not be run. "
              f"This miner will register WITHOUT a vrf_pubkey.")
        return "", ""
    # Reached when the binary RAN but produced key material of the wrong size. Returning here without a
    # word is the worse of the two failures: an operator who sees no message assumes there was nothing to
    # see. A component that degrades must say it degraded.
    print("[daemon] VRF key material has an unexpected size -> registering WITHOUT a vrf_pubkey.")
    return "", ""


def prove_availability_once(mid, vsk, last_chal=""):
    """Best-effort: if availability is ON (challenge present) and new, proves presence with a VRF PROOF.
    Never raises (must not disturb the inference loop). Returns the proven challenge (or last_chal)."""
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
            return last_chal
        # proto3 omits a string at its zero value, so an absent `challenge` IS the empty challenge:
        # availability is off. That default is only safe because the failing query is ruled out above.
        chal = reponse.get("challenge", "")
        if not chal or chal == last_chal:
            return last_chal   # availability OFF, or challenge already proven
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
            if not wait_tx(out):
                print(f"[daemon] {mid} availability proof REFUSED for challenge {chal[:12]}... "
                      f"({_tx_err(out)}) -> NOT marked proven, will retry")
                return last_chal
            print(f"[daemon] {mid} availability PROVEN (VRF) for challenge {chal[:12]}...")
            return chal
    except Exception as e:
        print(f"[daemon] {mid} availability proof skipped ({type(e).__name__})")
    return last_chal


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

    # PROCESS confinement at startup (anti same-user debugger, no core
    # dump, no-new-privs; mlockall opt-in). Best-effort, NEVER fatal. OFF via DENDRA_CONFINE=0.
    # STRONG OS confinement (routeless netns, seccomp, egress) is applied via modea_confine.sh.
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

    addr = keys_addr(a.id)
    a.id = align_identity(a.id, addr)

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

    # --- encryption identity (X25519): persisted ENCRYPTED at rest ---
    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    if not passphrase:
        print("[daemon] WARNING: DENDRA_MINER_PASSPHRASE is not set -> key stored IN CLEAR (mode 0600). "
              "Set it to encrypt the key at rest (a disk compromise means stolen prompts and funds).")
    if skpath.exists():
        sk = crypto.load_sk(str(skpath), passphrase)
    else:
        sk, _ = crypto.gen_keypair()
        crypto.save_sk(sk, str(skpath), passphrase)
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

    # THE FIRST WRITE THIS DAEMON EVER ATTEMPTS, AND ITS RESULT WAS THROWN AWAY.
    # The neighbour sixteen lines down tests its deposit and prints the refusal; this one did not, so a
    # relay that refused the key answered into a void and the daemon carried on registering. That
    # silence is not a missing log line: it is what makes the relay's WRITE policy unmeasurable from
    # the only place that exercises it. An operator starting a node is the first party to touch that
    # policy, and until now they were the last to hear about it.
    #
    # It stays non-fatal. This key is a FALLBACK: the source of truth is the `enc_pubkey` anchored
    # on chain (`reveal_helpers.committee_pubs`, and the client refuses an unanchored miner outright in
    # strict mode), so a refused deposit costs a cross-check, not the ability to mine. Turning it fatal
    # would stop a miner that has everything it needs.
    if relay.put(a.relay, "pub", a.id, {"pub": mypub}):
        print(f"[daemon] encryption pubkey published to the relay for {a.id}")
    else:
        print(f"[daemon] the relay REFUSED the pubkey deposit for {a.id}. Mining is NOT blocked -- the "
              f"key anchored on chain is what peers use -- but the relay copy will be missing, so the "
              f"client-side cross-check against it is not available. If deposits are refused here, they "
              f"will be refused for reveals too: check the relay's write policy before assuming this is "
              f"cosmetic.", flush=True)

    # SIGNED software attestation (measured hash = code + model_id + weights_hash + confinement),
    # published to the relay. A relay with the gate active (DENDRA_ATTEST_REQUIRE=1 + allow-list) only assigns
    # a confidential job to attested miners. Best-effort (never fatal). HONEST: deterrence, not
    # a proof of execution (cf. modea/confine.py). The measured hash is printed -> to add to the allow-list.
    try:
        from modea import confine as _confine
        _wh = model_weights_hash()
        _ask, _apub = _confine.load_or_create_attest_key(a.keydir, a.id)
        _att = _confine.signed_attestation(_ask, miner_id=a.id, model_id=MODEL_ID,
                                           weights_hash=_wh, enc_sk=sk)
        # "published" is a claim about the RELAY, so it is printed only when the relay accepted. A
        # deposit is refused for reasons that occur (unsigned write, saturated store), and a refusal
        # leaves this miner OUT of the attested set: with the gate armed (DENDRA_ATTEST_REQUIRE=1) it
        # is assigned no confidential job. That has to be readable in the log, not contradicted by it.
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
    vsk, vpk = vrf_identity(a.keydir, a.id)   # VRF identity (proof of availability)
    # REGISTERED IDENTITY != CURRENT KEY -> every commit will be refused.
    # `stake_of()` only checks that the miner_id EXISTS, not WHO owns it: an operator who lost their
    # keyring (volume recreated, machine reinstalled) believes they are registered — stake present,
    # "miner ready" — and then every commit is rejected by the chain with "only the miner's operator may
    # anchor its commit". The GPU runs, jobs are processed, nothing is ever paid. The chain is right to
    # refuse; it is the daemon's job to SAY so before mining for nothing.
    _op = miner_operator(a.id)
    if _op and _op != addr:
        print(f"[daemon] IDENTITY MISMATCH: '{a.id}' is registered on-chain for the operator\n"
              f"         {_op}\n"
              f"         but this machine signs with\n"
              f"         {addr}\n"
              f"         -> the chain will REFUSE every commit (unauthorized). Two ways out:\n"
              f"         (a) restart with a NEW identifier      : deploy/join.sh --id <new-id>\n"
              f"         (b) restore the original key of {_op} into the keyring.", flush=True)
    if stake_of(a.id) <= 0:
        # A FAUCET REFUSAL MUST BE SAID. Without funds, `create-miner` is rejected and the miner runs
        # outside the registry: the diagnostic must name the faucet, not suggest a chain problem.
        _fok, _fwhy = faucet_fund(a.faucet, addr)
        if not _fok:
            print(f"[daemon] FAUCET REFUSED ({a.faucet}) for {addr}: {_fwhy}\n"
                  f"         Without funds the on-chain registration is REJECTED and this miner will "
                  f"receive no job. Check the faucet URL, or ask the operator for funding.",
                  flush=True)
        else:
            # Wait for the credit only if the request was ACCEPTED: waiting after a refusal burns 40 s
            # at every startup to watch a balance that will not move.
            for _ in range(20):
                if bal_token(addr) > 0:
                    break
                time.sleep(2)
        _min = chain_min_stake()
        _stake = os.environ.get("DENDRA_MINER_STAKE") or str(_min)
        if int(_stake) < _min:
            print(f"[daemon] WARNING: DENDRA_MINER_STAKE={_stake} < the chain's min_stake ({_min}) "
                  f"-> create-miner would be REJECTED. Forcing {_min}.")
            _stake = str(_min)
        _out = ""
        for _ in range(3):
            if stake_of(a.id) > 0:
                break
            # we ANCHOR the X25519 pub on-chain (5th arg), signed by the miner's
            # Cosmos key -> the client will encrypt to THIS pub (anti relay-MITM).
            reg = ["create-miner", a.id, addr, "eu", _stake, mypub]
            reg_flags = []
            if vpk:
                reg_flags += ["--vrf-pubkey", vpk]   # vrf_pubkey as a FLAG (not a 2nd optional positional)
            else:
                # THE OMISSION MUST BE SAID, and it must name what it costs. vrf_pubkey is anchored AT
                # REGISTRATION; skipping it here is not deferred, it is decided. A miner then serves jobs
                # perfectly while being permanently unable to prove availability, and nothing connects the
                # two facts. The command is printed COMPLETE, with this miner's real id and key path
                # already substituted: a message carrying `<placeholders>` gets pasted verbatim.
                print(f"[daemon] REGISTERING WITHOUT A VRF KEY (dendra-vrf not found in this image).\n"
                      f"         Under verification_mode=1 the chain REFUSES availability proofs from a\n"
                      f"         miner with no anchored vrf_pubkey (ADR-022) and pays it no availability\n"
                      f"         share. Inference, commits and payouts are UNAFFECTED.\n"
                      f"         Fix: rebuild the miner image so it ships dendra-vrf, restart, then anchor\n"
                      f"         the key -- re-registration will NOT do it, this id being already in the\n"
                      f"         registry:\n"
                      f"           dendrad tx jobs rotate-miner-keys {a.id} "
                      f"--new-vrf-pubkey $(dendra-vrf pubkey $(cat {a.keydir}/{a.id}.vrf))", flush=True)
            _out = wait_tx(tx_from(a.id, *reg, flags=reg_flags)) or ""
            time.sleep(2)
        # THE FAILURE MUST BE VISIBLE. Without this block, `create-miner` can fail three times in
        # silence while the daemon still prints "miner ready / waiting for jobs": a miner absent from
        # the registry receives NO job, and nothing says so. A component that fails must say it,
        # otherwise the user pays for the diagnosis.
        if stake_of(a.id) <= 0:
            print(f"[daemon] ON-CHAIN REGISTRATION FAILED (stake={_stake}, min_stake={_min}, balance={bal_token(addr)}). "
                  f"This miner will receive NO job until it is in the registry.")
            if _out:
                print(f"[daemon]   last response from the chain: {str(_out)[:400]}")

    # ⚠️ THIS CHECK LIVES OUTSIDE THE REGISTRATION BLOCK, AND THAT IS THE WHOLE POINT.
    # The omission notice above only fires while REGISTERING. A miner ALREADY registered without a key
    # never goes through it again -- which is precisely the population that has the problem, and the only
    # one that never heard about it. Re-registering does not fix it either: the id being in the registry,
    # `create-miner` is not even replayed.
    # So the chain's REAL state is read at every startup and compared with what this node holds.
    _vrf_chain = miner_vrf_onchain(a.id)
    if not _vrf_chain:
        if vpk:
            # This node has the key, the chain does not: one gesture is missing, printed COMPLETE.
            print(f"[daemon] NO VRF KEY ANCHORED ON-CHAIN for {a.id}, though this node holds one.\n"
                  f"         Availability proofs are refused under verification_mode=1 until it is\n"
                  f"         anchored, and re-registration will not do it. One command:\n"
                  f"           dendrad tx jobs rotate-miner-keys {a.id} --new-vrf-pubkey {vpk}", flush=True)
        else:
            print(f"[daemon] NO VRF KEY, on-chain or locally (dendra-vrf not found in this image).\n"
                  f"         This miner serves jobs normally but can never prove availability, and is\n"
                  f"         paid no availability share. Rebuild the image so it ships dendra-vrf, restart,\n"
                  f"         then anchor the key this node will generate.", flush=True)
    elif vpk and _vrf_chain.strip().lower() != vpk.strip().lower():
        # THE FOURTH COMBINATION, AND THE ONLY ONE THAT LOOKS HEALTHY. Both keys exist, so neither
        # branch above fires; the chain refuses only an ABSENT key, so nothing complains at startup
        # either. What fails is the PROOF: it is produced with the key this node holds and verified
        # against the one the chain anchored, so every availability transaction is rejected for as
        # long as the two differ. Reading a value and testing only whether it is EMPTY is not
        # comparing it. The diagnosis is worth more here than at proof time: the remedy is one
        # command, and the miner loses its availability share for every epoch it runs without it.
        print(f"[daemon] VRF KEY MISMATCH for {a.id}: the chain has one key anchored, this node holds\n"
              f"         another. Availability proofs are produced with the local key and verified\n"
              f"         against the anchored one, so every one of them is refused. One command:\n"
              f"           dendrad tx jobs rotate-miner-keys {a.id} --new-vrf-pubkey {vpk}", flush=True)

    backend = pick_backend(a.backend)
    miner = Miner(a.id, backend=backend, hardened=True, sk=sk)
    whash = model_weights_hash()
    if MODEL_ID:
        print(f"[daemon] model_id={MODEL_ID}  weights_hash={(whash[:16]+'...') if whash else '<absent>'}")
    print(f"[daemon] miner {a.id} ready  addr={addr}  stake={stake_of(a.id)}  backend={backend}")
    print(f"[daemon] waiting for jobs at the relay {a.relay} ...")

    done = set()
    suffix = "__" + a.id
    _avail_tick = 0
    _last_chal = ""
    while True:
        try:
            lst = relay.listing(a.relay)
            ress = set(lst.get("res", []))
            for key in lst.get("req", []):
                # 17: a job whose response is deposited BUT whose commitment is still pending is no
                # longer skipped -- it is exactly the one to resume, and the resumption recomputes
                # nothing: it replays the anchoring with the journalled commitment.
                _pending = _pending_commitment(a.keydir, key)
                if key in done or (not key.endswith(suffix)) or (key in ress and not _pending):
                    continue
                if _pending and key in ress:
                    # RESUMPTION. The response is already at the relay and its commitment is journalled:
                    # only the anchoring is replayed. Re-running inference would produce a different
                    # answer, and the deposit is write-once and sealed to the client.
                    _c, _pc = _pending.get("commit", ""), _pending.get("pcommit", "")
                    if _c and _c in query("get-commit", key):
                        _forget_commitment(a.keydir, key)
                        done.add(key)
                        continue
                    _out = tx_from(a.id, "create-commit", key, _pc or _c, _c, "infer")
                    if wait_tx(_out) and _c in query("get-commit", key):
                        _forget_commitment(a.keydir, key)
                        done.add(key)
                        print(f"[daemon] {a.id} job {key}: anchoring RESUMED from the journal (inference NOT replayed)")
                    else:
                        print(f"[daemon] {a.id} job {key}: anchoring still refused -> the commitment STAYS"
                              f" in the journal; the next pass retries without recomputing")
                    continue
                if not _RE_KEY.match(key):       # unsafe relay key -> ignore (anti dendrad injection)
                    continue
                jid = key[: -len(suffix)]
                req = relay.get(a.relay, "req", key)
                if not req:
                    continue
                # ONE MALFORMED DEPOSIT MUST NOT STARVE EVERY JOB BEHIND IT, so the decoding of the
                # request body is caught HERE rather than left to the loop's own `except`. A `req`
                # missing `client_eph_pk`, or carrying a value that is not hex, raises: caught at the
                # loop level it would end the whole `for`, and since the listing is walked in
                # insertion order and the key is WRITE_ONCE, the same deposit would stop the same turn
                # at the same position on every pass -- starving every later job until the relay's
                # retention expires it, with no message naming any of them. A body we cannot parse is
                # one job we skip, not a turn we abandon.
                try:
                    eph = bytes.fromhex(req["client_eph_pk"])
                    sealed = Sealed(bytes.fromhex(req["nonce"]), bytes.fromhex(req["ct"]))
                except Exception as e:
                    print(f"[daemon] {a.id} job {jid}: unusable request body "
                          f"({type(e).__name__}) -> skipped, the other jobs continue")
                    continue
                try:
                    res = miner.handle_job(jid, eph, sealed, max_out=int(req.get("max_out", 0)))  # requested cap
                except Exception as e:
                    print(f"[daemon] {a.id} inference failed for job {jid}: {type(e).__name__}")
                    continue
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
                _journal_commitment(a.keydir, key, commit, pcommit)
                if not relay.put(a.relay, "res", key,
                                 {"nonce": res.sealed_result.nonce.hex(), "ct": res.sealed_result.ct.hex(),
                                  "in_tok": res.in_tok, "out_tok": res.out_tok}):  # real tokens (per-token pricing)
                    print(f"[daemon] {a.id} job {jid}: the relay REFUSED the sealed response -> commit NOT "
                          f"anchored, job NOT marked done; it is retried on the next pass")
                    continue
                flags = (["--model-id", MODEL_ID] if MODEL_ID else []) + (["--weights-hash", whash] if whash else [])
                anchored = bool(commit) and (commit in query("get-commit", key))
                for _ in range(3):
                    if anchored:
                        break
                    out = tx_from(a.id, "create-commit", key, pcommit, commit, "infer", flags=flags)
                    if not wait_tx(out):
                        _nc = (commit.count(",") + 1) if commit else 0
                        print(f"[daemon] {a.id} create-commit job {jid} FAILED ({_nc}c): {_tx_err(out)}")
                        print(f"[daemon]   commit[:90]={commit[:90]!r}")
                        print(f"[daemon]   out[:450]={out[:450]!r}")
                    time.sleep(2)
                    anchored = commit in query("get-commit", key)
                if anchored:
                    done.add(key)
                    # The commitment is ON THE CHAIN: the journal may forget it. It is never cleared on
                    # a hope, only on a measurement.
                    _forget_commitment(a.keydir, key)
                    print(f"[daemon] {a.id} processed job {jid} -> sealed response + proof ANCHORED "
                          f"(balance: {bal_token(addr)} token)")
                else:
                    # ⛔ THIS JOB IS OVER, AND SAYING OTHERWISE COSTS THE OPERATOR THE DIAGNOSIS. The
                    # sealed response IS deposited, so the key appears in the relay's `res` listing,
                    # and the top of this loop skips every key that appears there -- deliberately, and
                    # correctly: a later pass would re-run inference, and an LLM does not return the
                    # same answer twice, so it would anchor a commitment over an answer the client can
                    # never fetch. The response is sealed to the client's key, so this process cannot
                    # read back the one it stored in order to commit to THAT. Nothing here retries.
                    print(f"[daemon] {a.id} job {jid}: response posted, COMMIT NOT anchored -> the commitment is "
                          f"JOURNALLED; the next pass retries the ANCHORING ONLY, never the inference (an LLM "
                          f"does not answer twice the same, and the deposit is sealed to the client). Fix what "
                          f"refused the transaction; nothing is lost.")
        except Exception as e:
            print(f"[daemon] {a.id} loop: {type(e).__name__}: {e}")
        # prove availability periodically (best-effort; no-op if availability OFF).
        _avail_tick += 1
        if vsk and _avail_tick % 4 == 0:
            _last_chal = prove_availability_once(a.id, vsk, _last_chal)
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
