"""Miner-client hardening — the application-level half of the Mode A security controls.

Provides:
  - SecureBytes: buffer on LOCKED pages (mlock/VirtualLock), zeroed on exit.
  - disable_core_dumps(): prevents a crash from dumping cleartext memory to disk.
  - build_manifest()/self_attest(): SOFTWARE ATTESTATION — does the running code match the sealed
    binary registered on-chain? (ADR-011)
  - harden_process(): an honest report of what could actually be applied on THIS platform.

Best effort, cross-platform. The stronger controls (egress-less sandbox, global no-swap, hardware
attestation) belong to the OS and the hardware, and are described in the security document.
"""
from __future__ import annotations

import builtins
import ctypes
import ctypes.util
import hashlib
import os
import platform
import socket
import sys
from pathlib import Path


# --------------------------- locked memory ------------------------------------
def _lock_addr(addr: int, length: int) -> bool:
    try:
        if sys.platform.startswith("win"):
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            return bool(k32.VirtualLock(ctypes.c_void_p(addr), ctypes.c_size_t(length)))
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        return libc.mlock(ctypes.c_void_p(addr), ctypes.c_size_t(length)) == 0
    except Exception:
        return False


def _unlock_addr(addr: int, length: int) -> None:
    try:
        if sys.platform.startswith("win"):
            ctypes.windll.kernel32.VirtualUnlock(ctypes.c_void_p(addr), ctypes.c_size_t(length))  # type: ignore[attr-defined]
        else:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.munlock(ctypes.c_void_p(addr), ctypes.c_size_t(length))
    except Exception:
        pass


class SecureBytes:
    """Mutable buffer on locked pages (best effort), zeroed on close.

    Usage:
        with SecureBytes.copy_from(secret_bytes) as sb:
            use(sb.view())     # bytes
        # here: zeroed and unlocked
    """

    def __init__(self, size: int):
        self.buf = bytearray(size)
        self._addr = ctypes.addressof((ctypes.c_char * size).from_buffer(self.buf)) if size else 0
        self.locked = _lock_addr(self._addr, size) if size else False

    @classmethod
    def copy_from(cls, data: bytes) -> "SecureBytes":
        sb = cls(len(data))
        sb.buf[:] = data
        return sb

    def view(self) -> bytes:
        return bytes(self.buf)

    def view_into(self) -> memoryview:
        """ZERO-COPY view (memoryview) on the locked buffer: prefer it over view() to avoid
        materialising an immutable `bytes` copy that the GC may leave lying around. The caller must NOT
        keep the memoryview beyond the `with` block."""
        return memoryview(self.buf)

    def zero(self) -> None:
        # ctypes.memset writes in C on the real region (a Python loop may be optimised or COW-moved).
        n = len(self.buf)
        if n and self._addr:
            try:
                ctypes.memset(self._addr, 0, n)
                return
            except Exception:
                pass
        for i in range(len(self.buf)):
            self.buf[i] = 0

    def close(self) -> None:
        n = len(self.buf)
        self.zero()
        if self.locked and n:
            _unlock_addr(self._addr, n)
            self.locked = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------- core dumps ---------------------------------------
def disable_core_dumps() -> bool:
    """POSIX: RLIMIT_CORE = 0. Windows: not applicable here (WER is a system-level setting)."""
    try:
        import resource  # POSIX only
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return True
    except Exception:
        return False


# --------------------------- best-effort wipe of immutables -------------------
def best_effort_wipe(data) -> bool:
    """Overwrite IN PLACE the content of a cleartext `bytes`/`bytearray` through ctypes.memset.

    Purpose: after a job, the cleartext prompt and response must not survive in RAM until the next GC
    (and then swap). `bytearray` is mutable, so it is overwritten cleanly. For an IMMUTABLE `bytes` the
    underlying C buffer (PyBytesObject.ob_sval) is written anyway, which erases the single copy that
    object points to.

    HONEST LIMIT, not avoidable in pure Python: CPython may already have COPIED that `bytes`
    (decode/encode, allocator slack), the original object is not always the sole holder, and small
    strings may be interned. This REDUCES the exposure window; it is not a guarantee of full erasure —
    only mlock plus a native buffer from the start (see SecureBytes) comes close. NEVER wipe a shared
    object (risk of corruption): reserve this for transient buffers you are the only holder of."""
    if data is None:
        return False
    n = len(data)
    if n == 0:
        return True
    try:
        if isinstance(data, bytearray):
            addr = ctypes.addressof((ctypes.c_char * n).from_buffer(data))
            ctypes.memset(addr, 0, n)
            return True
        if isinstance(data, bytes):
            # internal C buffer of an immutable bytes: standard offset of ob_sval in PyBytesObject
            # (ob_refcnt + ob_type + ob_size + ob_shash). Best effort; any error aborts silently —
            # never fatal, never corrupting another object.
            buf = (ctypes.c_char * n).from_buffer_copy(data)  # size validation
            del buf
            addr = id(data) + (ctypes.sizeof(ctypes.c_size_t) * 4)
            ctypes.memset(addr, 0, n)
            return True
    except Exception:
        return False
    return False


def lock_process_memory() -> bool:
    """mlockall(MCL_CURRENT|MCL_FUTURE): locks the process's ENTIRE RAM against swap, so cleartext
    living outside SecureBytes (KV cache, activations) never reaches the disk. Best effort: fails
    without CAP_IPC_LOCK or a sufficient RLIMIT_MEMLOCK, and is not applicable on Windows. Expensive on
    an ML process -> enable it deliberately (see confine.lock_all_memory / DENDRA_MLOCKALL)."""
    if sys.platform.startswith("win"):
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        return libc.mlockall(1 | 2) == 0   # MCL_CURRENT | MCL_FUTURE
    except Exception:
        return False


# --------------------------- software attestation -----------------------------
def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent


def build_manifest(pkg_dir: Path | None = None) -> dict[str, str]:
    """SHA-256 hash of every .py in the package -> the manifest (the "sealed binary")."""
    pkg = pkg_dir or _pkg_dir()
    out = {}
    for p in sorted(pkg.glob("*.py")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def self_attest(expected: dict[str, str], pkg_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Compare the running code to the expected manifest. Returns (ok, diverging_files).

    This is a LOCAL SANITY CHECK, NOT a binding attestation. A process that lies about its code also
    lies about its hash (or patches memory AFTER the attestation: TOCTOU), and `expected` is supplied
    as an argument, so comparing it to itself is tautological. There is no hardware root of trust. The
    real guarantee is a REMOTE attestation (TEE/TPM quote, reproducible build signed outside the
    enclave) bound to the on-chain hash — not implemented."""
    current = build_manifest(pkg_dir)
    mismatches = []
    for name, h in expected.items():
        if current.get(name) != h:
            mismatches.append(name)
    for name in current:
        if name not in expected:
            mismatches.append(name + " (not registered)")
    return (len(mismatches) == 0, mismatches)


# --------------------------- report -------------------------------------------
def harden_process() -> dict:
    """Apply what is possible and return an honest report."""
    rep = {
        "platform": platform.system(),
        "core_dumps_disabled": disable_core_dumps(),
    }
    # quick memory-locking probe
    sb = SecureBytes(64)
    rep["memory_locking"] = sb.locked
    sb.close()
    # immutable wipe (self-test on a throwaway buffer -> touches no shared object)
    rep["immutable_wipe"] = best_effort_wipe(bytearray(b"\xff" * 32))
    rep["notes"] = ("egress sandbox, global no-swap and hardware attestation belong to the OS and the "
                    "hardware; what is applied here is application-level best effort.")
    return rep


# =============================================================================
#  ANTI-LEAK GUARDS (application-level defence in depth)
#  During confidential inference, cleartext must leak neither over the NETWORK
#  (exfiltration) nor to DISK (log/dump). These guards intercept the attempts at
#  the Python level. In PRODUCTION they are DOUBLED by OS confinement (routeless
#  network namespace, seccomp, read-only filesystem).
# =============================================================================

class EgressBlocked(RuntimeError):
    """Raised when a NON-local network connection is attempted under `no_egress`."""


class DiskWriteBlocked(RuntimeError):
    """Raised when an unauthorised disk write is attempted under `no_disk_writes`."""


def _is_loopback(host) -> bool:
    if not isinstance(host, str):
        return False
    return (host in ("localhost", "::1", "::ffff:127.0.0.1")
            or host.startswith("127."))


def _inference_allowlist():
    """Resolved IPs of the declared inference engines (OLLAMA_ENDPOINT / DENDRA_JUDGE_ENDPOINT).

    WHY THIS IS NOT A HOLE IN THE ANTI-EXFILTRATION GUARD. The guard forbids any non-local egress while
    cleartext is being processed. Assuming "Ollama runs on loopback" is true for a manual install and
    FALSE in the official Docker kit, where Ollama is a neighbouring container
    (http://ollama:11434 -> a bridge-network IP). The result is `EgressBlocked` on EVERY job, so no
    Docker miner can infer at all: the guard protects nothing more, it only prevents honest work.
    Only the hosts explicitly configured as inference engines are allowed, never a network range: a
    malicious backend still cannot POST the cleartext anywhere else.
    """
    hosts = set()
    for var in ("OLLAMA_ENDPOINT", "DENDRA_JUDGE_ENDPOINT", "DENDRA_EGRESS_ALLOW"):
        val = os.environ.get(var, "")
        for item in val.split(","):
            item = item.strip()
            if not item:
                continue
            h = item.split("//")[-1].split("/")[0].split(":")[0]
            if not h:
                continue
            hosts.add(h)
            try:
                for ai in socket.getaddrinfo(h, None):
                    hosts.add(ai[4][0])
            except Exception:
                pass
    return hosts


def _dest_host(address):
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


class no_egress:
    """Blocks every NON-loopback outbound connection (anti-exfiltration) inside the `with` block.

    Loopback (127.0.0.1/::1) stays allowed, so inference through a local Ollama works, while a
    malicious backend cannot POST the cleartext outside. Best effort at the Python level (a native
    binary bypasses it); in production: a network namespace with no default route.
    """

    def __init__(self, allow_loopback: bool = True):
        self.allow_loopback = allow_loopback
        self._connect = None
        self._connect_ex = None
        # DECLARED inference engines (see _inference_allowlist): in a container, Ollama is not on
        # loopback. Resolved on entering the block, not here, to follow a Docker IP change.
        self._allow_hosts = set()

    def _check(self, sock, address):
        if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", -1):
            return  # local IPC, no network involved
        host = _dest_host(address)
        if self.allow_loopback and _is_loopback(host):
            return
        if host in self._allow_hosts:
            return
        # A Docker IP can change between restarts: resolve ONCE more before refusing, otherwise a plain
        # `docker compose up` would reintroduce a total block on inference.
        self._allow_hosts |= _inference_allowlist()
        if host in self._allow_hosts:
            return
        raise EgressBlocked(f"egress blocked to {host!r} (Mode A anti-exfiltration; "
                            f"allowed engines: {sorted(self._allow_hosts) or 'none'})")

    def __enter__(self):
        # Resolved ON ENTERING the block: container IPs change from one `up` to the next.
        self._allow_hosts = _inference_allowlist()
        self._connect = socket.socket.connect
        self._connect_ex = socket.socket.connect_ex
        guard = self

        def connect(sock, address, *a, **k):
            guard._check(sock, address)
            return guard._connect(sock, address, *a, **k)

        def connect_ex(sock, address, *a, **k):
            guard._check(sock, address)
            return guard._connect_ex(sock, address, *a, **k)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._connect
        socket.socket.connect_ex = self._connect_ex


class no_disk_writes:
    """Blocks opening files for WRITING inside the `with` block, so cleartext does not leak through a
    log or a file. Reading stays allowed. `allow` lists the tolerated paths. Best effort (it intercepts
    `open`); in production: a read-only filesystem with no writable /tmp inside the sandbox."""

    def __init__(self, allow=()):
        self.allow = {str(p) for p in allow} | {"/dev/null", os.devnull}
        self._open = None

    def __enter__(self):
        self._open = builtins.open
        guard = self

        def guarded_open(file, mode="r", *a, **k):
            m = mode if isinstance(mode, str) else "r"
            if any(c in m for c in "wax+") and str(file) not in guard.allow:
                raise DiskWriteBlocked(f"disk write blocked: {file!r} (mode {m!r})")
            return guard._open(file, mode, *a, **k)

        builtins.open = guarded_open
        return self

    def __exit__(self, *exc):
        builtins.open = self._open


class ConfidentialGuard:
    """Confidential-inference envelope: no non-local egress, no disk write, core dumps disabled. To be
    combined with SecureBytes (cleartext in locked, zeroed memory)."""

    def __init__(self, allow_loopback: bool = True, allow_paths=()):
        self._ne = no_egress(allow_loopback)
        self._nd = no_disk_writes(allow_paths)

    def __enter__(self):
        disable_core_dumps()
        self._ne.__enter__()
        self._nd.__enter__()
        return self

    def __exit__(self, *exc):
        self._nd.__exit__(*exc)
        self._ne.__exit__(*exc)


def make_canary(tag: str = "DENDRA") -> str:
    """Unique canary token: if it appears outside the enclave (network, disk, log), there is a leak."""
    return f"CANARY-{tag}-{os.urandom(8).hex()}"
