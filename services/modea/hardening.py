"""Durcissement du client mineur (controles de MODE-A-SECURITE, partie applicative).

Fournit :
  - SecureBytes      : tampon a pages VERROUILLEES (mlock/VirtualLock) + zeroisation a la sortie.
  - disable_core_dumps() : empeche un crash de dumper la memoire (clair) sur disque.
  - build_manifest()/self_attest() : ATTESTATION LOGICIELLE — le code qui tourne correspond-il
    au binaire scelle/enregistre on-chain ? (ADR-011)
  - harden_report()  : ce qui a pu etre applique sur CETTE plateforme (honnete).

Best-effort multiplateforme. Les controles plus forts (sandbox sans egress, no-swap global,
attestation materielle) restent decrits dans MODE-A-SECURITE et relevent de l'OS/hardware.
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


# --------------------------- memoire verrouillee ------------------------------
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
    """Tampon mutable a pages verrouillees (best-effort), zeroise a la fermeture.

    Usage :
        with SecureBytes.copy_from(secret_bytes) as sb:
            use(sb.view())     # bytes
        # ici : zeroise + deverrouille
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
        """Vue ZERO-COPIE (memoryview) sur le tampon verrouille : a preferer a view() quand on veut
        eviter de materialiser une copie `bytes` immuable (que le GC pourra laisser trainer). Le lecteur
        ne doit PAS conserver la memoryview au-dela du bloc `with`."""
        return memoryview(self.buf)

    def zero(self) -> None:
        # ctypes.memset ecrit en C sur la zone reelle (un for-Python peut etre optimise/COW-deplace).
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
    """POSIX : RLIMIT_CORE = 0. Windows : non applicable ici (WER -> conf systeme)."""
    try:
        import resource  # POSIX uniquement
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return True
    except Exception:
        return False


# --------------------------- zeroisation best-effort des immuables ------------
def best_effort_wipe(data) -> bool:
    """Ecrase SUR PLACE le contenu d'un `bytes`/`bytearray` clair via ctypes.memset.

    But : apres un job, on ne veut pas que le PROMPT/REPONSE en clair survive en RAM jusqu'au prochain
    GC (puis swap). `bytearray` est mutable -> on ecrase proprement. Pour un `bytes` IMMUABLE on ecrit
    quand meme dans son buffer C sous-jacent (PyBytesObject.ob_sval), ce qui efface l'unique copie
    pointee par cet objet.

    LIMITE HONNETE (documentee, non contournable en pur Python) : CPython peut avoir deja COPIE ce
    `bytes` (decode/encode/slack d'allocateur), et l'objet d'origine n'est pas toujours l'unique
    detenteur ; les petites chaines peuvent etre internees. C'est de la REDUCTION de fenetre
    d'exposition, pas une garantie d'effacement total (seul mlock + un buffer natif des le debut, cf.
    SecureBytes, s'en approche). N'ecrase JAMAIS un objet partage (risque de corruption) : reserve aux
    tampons transitoires dont on est le seul detenteur."""
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
            # buffer C interne d'un bytes immuable : offset standard de ob_sval dans PyBytesObject
            # (ob_refcnt + ob_type + ob_size + ob_shash). Best-effort ; toute erreur -> on abandonne
            # silencieusement (jamais fatal, jamais de corruption d'un autre objet).
            buf = (ctypes.c_char * n).from_buffer_copy(data)  # validation de taille
            del buf
            addr = id(data) + (ctypes.sizeof(ctypes.c_size_t) * 4)
            ctypes.memset(addr, 0, n)
            return True
    except Exception:
        return False
    return False


def lock_process_memory() -> bool:
    """mlockall(MCL_CURRENT|MCL_FUTURE) : verrouille TOUTE la RAM du processus contre le swap
    (le clair hors SecureBytes -- KV-cache, activations -- ne part pas sur disque). Best-effort :
    echoue sans CAP_IPC_LOCK / RLIMIT_MEMLOCK suffisant, et non applicable sous Windows. Couteux sur
    un process ML -> a activer sciemment (cf. confine.lock_all_memory / DENDRA_MLOCKALL)."""
    if sys.platform.startswith("win"):
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        return libc.mlockall(1 | 2) == 0   # MCL_CURRENT | MCL_FUTURE
    except Exception:
        return False


# --------------------------- attestation logicielle ---------------------------
def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent


def build_manifest(pkg_dir: Path | None = None) -> dict[str, str]:
    """Hash SHA-256 de chaque .py du paquet -> manifeste (le 'binaire scelle')."""
    pkg = pkg_dir or _pkg_dir()
    out = {}
    for p in sorted(pkg.glob("*.py")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def self_attest(expected: dict[str, str], pkg_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Compare le code courant au manifeste attendu. Renvoie (ok, fichiers_divergents).

    ⚠️ AUDIT CR-05 : SANITY-CHECK LOCAL, PAS une attestation contraignante. Un processus qui ment sur
    son code ment aussi sur son hash (ou patche en memoire APRES l'attestation : TOCTOU), et `expected`
    est fourni en argument (le comparer a lui-meme est tautologique). Aucune racine de confiance
    materielle. La vraie garantie = attestation DISTANTE (TEE/TPM quote, build reproductible signe hors
    enclave) liee au hash on-chain -- non implementee."""
    current = build_manifest(pkg_dir)
    mismatches = []
    for name, h in expected.items():
        if current.get(name) != h:
            mismatches.append(name)
    for name in current:
        if name not in expected:
            mismatches.append(name + " (non enregistre)")
    return (len(mismatches) == 0, mismatches)


# --------------------------- rapport ------------------------------------------
def harden_process() -> dict:
    """Applique ce qui est possible et renvoie un rapport honnete."""
    rep = {
        "platform": platform.system(),
        "core_dumps_disabled": disable_core_dumps(),
    }
    # test rapide du verrouillage memoire
    sb = SecureBytes(64)
    rep["memory_locking"] = sb.locked
    sb.close()
    # zeroisation immuable (auto-test sur un buffer jetable -> ne touche aucun objet partage)
    rep["immutable_wipe"] = best_effort_wipe(bytearray(b"\xff" * 32))
    rep["notes"] = ("egress-sandbox + no-swap global + attestation materielle = OS/hardware "
                    "(cf. MODE-A-SECURITE) ; ici : best-effort applicatif.")
    return rep


# =============================================================================
#  C4 - GARDES ANTI-FUITE (defense applicative en profondeur)
#  Pendant l'inference confidentielle, le clair ne doit fuir ni par le RESEAU
#  (exfiltration) ni par le DISQUE (log/dump). Ces gardes interceptent les
#  tentatives au niveau Python. En PRODUCTION elles se DOUBLENT d'un confinement
#  OS (namespace reseau sans route, seccomp, FS read-only) -- cf. MODE-A-SECURITE.
# =============================================================================

class EgressBlocked(RuntimeError):
    """Levee quand une connexion reseau NON-locale est tentee sous `no_egress`."""


class DiskWriteBlocked(RuntimeError):
    """Levee quand une ecriture disque non autorisee est tentee sous `no_disk_writes`."""


def _is_loopback(host) -> bool:
    if not isinstance(host, str):
        return False
    return (host in ("localhost", "::1", "::ffff:127.0.0.1")
            or host.startswith("127."))


def _dest_host(address):
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


class no_egress:
    """Bloque toute connexion sortante NON-loopback (anti-exfiltration) dans le bloc `with`.

    La boucle locale (127.0.0.1/::1) reste autorisee -> l'inference via Ollama local fonctionne,
    mais un backend malveillant ne peut pas POSTer le clair vers l'exterieur. Best-effort Python
    (un binaire natif contourne) ; en prod : namespace reseau sans route par defaut.
    """

    def __init__(self, allow_loopback: bool = True):
        self.allow_loopback = allow_loopback
        self._connect = None
        self._connect_ex = None

    def _check(self, sock, address):
        if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", -1):
            return  # IPC local (ex: pas de reseau)
        host = _dest_host(address)
        if self.allow_loopback and _is_loopback(host):
            return
        raise EgressBlocked(f"egress bloque vers {host!r} (anti-exfiltration Mode A)")

    def __enter__(self):
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
    """Bloque l'ouverture de fichiers en ECRITURE dans le bloc `with` (le clair ne fuit pas
    par log/fichier). Lecture autorisee. `allow` liste les chemins tolere(s). Best-effort
    (intercepte `open`) ; en prod : FS read-only / pas de /tmp inscriptible dans le sandbox."""

    def __init__(self, allow=()):
        self.allow = {str(p) for p in allow} | {"/dev/null", os.devnull}
        self._open = None

    def __enter__(self):
        self._open = builtins.open
        guard = self

        def guarded_open(file, mode="r", *a, **k):
            m = mode if isinstance(mode, str) else "r"
            if any(c in m for c in "wax+") and str(file) not in guard.allow:
                raise DiskWriteBlocked(f"ecriture disque bloquee: {file!r} (mode {m!r})")
            return guard._open(file, mode, *a, **k)

        builtins.open = guarded_open
        return self

    def __exit__(self, *exc):
        builtins.open = self._open


class ConfidentialGuard:
    """Enveloppe d'inference confidentielle : pas d'egress non-local + pas d'ecriture disque +
    core dumps desactives. A combiner avec SecureBytes (clair en memoire verrouillee/zeroisee)."""

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
    """Jeton-piege unique : s'il apparait hors de l'enclave (reseau/disque/log), il y a fuite."""
    return f"CANARY-{tag}-{os.urandom(8).hex()}"
