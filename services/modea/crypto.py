"""Couche cryptographique du Mode A.

- Cles d'identite et cles EPHEMERES X25519 (une session = une cle ephemere -> forward secrecy).
- Accord de cle ECDH (X25519) + derivation HKDF-SHA256.
- Chiffrement authentifie AES-256-GCM (AEAD).
- Zeroisation best-effort des secrets en memoire.

Dependance : `cryptography` (pip install cryptography).
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
    """Genere une paire X25519. Renvoie (cle_privee, cle_publique_bytes 32o)."""
    sk = X25519PrivateKey.generate()
    pk = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return sk, pk


def derive_session_key(sk: X25519PrivateKey, peer_pk: bytes, *, info: bytes) -> bytes:
    """ECDH X25519 + HKDF-SHA256 -> cle de session 32 octets."""
    shared = sk.exchange(X25519PublicKey.from_public_bytes(peer_pk))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared)
    # zeroisation best-effort du secret partage brut
    zeroize(bytearray(shared))
    return key


@dataclass
class Sealed:
    nonce: bytes
    ct: bytes  # ciphertext + tag GCM


def encrypt(key: bytes, plaintext: bytes, *, aad: bytes = b"") -> Sealed:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return Sealed(nonce=nonce, ct=ct)


def decrypt(key: bytes, sealed: Sealed, *, aad: bytes = b"") -> bytes:
    return AESGCM(key).decrypt(sealed.nonce, sealed.ct, aad)


def zeroize(buf: bytearray) -> None:
    """Ecrase un buffer mutable (best-effort en Python). A completer par mlock/VirtualLock
    + buffers natifs dans le client mineur de production (cf. MODE-A-SECURITE)."""
    for i in range(len(buf)):
        buf[i] = 0


# --------------------------- (de)serialisation de cles ------------------------
def pub_bytes(sk: X25519PrivateKey) -> bytes:
    """Cle publique brute (32 o) d'une cle privee X25519."""
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def sk_to_bytes(sk: X25519PrivateKey) -> bytes:
    """Serialise une cle privee X25519 en 32 octets bruts (keystore LOCAL du mineur)."""
    return sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def sk_from_bytes(b: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(b)


# --------------------------- keystore mineur CHIFFRE au repos (audit PY-09) ----
# Sans ca, la cle X25519 du mineur est 32 octets EN CLAIR sur disque : une compromission
# disque = dechiffrement de TOUS les prompts adresses au mineur + usurpation. On chiffre au
# repos avec une passphrase (scrypt -> AES-256-GCM). Permissions 0600 dans tous les cas.
_SK_MAGIC = b"DENDRA-SKENC1\n"      # entete d'un fichier de cle CHIFFRE (sinon = ancien format clair)
_SCRYPT = dict(length=32, n=2 ** 15, r=8, p=1)


def save_sk(sk: X25519PrivateKey, path: str, passphrase: str = "") -> None:
    """Ecrit la cle privee mineur. CHIFFREE si `passphrase` non vide ; sinon claire (avec 0600).
    Toujours via open(O_CREAT,0600) pour ne jamais laisser un instant la cle en 0644."""
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
    """Charge une cle mineur (format chiffre OU ancien format clair, pour migration)."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(_SK_MAGIC):
        if not passphrase:
            raise RuntimeError("cle mineur chiffree : DENDRA_MINER_PASSPHRASE requis pour la dechiffrer")
        body = data[len(_SK_MAGIC):]
        salt, nonce, ct = body[:16], body[16:28], body[28:]
        key = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
        try:
            raw = AESGCM(key).decrypt(nonce, ct, b"dendra-sk")   # leve si passphrase fausse
        finally:
            zeroize(bytearray(key))
        return sk_from_bytes(raw)
    return sk_from_bytes(data)          # ancien format clair (sera re-chiffre au prochain save_sk)
