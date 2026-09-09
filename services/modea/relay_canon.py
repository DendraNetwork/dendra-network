"""Canonical message of a signed relay write — the wire form, made executable.

═══ WHY THIS FILE COMES BEFORE ANY RELAY CODE ══════════════════════════════════════════════════
An ambiguous serialization is a **signature malleability**: two different writes that produce the
same signed message, hence a valid signature for a write nobody ever authorized. Such a hole looks
exactly like a guard. The form is therefore fixed FIRST, and proven.

═══ THE FORM ═══════════════════════════════════════════════════════════════════════════════════

    message = LP(DOMAIN) ‖ LP(kind) ‖ LP(key) ‖ LP(miner_id) ‖ sha256(body)[32] ‖ U64(height)

    LP(x)  = U32BE(len(x)) ‖ x        (length prefix, FIXED width)
    U32BE  = 32-bit big-endian integer; U64 = 64-bit big-endian
    DOMAIN = b"dendra/relay-write/v1"

Four properties, each for a named reason:

1. **DOMAIN SEPARATION.** Without it, a signature valid here would be replayable anywhere the same
   key signs something else. The `v1` lives inside the domain: changing the field list changes the
   domain and therefore invalidates every old signature. A format that evolves silently would
   accept messages built under the previous rule.

2. **LENGTH PREFIXES, NEVER A SEPARATOR.** This is the property that closes the malleability:
   `kind="a|b", key="c"` and `kind="a", key="b|c"` must produce DIFFERENT messages. With a `|`
   separator both would produce `a|b|c`.

3. **THE DIGEST OF THE BODY, NOT THE BODY.** The signature stays constant-size and independent of
   the payload. `sha256` is 32 bytes, a fixed width, so no prefix is needed — and that is
   justified rather than merely economical: a fixed-width field is injective by construction.

4. **ANTI-REPLAY BOUND.** A signature with no bound is valid **forever**, so a captured payload can
   be re-injected indefinitely. The height therefore enters the signed message.

═══ WHAT THIS MODULE DOES NOT DO ═══════════════════════════════════════════════════════════════
It verifies NO signature, queries no registry and authorizes nothing. It builds a message and its
digest. The decision that a write is legitimate additionally requires: verifying the signature,
deriving the address from the public key (`modea.cosmos_addr`), comparing it to `miner.Operator`
read from the registry, and enforcing the anti-replay window.

⚠️ BLIND SPOT THIS FILE CANNOT CLOSE: **where does the relay get the current height from?**
The relay has NO access to the chain, so it cannot compare `height` against a height it does not
know. This module only puts the height INSIDE the signed message, which is necessary whatever the
answer turns out to be.
"""
from __future__ import annotations

import hashlib
import struct

DOMAINE = b"dendra/relay-write/v1"

# Cap on each variable-width field. Neither arbitrary nor cosmetic: a `kind` or a `key` of several
# megabytes has no legitimate use and would offer a memory-exhaustion vector BEFORE any signature
# verification — that is, free of charge for the attacker.
MAX_CHAMP = 65535


def _lp(x: bytes) -> bytes:
    """Field prefixed with its length (U32BE). Raises ValueError past the cap."""
    if len(x) > MAX_CHAMP:
        raise ValueError(f"field of {len(x)} bytes exceeds the cap of {MAX_CHAMP}")
    return struct.pack(">I", len(x)) + x


def _to_bytes(v, name: str) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode("utf-8")
    raise ValueError(f"{name}: expected str or bytes, got {type(v).__name__}")


def canonical_message(kind, key, body, miner_id, height: int) -> bytes:
    """The EXACT message the signature covers. Deterministic, injective, escape-free.

    `body` is the raw body of the payload; its sha256 is what enters the message.
    `height` is the chain height claimed by the writer, and is the anti-replay bound.
    """
    if not isinstance(height, int) or isinstance(height, bool):
        raise ValueError(f"height: expected an integer, got {type(height).__name__}")
    if height < 0 or height > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"height out of U64 range: {height}")
    corps_b = _to_bytes(body, "body")
    return (
        _lp(DOMAINE)
        + _lp(_to_bytes(kind, "kind"))
        + _lp(_to_bytes(key, "key"))
        + _lp(_to_bytes(miner_id, "miner_id"))
        + hashlib.sha256(corps_b).digest()
        + struct.pack(">Q", height)
    )


def empreinte(kind, key, body, miner_id, height: int) -> bytes:
    """sha256 of the canonical message — 32 bytes.

    It serves two distinct purposes: (a) what the signature covers; (b) the KEY OF A SEEN-BEFORE SET
    on the relay side. A replay produces exactly the same digest, so a seen-before set rejects it
    **exactly**, without depending on a clock or a height — whereas the window depends on a time
    reference the relay does not yet have.
    """
    return hashlib.sha256(canonical_message(kind, key, body, miner_id, height)).digest()


def decoder(message: bytes):
    """Returns `(domain, kind, key, miner_id, body_digest, height)` or raises ValueError.

    ⚠️ THIS DECODER IS THE PROOF, NOT A CONVENIENCE. The injectivity of the encoding is not argued:
    it is demonstrated by reconstructing the exact input from the output. If two distinct inputs
    could produce the same message, decoding could not return both — so the round-trip test over
    adversarial cases is a real measurement, not a paraphrase of the code.
    """
    def lire_lp(m, i):
        if i + 4 > len(m):
            raise ValueError("truncated message (length prefix)")
        (n,) = struct.unpack(">I", m[i:i + 4])
        i += 4
        if i + n > len(m):
            raise ValueError(f"truncated message (a field of {n} bytes was announced)")
        return m[i:i + n], i + n

    i = 0
    dom, i = lire_lp(message, i)
    kind, i = lire_lp(message, i)
    key, i = lire_lp(message, i)
    mid, i = lire_lp(message, i)
    if i + 32 + 8 != len(message):
        raise ValueError(f"unexpected message tail ({len(message) - i} bytes, 40 expected)")
    h_corps = message[i:i + 32]
    (height,) = struct.unpack(">Q", message[i + 32:i + 40])
    return dom, kind, key, mid, h_corps, height


def message_height(message: bytes) -> int:
    """The height CARRIED BY THE MESSAGE — re-read from the bytes, never taken from alongside them.

    ⚠️ THIS FUNCTION EXISTS FOR ONE REASON, AND IT IS A SECURITY REASON. The anti-replay high-water
    mark must anchor on **the height the signature covers**. As long as the height travels as a
    parameter beside the message, nothing prevents someone from wiring in a lookalike value — the
    account **sequence number**, typically, which is an account counter, is controlled by the
    writer, and says NOTHING about chain time. Re-reading the height here makes the anchoring a
    property of the message rather than a convention between callers.

    It lives in THIS file, next to the encoder, because this is where — and nowhere else — the
    position of the height in the form is known. A position constant copied into the caller would be
    a DERIVED rule, and would diverge at the first change of form.
    """
    return decoder(message)[-1]
