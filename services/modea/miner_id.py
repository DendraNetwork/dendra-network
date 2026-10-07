"""Derivation of the canonical miner identifier, client side.

WHY THIS FILE EXISTS. Since the third consensus epoch the chain DERIVES a miner's identifier from the
signer's address and refuses any other value (x/jobs/keeper/msg_server_miner.go, CreateMiner). No code
in this kit derived it: `deploy/join.sh` invented `m-<sha10>` for a fresh operator, `create-miner` was
refused, and the node ran outside the registry — serving nothing, earning nothing, and counted nowhere.
The failure was visible (the daemon says the registration failed) but its CAUSE was not: the operator
was told "this miner will receive no job" with no way to learn which identifier the chain expected.

⚠️ THIS IS A SECOND IMPLEMENTATION OF A CONSENSUS RULE, and that is a liability, not a convenience.
The authority is `chain/x/jobs/types/miner_id.go`; this file must track it exactly. Three properties
are load-bearing and none of them may drift:
  · the domain string is FIXED-LENGTH and versioned — changing the derivation must change `/v1`, so
    that old and new identifiers cannot be silently equal for some inputs;
  · the hash covers the RAW account bytes, never the bech32 string — hashing the string would make the
    identifier depend on the account prefix, so the same key would derive differently on a chain whose
    prefix changed;
  · the human-readable part is `dm`, deliberately NOT the account prefix, so an identifier pasted where
    an address is expected fails at the decoder instead of far away as "miner not found".
`tests/test_miner_id.py` pins the implementation against a pair produced by the live chain, so a drift
between this file and the keeper surfaces as a red test rather than as a refused registration.
"""

import hashlib

from .cosmos_addr import _convertbits, bech32_decode, bech32_encode

# Kept byte-identical to chain/x/jobs/types/miner_id.go. Do not "tidy" these three constants.
MINER_ID_HRP = "dm"
MINER_ID_DOMAIN = "dendra/miner-id/v1"
MINER_ID_PAYLOAD_LEN = 10


def derive_miner_id(addr_bytes: bytes) -> str:
    """Canonical identifier for RAW account bytes. Pure function: no clock, no chain state."""
    if not addr_bytes:
        # An empty address is not an address: deriving from it yields a well-formed identifier for
        # nobody, which passes every downstream check while binding to no key. Fail closed.
        raise ValueError("cannot derive a miner id from an empty address")
    digest = hashlib.sha256(MINER_ID_DOMAIN.encode() + addr_bytes).digest()
    data5 = _convertbits(digest[:MINER_ID_PAYLOAD_LEN], 8, 5, True)
    if data5 is None:
        raise ValueError("bit conversion failed")
    return bech32_encode(MINER_ID_HRP, data5)


def miner_id_for_account(address: str) -> str:
    """Identifier for a bech32 ACCOUNT address (dendra1...), decoded first.

    ⚠️ A decode failure is an ERROR, never a fallback. Returning the input unchanged — the tempting
    'be liberal in what you accept' — would hand `create-miner` a value the chain refuses, and the
    operator would read the refusal as a chain problem rather than a malformed address."""
    dec = bech32_decode(address)
    if dec is None:
        raise ValueError(f"address {address!r} is not decodable bech32")
    _hrp, data5 = dec
    raw = _convertbits(data5, 5, 8, False)
    if raw is None:
        raise ValueError(f"address {address!r} carries no decodable payload")
    return derive_miner_id(bytes(raw))
