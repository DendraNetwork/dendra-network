"""Derive a Cosmos address (`dendra1...`) from a public key, in pure Python.

WHY THIS MODULE EXISTS. The relay must be able to answer ONE question: "is the writer the operator of
the miner it claims to be?". The registry gives it `miner.Operator` (a `dendra1...` address); the writer
presents a public key and a signature. The missing link is the bridge from public key to address.

That bridge is written here DELIBERATELY rather than imported from a Cosmos stack:
  · the relay container already ships `cryptography` and `ripemd160`, so the three primitives needed are
    present and a Cosmos stack would be a heavy dependency added for ~60 lines of bech32;
  · the relay has NO access to the chain, and this computation is purely LOCAL — it queries no account.

WHAT THIS MODULE DOES NOT DO. It verifies no signature and authorises nothing. It translates a key into
an address, and that is all. Turning that into a decision ("this write is legitimate") is the caller's
job, and the caller must ALSO verify the signature: a matching address only proves knowledge of a PUBLIC
key, which is to say nothing at all.

SECP256K1 is the observed case, not an assumption. Miner operator accounts on the live chain publish a
`cosmos.crypto.secp256k1.PubKey`, so the derivation is
`bech32(hrp, ripemd160(sha256(compressed_pubkey)))`. For ed25519 the rule would be `sha256(pubkey)[:20]`;
both are implemented, but only secp256k1 has been observed in use.
"""
from __future__ import annotations

import hashlib

# ── bech32 (BIP-173) ────────────────────────────────────────────────────────────────────────────
# The reference implementation, rewritten here to avoid a dependency. The polymod and the character
# table are normative: they are not design choices and must not be "simplified".
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    """Regroup bits. Returns `None` on malformed input — never an approximate result."""
    acc, bits, ret = 0, 0, []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for b in data:
        if b < 0 or (b >> frombits):
            return None
        acc = ((acc << frombits) | b) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def bech32_encode(hrp: str, data_5bit) -> str:
    combined = list(data_5bit)
    chk = _polymod(_hrp_expand(hrp) + combined + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(chk >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in combined + checksum)


def bech32_decode(addr: str):
    """Return (hrp, 5-bit data) or `None`. Rejects anything not STRICTLY conformant: mixed case, a
    character outside the alphabet, a wrong checksum. Nothing is "repaired"."""
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return None
    if addr.lower() != addr and addr.upper() != addr:
        return None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return None
    hrp, data = addr[:pos], addr[pos + 1:]
    if any(c not in _CHARSET for c in data):
        return None
    dec = [_CHARSET.find(c) for c in data]
    if _polymod(_hrp_expand(hrp) + dec) != 1:
        return None
    return hrp, dec[:-6]


# ── Cosmos address ──────────────────────────────────────────────────────────────────────────────
def address_from_pubkey(pubkey: bytes, hrp: str = "dendra", algo: str = "secp256k1") -> str:
    """Return `dendra1...` for a raw public key. Raises ValueError on non-conformant input.

    secp256k1: a 33-byte COMPRESSED key (0x02/0x03 prefix) -> ripemd160(sha256(pk))
    ed25519  : a 32-byte key                               -> sha256(pk)[:20]
    """
    if algo == "secp256k1":
        if len(pubkey) != 33 or pubkey[0] not in (2, 3):
            # `.hex()` assumed bytes: on a str -- the exact wrong input this branch exists to name --
            # the message itself raised, so the diagnostic died with the case it was written for.
            _p = pubkey[:1]
            _p = _p.hex() if isinstance(_p, (bytes, bytearray)) else repr(_p)
            raise ValueError(f"secp256k1: expected a 33-byte compressed key (0x02/0x03), "
                             f"got {len(pubkey)} {type(pubkey).__name__} with prefix {_p or 'none'}")
        h = hashlib.new("ripemd160", hashlib.sha256(pubkey).digest()).digest()
    elif algo == "ed25519":
        if len(pubkey) != 32:
            raise ValueError(f"ed25519: expected a 32-byte key, got {len(pubkey)}")
        h = hashlib.sha256(pubkey).digest()[:20]
    else:
        raise ValueError(f"unknown algorithm: {algo!r} — a key scheme is never guessed")
    five = _convertbits(h, 8, 5)
    if five is None:
        raise ValueError("bit conversion failed")
    return bech32_encode(hrp, five)


def pubkey_matches_address(pubkey: bytes, address: str, algo: str = "secp256k1") -> bool:
    """Does this key yield EXACTLY this address? Any anomaly answers False.

    The HRP is taken from the address supplied, never assumed: comparing a `dendra1...` address against a
    derivation made under `cosmos1...` would fail for the prefix alone, and a refusal for the wrong
    reason is as misleading as an acceptance.
    """
    d = bech32_decode(address)
    if d is None:
        return False
    try:
        return address_from_pubkey(pubkey, hrp=d[0], algo=algo) == address.lower()
    except ValueError:
        return False
