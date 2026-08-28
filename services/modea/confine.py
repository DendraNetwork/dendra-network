"""PROCESS-level confinement of the Mode A miner — the counterpart of `hardening.py`.

`hardening.py` hardens inference at the APPLICATION level and PER BUFFER (SecureBytes mlock plus
zeroisation, ConfidentialGuard no-egress/no-disk during a job). This module adds the hardening of the
WHOLE PROCESS, applied ONCE at miner daemon startup:

  - PR_SET_DUMPABLE = 0   -> the kernel refuses ptrace attachment from a debugger owned by the SAME
                            (non-root) user, and writes no core dump (memory in the clear) to disk.
                            That is precisely the residual risk named in MODE-A-SECURITE ("attach a
                            debugger to the process"). Note that root CAN still override this, so only
                            Mode B (MPC) or a TEE closes that hole. Stated honestly: this raises the bar
                            against same-user or accidental snooping, not against root.
  - PR_SET_NO_NEW_PRIVS=1 -> the process (and its children) can NO LONGER gain privileges (setuid,
                            capabilities), so an exfiltration cannot escalate.
  - RLIMIT_CORE = 0       -> belt and braces with DUMPABLE=0: no core dump.
  - mlockall (opt-in)     -> locks ALL the process memory, not only the SecureBytes: activations and
                            KV-cache outside SecureBytes do not reach swap. Expensive on an ML process
                            (and it can fail without CAP_IPC_LOCK / RLIMIT_MEMLOCK), so it is OFF by
                            default and enabled by DENDRA_MLOCKALL=1. Best effort, reported honestly.
  - umask 0077            -> every created file is private (0600) by default.

Everything is BEST EFFORT and cross-platform: on Windows, or without libc, the unavailable controls are
simply reported as `False`. No call is fatal (the miner starts anyway, reporting what could be applied)
and the confinement attestation exposes the real, verifiable state.

STRONG OS confinement (network namespace with no route except the relay, seccomp, read-only FS) belongs
to the OS and is set up by the `modea_confine.sh` (bubblewrap) and `modea_egress.sh` (nft firewall)
launchers.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import sys

# Linux prctl constants (asm-generic/uapi). Stable values of the Linux ABI.
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_MCL_CURRENT = 1
_MCL_FUTURE = 2


def _libc():
    if sys.platform.startswith("win"):
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except Exception:
        return None


def _prctl(libc, option: int, arg2: int = 0) -> bool:
    try:
        return libc.prctl(option, arg2, 0, 0, 0) == 0
    except Exception:
        return False


def set_non_dumpable() -> bool:
    """PR_SET_DUMPABLE=0: no same-user ptrace, no core dump. Linux only."""
    libc = _libc()
    if libc is None or platform.system() != "Linux":
        return False
    return _prctl(libc, _PR_SET_DUMPABLE, 0)


def set_no_new_privs() -> bool:
    """PR_SET_NO_NEW_PRIVS=1: forbids any later privilege escalation. Linux only."""
    libc = _libc()
    if libc is None or platform.system() != "Linux":
        return False
    return _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1)


def disable_core_dumps() -> bool:
    """RLIMIT_CORE = 0 (POSIX)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return True
    except Exception:
        return False


def lock_all_memory() -> bool:
    """mlockall(MCL_CURRENT|MCL_FUTURE): locks all memory (anti-swap). Best effort."""
    libc = _libc()
    if libc is None:
        return False
    try:
        return libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) == 0
    except Exception:
        return False


def apply_process_confinement(mlockall: bool | None = None) -> dict:
    """Applies the process hardening at startup and returns an HONEST report of the real state.

    `mlockall`: None -> read DENDRA_MLOCKALL (OFF by default, because it is costly on an ML process)."""
    if mlockall is None:
        mlockall = os.environ.get("DENDRA_MLOCKALL", "0") == "1"
    try:
        os.umask(0o077)
        umask_ok = True
    except Exception:
        umask_ok = False
    rep = {
        "platform": platform.system(),
        "non_dumpable": set_non_dumpable(),       # blocks a same-user debugger and core dumps
        "no_new_privs": set_no_new_privs(),        # no escalation
        "core_dumps_disabled": disable_core_dumps(),
        "umask_0077": umask_ok,
        "mlockall": (lock_all_memory() if mlockall else False),
        "mlockall_requested": bool(mlockall),
    }
    # Residual risk that is ALWAYS true, and said so: a ROOT operator bypasses all of this.
    rep["residual_root_risk"] = True
    rep["note"] = ("best-effort process hardening: raises the bar against same-user, non-root snooping "
                   "and against core dumps; a root operator is still able to read memory (only Mode B "
                   "with MPC, or a TEE, closes that hole).")
    return rep


def confinement_attestation(sk=None) -> dict:
    """Software confinement attestation: measures the code (manifest), the confinement state and the
    egress/disk posture. It PROVES, on a best-effort and non-hardware basis, that a standard, unmodified
    and confined client is running. When `sk` (an X25519 key) is supplied, the public key is attached to
    bind the attestation to the on-chain identity (enc_pubkey). Stated honestly: this is a software
    self-measurement, so it detects modified code or missing confinement, but NOT a post-attestation
    memory patch (TOCTOU) nor a lying root operator. The hardware root of trust (TEE/TPM) is not
    implemented."""
    from . import hardening
    rep = {
        "code_manifest": hardening.build_manifest(),       # sha256 of every .py in the package
        "confinement": apply_process_confinement_report_cached(),
    }
    if sk is not None:
        try:
            from . import crypto
            rep["enc_pubkey"] = crypto.pub_bytes(sk).hex()  # binds the attestation to the on-chain identity
        except Exception:
            pass
    return rep


# Report cache (apply must run only once; the attestation re-reads it without re-applying).
_CACHED_REPORT: dict | None = None


def apply_process_confinement_report_cached() -> dict:
    global _CACHED_REPORT
    if _CACHED_REPORT is None:
        _CACHED_REPORT = apply_process_confinement()
    return _CACHED_REPORT


# =============================================================================
#  MEASURED AND SIGNED SOFTWARE ATTESTATION
#  Purpose: before assigning a "confidential" job, the relay and the chain want evidence that the miner
#  runs a KNOWN CLIENT (measured code), the RIGHT model version and an expected configuration, and that
#  it applied the confinement. A canonical MEASURED HASH is produced over those elements and signed by
#  an Ed25519 key belonging to the miner. The relay VERIFIES it (signature plus expected hash) before
#  the assignment.
#
#  Stated honestly, without overselling: this is software DETERRENCE and DETECTION, NOT a
#  cryptographic guarantee of execution. Without a hardware root of trust (TEE/TPM), a root operator can
#  (a) sign a "clean" hash while running something else, or (b) patch memory AFTER the attestation
#  (TOCTOU). What it does bring: a miner cannot run an ARBITRARY unrecognised binary without declaring
#  it, the administrator has to LIE ACTIVELY (the key is bound to its on-chain identity, so its stake is
#  at risk once a leak is proven), and a version or configuration drift becomes detectable. The real
#  guarantee remains Mode B (MPC) or a hardware TEE in a datacentre.
# =============================================================================

# Attestation schema version: included in the measured hash so that an old and a new format can never
# produce a collision or a confusion.
ATTEST_SCHEMA = "dendra-modea-attest-1"


def measured_hash(*, model_id: str = "", weights_hash: str = "",
                  extra_config: dict | None = None, pkg_dir=None) -> tuple[str, dict]:
    """Computes the MEASURED HASH (sha256) binding: schema + code manifest (sha256 of every .py) +
    model_id + weights_hash + extra configuration + confinement state. Returns (hash_hex, measure).

    `measure` is the canonical dict that is hashed (useful for debugging and for on-chain republication).
    The serialisation is DETERMINISTIC (sorted JSON, compact separators), so the same measured client
    always yields the same hash, comparable with an expected value (allow-list of recognised
    binaries)."""
    from . import hardening
    measure = {
        "schema": ATTEST_SCHEMA,
        "code_manifest": hardening.build_manifest(pkg_dir),
        "model_id": model_id or "",
        "weights_hash": weights_hash or "",
        "config": dict(sorted((extra_config or {}).items())),
        "confinement": apply_process_confinement_report_cached(),
    }
    blob = json.dumps(measure, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest(), measure


def _ed25519_sk_from_seed(seed32: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed32)


def load_or_create_attest_key(keydir: str, miner_id: str) -> tuple[object, str]:
    """Loads (or creates) the miner's Ed25519 ATTESTATION key, distinct from the X25519 encryption key
    and from the Cosmos key. Persisted with mode 0600 under `<keydir>/<miner_id>.attestkey`.
    Returns (sk_ed25519, pubkey_hex). In production this public key is to be anchored on-chain, next to
    enc_pubkey and vrf_pubkey, so that the relay knows WHO signed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    from pathlib import Path
    p = Path(keydir) / f"{miner_id}.attestkey"
    if p.exists():
        seed = bytes.fromhex(p.read_text().strip())
        sk = _ed25519_sk_from_seed(seed)
    else:
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, seed.hex().encode())
        finally:
            os.close(fd)
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return sk, pub


def signed_attestation(attest_sk, *, miner_id: str, model_id: str = "", weights_hash: str = "",
                       extra_config: dict | None = None, enc_sk=None, pkg_dir=None) -> dict:
    """Produces the SIGNED attestation to send to the relay. It contains: miner_id, the measured hash,
    the detailed measure, the Ed25519 public key, optionally the X25519 encryption public key, and the
    Ed25519 SIGNATURE of the hash.

    The relay then verifies (verify_attestation): a valid signature, the hash present in its allow-list
    of recognised binaries, and optionally the public key bound to the on-chain identity. `attest_sk` is
    the attestation key returned by load_or_create_attest_key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    h, measure = measured_hash(model_id=model_id, weights_hash=weights_hash,
                               extra_config=extra_config, pkg_dir=pkg_dir)
    pub = attest_sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    sig = attest_sk.sign(bytes.fromhex(h)).hex()
    att = {
        "schema": ATTEST_SCHEMA,
        "miner_id": miner_id,
        "measured_hash": h,
        "measure": measure,
        "attest_pubkey": pub,
        "signature": sig,
    }
    if enc_sk is not None:
        try:
            from . import crypto
            att["enc_pubkey"] = crypto.pub_bytes(enc_sk).hex()   # binds to the encryption identity
        except Exception:
            pass
    return att


def verify_attestation(att: dict, *, allowed_hashes=None, expected_pubkey: str | None = None,
                       recompute_measure: bool = True) -> tuple[bool, str]:
    """RELAY/CHAIN-SIDE VERIFICATION of a signed attestation. Returns (ok, reason).

    Checks:
      1) the Ed25519 SIGNATURE of `measured_hash` is valid for `attest_pubkey`;
      2) when `recompute_measure` is set, `measured_hash` matches the sha256 of the attached `measure`
         (a miner cannot announce a clean hash together with a dirty measure);
      3) when `allowed_hashes` is supplied, the measured hash MUST appear in it (allow-list of
         recognised binaries and configurations, so a modified or unknown client is refused for
         confidential jobs);
      4) when `expected_pubkey` is supplied, the signing public key MUST match (binding to the on-chain
         identity: only an attestation signed by THIS miner is accepted).

    ⚠️ `expected_pubkey` MUST COME FROM SOMEWHERE ELSE THAN `att`. Passing `att["attest_pubkey"]`
    compares the value to itself: the check can never fail, yet it reads — in the source and in a
    review — like a binding to an identity. A caller with no independent source for the key must pass
    `None` and say so; a control that cannot fail is not a weak control, it is an absent one that gets
    counted. (The relay is exactly that caller: an attestation is signed by an Ed25519 key, and the
    registry it reads holds Cosmos secp256k1 operator addresses, so it has nothing to compare against.)

    Stated honestly: this proves "a measured client X, signed by key Y, was presented", NOT "this
    client is really running and was not patched afterwards" (there is no hardware root of trust)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        h = att["measured_hash"]
        pub_hex = att["attest_pubkey"]
        sig = bytes.fromhex(att["signature"])
    except Exception:
        return False, "malformed attestation (missing fields)"
    if att.get("schema") != ATTEST_SCHEMA:
        return False, f"unexpected schema ({att.get('schema')!r})"
    if expected_pubkey is not None and pub_hex != expected_pubkey:
        return False, "signing pubkey does not match the expected identity"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(sig, bytes.fromhex(h))
    except (InvalidSignature, Exception):
        return False, "invalid signature"
    if recompute_measure and "measure" in att:
        blob = json.dumps(att["measure"], sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(blob).hexdigest() != h:
            return False, "measured hash does not match the attached measure (forged measure)"
    if allowed_hashes is not None and h not in set(allowed_hashes):
        return False, "measured hash absent from the allow-list (unrecognised client or config)"
    return True, "ok"
