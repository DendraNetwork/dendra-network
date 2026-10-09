"""Cryptographic layer of Mode A.

- Identity keys and EPHEMERAL X25519 keys (one session, one ephemeral key -> forward secrecy).
- ECDH key agreement (X25519) plus HKDF-SHA256 derivation.
- Authenticated encryption with AES-256-GCM (AEAD).
- Best-effort zeroization of in-memory secrets.

Dependency: `cryptography` (pip install cryptography).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)


def gen_keypair() -> tuple[X25519PrivateKey, bytes]:
    """Generates an X25519 key pair. Returns (private_key, public_key_bytes 32B)."""
    sk = X25519PrivateKey.generate()
    pk = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return sk, pk


def derive_session_key(sk: X25519PrivateKey, peer_pk: bytes, *, info: bytes) -> bytes:
    """ECDH X25519 plus HKDF-SHA256 -> a 32-byte session key."""
    shared = sk.exchange(X25519PublicKey.from_public_bytes(peer_pk))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared)
    # best-effort zeroization of the raw shared secret
    zeroize(bytearray(shared))
    return key


@dataclass
class Sealed:
    nonce: bytes
    ct: bytes  # ciphertext plus GCM tag


def encrypt(key: bytes, plaintext: bytes, *, aad: bytes = b"") -> Sealed:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return Sealed(nonce=nonce, ct=ct)


def decrypt(key: bytes, sealed: Sealed, *, aad: bytes = b"") -> bytes:
    return AESGCM(key).decrypt(sealed.nonce, sealed.ct, aad)


def zeroize(buf: bytearray) -> None:
    """Overwrites a mutable buffer (best effort in Python). To be completed with mlock/VirtualLock
    and native buffers in the production miner client (see MODE-A-SECURITE)."""
    for i in range(len(buf)):
        buf[i] = 0


# --------------------------- key (de)serialization ----------------------------
def pub_bytes(sk: X25519PrivateKey) -> bytes:
    """Raw public key (32 B) of an X25519 private key."""
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def sk_to_bytes(sk: X25519PrivateKey) -> bytes:
    """Serializes an X25519 private key to 32 raw bytes (the miner's LOCAL keystore)."""
    return sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


PROMPT_COMMIT_INFO = b"dendra-prompt-commit-v1"


def prompt_salt(sk, job_id: str) -> str:
    """Per-job salt for the prompt commitment: DERIVED, never stored, never shared.

    The commitment has to be verifiable by a juror and useless to anyone else. A bare
    `sha256(prompt)` anchored on a public chain would let any observer CONFIRM a guessed prompt --
    the confidentiality this protocol exists to provide. A random salt would have to survive between
    the process that anchors the commit and the one that reveals, becoming state that can be lost
    exactly when it is needed. Deriving it from the miner's long-lived reveal key gives both: the
    daemon and the reveal worker load the same key file, so they agree without exchanging anything,
    and nobody without that key can compute it.
    """
    return hashlib.sha256(sk_to_bytes(sk) + PROMPT_COMMIT_INFO + job_id.encode("utf-8")).hexdigest()


def prompt_commitment(salt_hex: str, prompt: str) -> str:
    """The value anchored in `prompt_commit`: binds the QUESTION to the job, checkable by a juror.

    Without it a juror grades an answer against a question supplied by the party being audited, which
    measures nothing: the audited party picks the question its answer fits.
    """
    return hashlib.sha256((salt_hex + "|" + (prompt or "")).encode("utf-8")).hexdigest()


def sk_from_bytes(b: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(b)


# --------------------------- miner key files ENCRYPTED at rest ------------------
# Without this, the miner's key files sit on disk in clear: the X25519 key (a disk compromise decrypts
# EVERY prompt addressed to that miner), the VRF secret, the attestation key and the recovery phrase of
# its Cosmos key. Each is written in ONE envelope under the keyring's passphrase (scrypt -> AES-256-GCM),
# bound to its kind by the AEAD's associated data, so one file cannot be passed off as another.
#   MAGIC | salt (16) | nonce (12) | ciphertext + tag
# A file that does not start with MAGIC is the legacy clear format: it is still read, and re-encrypted in
# place (atomically) as soon as a passphrase is there to seal it with (`load_secret(..., seal_with=)`).
# Every write goes through `write_private`: created 0600 by os.open, never momentarily readable under the
# process umask, and swapped in by os.replace, so a reader sees the old file or the new one, never half.
MAGIC = b"DENDRA-SKENC1\n"          # header of an ENCRYPTED key file (otherwise: legacy clear format)
_SK_MAGIC = MAGIC                   # the X25519 key file has carried this header since it was first sealed
_SCRYPT = dict(length=32, n=2 ** 15, r=8, p=1)
AAD_SK = b"dendra-sk"
AAD_VRF = b"dendra-vrf"
AAD_ATTEST = b"dendra-attestkey"
AAD_RECOVERY = b"dendra-recovery"


class KeyEnvelopeError(RuntimeError):
    """A key file that cannot be opened. Never a reason to create a new key in its place."""


class EnvelopeLocked(KeyEnvelopeError):
    """The file is encrypted and no passphrase was given."""


class EnvelopeWrongPassphrase(KeyEnvelopeError):
    """The passphrase does not open the file (or the file was altered)."""


def is_sealed(data: bytes) -> bool:
    return data.startswith(MAGIC)


def file_is_sealed(path: str) -> bool:
    with open(path, "rb") as f:
        return is_sealed(f.read(len(MAGIC)))


def seal(raw: bytes, passphrase: str, aad: bytes) -> bytes:
    salt = os.urandom(16)
    key = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
    nonce = os.urandom(12)
    try:
        ct = AESGCM(key).encrypt(nonce, raw, aad)
    finally:
        zeroize(bytearray(key))
    return MAGIC + salt + nonce + ct


def unseal(data: bytes, passphrase: str, aad: bytes, path: str = "") -> bytes:
    if not passphrase:
        raise EnvelopeLocked(f"{path or 'the key file'} is encrypted and no passphrase was given: the kit reads "
                             "it from /run/dendra-secrets/keyring-passphrase (DENDRA_SECRETS_DIR in "
                             "deploy/testnet-miner/.env)")
    body = data[len(MAGIC):]
    salt, nonce, ct = body[:16], body[16:28], body[28:]
    key = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except Exception as e:  # noqa: BLE001 -- InvalidTag: wrong passphrase, altered file, or another kind
        raise EnvelopeWrongPassphrase(f"{path or 'the key file'} does not open with the passphrase given "
                                      "(wrong passphrase, or the file was altered)") from e
    finally:
        zeroize(bytearray(key))


def write_private(path: str, data: bytes) -> None:
    """Atomic write of a private file: a temporary file in the same directory, created 0600 by os.open
    (O_EXCL: never an existing file, never a symlink), flushed to disk, then os.replace'd over `path`."""
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_envelope(path: str, passphrase: str, aad: bytes) -> tuple:
    """(clear bytes, was_sealed). Raises FileNotFoundError, EnvelopeLocked, EnvelopeWrongPassphrase."""
    with open(path, "rb") as f:
        data = f.read()
    if is_sealed(data):
        return unseal(data, passphrase, aad, path), True
    return data, False


def store_secret(path: str, raw: bytes, seal_with: str, aad: bytes) -> None:
    """Writes a key file: sealed under `seal_with` when it is non-empty, in clear otherwise; 0600 either way."""
    write_private(path, seal(raw, seal_with, aad) if seal_with else raw)


def load_secret(path: str, passphrase: str, aad: bytes, *, seal_with: str = "") -> bytes:
    """Reads a key file. A file found IN CLEAR while `seal_with` is given is re-encrypted in place,
    atomically, and read back before this returns: the clear file is replaced only by one that opens to
    the same bytes."""
    raw, sealed = read_envelope(path, passphrase, aad)
    if not sealed and seal_with:
        write_private(path, seal(raw, seal_with, aad))
        again, now_sealed = read_envelope(path, seal_with, aad)
        if not now_sealed or again != raw:
            raise KeyEnvelopeError(f"{path} was re-encrypted and does not read back the same")
    return raw


def save_sk(sk: X25519PrivateKey, path: str, passphrase: str = "") -> None:
    """Writes the miner private key: sealed when `passphrase` is non-empty, in clear otherwise (0600)."""
    raw = sk_to_bytes(sk)
    try:
        store_secret(path, raw, passphrase, AAD_SK)
    finally:
        zeroize(bytearray(raw))


def load_sk(path: str, passphrase: str = "", *, seal_with: str = "") -> X25519PrivateKey:
    """Loads a miner key, sealed or in the legacy clear format. With `seal_with`, a key found in clear is
    re-encrypted in place (only the daemon passes it: one writer per file)."""
    return sk_from_bytes(load_secret(path, passphrase, AAD_SK, seal_with=seal_with))
