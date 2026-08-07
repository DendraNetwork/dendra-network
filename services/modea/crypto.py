"""Cryptographic layer of Mode A.

- Identity keys and EPHEMERAL X25519 keys (one session, one ephemeral key -> forward secrecy).
- ECDH key agreement (X25519) plus HKDF-SHA256 derivation.
- Authenticated encryption with AES-256-GCM (AEAD).
- Best-effort zeroization of in-memory secrets.

Dependency: `cryptography` (pip install cryptography).
"""
from __future__ import annotations

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


def sk_from_bytes(b: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(b)


# --------------------------- miner keystore ENCRYPTED at rest ------------------
# Without this, the miner's X25519 key sits on disk as 32 PLAINTEXT bytes: a disk compromise decrypts
# EVERY prompt addressed to that miner and allows impersonation. The key is encrypted at rest with a
# passphrase (scrypt -> AES-256-GCM). Permissions are 0600 in every case.
_SK_MAGIC = b"DENDRA-SKENC1\n"      # header of an ENCRYPTED key file (otherwise: legacy plaintext)
_SCRYPT = dict(length=32, n=2 ** 15, r=8, p=1)


def save_sk(sk: X25519PrivateKey, path: str, passphrase: str = "") -> None:
    """Writes the miner private key. ENCRYPTED when `passphrase` is non-empty, plaintext otherwise
    (still 0600). Always through open(O_CREAT, 0600), so the key is never momentarily 0644."""
    raw = sk_to_bytes(sk)
    try:
        if passphrase:
            salt = os.urandom(16)
            key = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
            nonce = os.urandom(12)
            ct = AESGCM(key).encrypt(nonce, raw, b"dendra-sk")
            zeroize(bytearray(key))
            blob = _SK_MAGIC + salt + nonce + ct
        else:
            blob = raw
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        zeroize(bytearray(raw))


def load_sk(path: str, passphrase: str = "") -> X25519PrivateKey:
    """Loads a miner key (encrypted format OR the legacy plaintext format, for migration)."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(_SK_MAGIC):
        if not passphrase:
            raise RuntimeError("miner key is encrypted: DENDRA_MINER_PASSPHRASE is required to decrypt it")
        body = data[len(_SK_MAGIC):]
        salt, nonce, ct = body[:16], body[16:28], body[28:]
        key = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
        try:
            raw = AESGCM(key).decrypt(nonce, ct, b"dendra-sk")   # raises on a wrong passphrase
        finally:
            zeroize(bytearray(key))
        return sk_from_bytes(raw)
    return sk_from_bytes(data)          # legacy plaintext format (re-encrypted on the next save_sk)
