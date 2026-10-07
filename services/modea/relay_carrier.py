"""Carrier of a signed write: an amino document under a DEDICATED CHAIN-ID, unbroadcastable by construction.

═══ WHY A DEDICATED CHAIN-ID, AND NOT THE REAL ONE ══════════════════════════════════════════════
The natural carrier would be a transaction document signed offline and never broadcast. The measured
trap: **such a document consumes a SEQUENCE NUMBER**. It is signed for the account's CURRENT
sequence — the one its next real transaction will use. Anyone who captures and broadcasts it **makes
the next settlement fail**. This is not a micro-transfer, it is a **free, replayable denial of
service against the account that settles jobs**.

The remedy solves two problems at once: sign under **`dendra/relay-write/v1`**.
  · **UNBROADCASTABLE BY CONSTRUCTION** — the `chain_id` enters the signed document; the chain
    recomputes the bytes under ITS chain-id and the signature no longer matches. And unlike a wrong
    sequence (which eventually becomes reachable), **it NEVER becomes valid**.
  · **IT IS the domain separation** — no need to add one elsewhere: the carrier carries it.

═══ MEASURED ON THE LIVE CHAIN, WITH A WITNESS ══════════════════════════════════════════════════
    A) witness  : real chain-id + correct sequence        -> code 0, ACCEPTED
    B) dedicated: chain-id `.../relay-write/v1`, right seq -> code 4, "signature verification
                  failed; ... chain-id (dendra): unauthorized"
    C) Python   : the signature verifies under the dedicated chain-id, and **NOT** under `dendra`.
The witness matters as much as the case: without it, a refusal would prove nothing — it could come
from the sequence, the account or the node.

THIS MODULE REFUSES TO BUILD UNDER A REAL CHAIN-ID. That is the central guard: without it, one
typo or one copy-paste brings the denial of service back. The caller is not trusted to get right the
one parameter that makes the carrier safe.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

DOMAINE_CHAIN_ID = "dendra/relay-write/v1"

# A chain-id is REFUSED unless it carries the marker: this tests what the value IS, rather than
# blacklisting the real chain-ids — a blacklist is forgotten as soon as a network is added.
MARQUEUR = "/relay-write/"


class InvalidCarrier(ValueError):
    pass


def _verifier_chain_id(chain_id: str) -> str:
    if not isinstance(chain_id, str) or not chain_id:
        raise InvalidCarrier("chain_id is empty or not a string")
    if MARQUEUR not in chain_id:
        raise InvalidCarrier(
            f"chain_id {chain_id!r} does not carry {MARQUEUR!r} — REFUSED. A document signed under a "
            f"REAL chain-id is broadcastable: it consumes the account sequence and makes its next "
            f"transaction fail. That is the denial of service this carrier exists to prevent.")
    return chain_id


def document_amino_note(note: str, adresse: str, account_number, sequence,
                        chain_id: str = DOMAINE_CHAIN_ID,
                        montant: str = "1", denom: str = "udndr") -> bytes:
    """The EXACT bytes that `dendrad tx sign --sign-mode amino-json` signs, with a free-form memo.

    SEPARATE from `document_amino()` ON PURPOSE. The two responsibilities — *rebuilding the document
    byte for byte* and *placing a 32-byte digest in it* — were welded together in the first version,
    and the test exposed it: it was impossible to verify against a REAL signature whose memo was a
    test string. A function that imposes a policy on its inputs can no longer serve as a verifier. The
    verifier takes the memo as it is; it is the ISSUER that imposes the digest.

    NORMATIVE, not stylistic: keys **sorted**, compact separators, `fee.gas` (not `gas_limit`),
    integers as **strings**. A single comma out of place and the signature no longer verifies — an
    approximate reconstruction fails silently.
    """
    _verifier_chain_id(chain_id)
    if not isinstance(note, str):
        raise InvalidCarrier(f"note must be a string (got {type(note).__name__})")
    if not isinstance(adresse, str) or "1" not in adresse:
        raise InvalidCarrier(f"invalid address: {adresse!r}")
    for name, v in (("account_number", account_number), ("sequence", sequence)):
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise InvalidCarrier(f"{name} must be an integer or its string form (got {type(v).__name__})")
        if not str(v).isdigit():
            raise InvalidCarrier(f"{name} is not numeric: {v!r}")
    doc = {
        "account_number": str(account_number),
        "chain_id": chain_id,
        "fee": {"amount": [], "gas": "200000"},
        "memo": note,
        "msgs": [{"type": "cosmos-sdk/MsgSend",
                  "value": {"amount": [{"amount": montant, "denom": denom}],
                            "from_address": adresse, "to_address": adresse}}],
        "sequence": str(sequence),
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def document_amino(empreinte: bytes, adresse: str, account_number, sequence,
                   chain_id: str = DOMAINE_CHAIN_ID,
                   montant: str = "1", denom: str = "udndr") -> bytes:
    """The document of a REAL write: the memo IS the digest, 32 bytes, in hexadecimal.

    This is where the policy applies — not in the verifier: a write to the relay must carry a 32-byte
    digest, full stop. The verifier rebuilds whatever it is given, otherwise it cannot judge a document
    it did not produce itself.
    """
    if not isinstance(empreinte, (bytes, bytearray)) or len(empreinte) != 32:
        raise InvalidCarrier(f"digest of {len(empreinte or b'')} bytes, 32 expected")
    return document_amino_note(binascii.hexlify(empreinte).decode(), adresse,
                               account_number, sequence, chain_id, montant, denom)


def empreinte_du_document(doc_signe: dict) -> bytes:
    """Extracts the digest from the memo. Raises unless it is 32 hex-encoded bytes."""
    try:
        memo = doc_signe["body"]["memo"]
    except (KeyError, TypeError) as e:
        raise InvalidCarrier(f"document without a readable memo: {type(e).__name__}") from e
    try:
        b = binascii.unhexlify(memo)
    except Exception as e:  # noqa: BLE001
        raise InvalidCarrier(f"non-hexadecimal memo: {memo!r:.40}") from e
    if len(b) != 32:
        raise InvalidCarrier(f"memo of {len(b)} bytes, 32 expected")
    return b


def elements_signature(doc_signe: dict):
    """(pubkey, signature, sequence) from the JSON produced by `dendrad tx sign -o json`.

    Reads the REAL shape of the binary (`auth_info.signer_infos[0]`) and refuses anything that is
    not exactly one signer: a multi-signer document makes no sense here, and letting the first one
    through would be choosing blindly.
    """
    try:
        infos = doc_signe["auth_info"]["signer_infos"]
        sigs = doc_signe["signatures"]
    except (KeyError, TypeError) as e:
        raise InvalidCarrier(f"unexpected structure: {type(e).__name__}") from e
    if len(infos) != 1 or len(sigs) != 1:
        raise InvalidCarrier(f"{len(infos)} signer(s) and {len(sigs)} signature(s) — exactly 1 and 1 required")
    si = infos[0]
    mode = (si.get("mode_info") or {}).get("single", {}).get("mode")
    if mode != "SIGN_MODE_LEGACY_AMINO_JSON":
        raise InvalidCarrier(
            f"mode {mode!r} — only SIGN_MODE_LEGACY_AMINO_JSON is verifiable without a Cosmos stack; "
            f"`direct` would require protobuf, which is the dependency being avoided here")
    try:
        pub = base64.b64decode((si["public_key"] or {})["key"], validate=True)
        sig = base64.b64decode(sigs[0], validate=True)
    except Exception as e:  # noqa: BLE001
        raise InvalidCarrier(f"key or signature is not base64: {type(e).__name__}") from e
    if len(pub) != 33:
        raise InvalidCarrier(f"public key of {len(pub)} bytes, 33 expected (compressed secp256k1)")
    if len(sig) != 64:
        raise InvalidCarrier(
            f"signature of {len(sig)} bytes, 64 expected — a Cosmos signature is a compact `r||s`, "
            f"NOT DER; without conversion, a valid signature fails verification")
    return pub, sig, si.get("sequence")


def verifier(account_number, sequence, chain_id: str = DOMAINE_CHAIN_ID):
    """Returns a `verify_signature(pubkey, message, signature) -> bool` for `relay_write`.

    ═══ THE MISSING LINK, AND IT WAS MISSING IN THE MIDDLE ══════════════════════════════════════
    `relay_write` verifies the signature over the CANONICAL MESSAGE; `dendrad` signs the AMINO
    DOCUMENT, whose memo is `hex(digest)`. Both halves existed, each tested on its own, and nothing
    joined them: no signature the binary can produce would have passed verification. A mechanism
    whose every part is proven may never have been assembled.

    THE ADDRESS IS DERIVED FROM THE KEY, NOT TRANSPORTED. The signed document carries the signer's
    address, and it could travel alongside. It does not: `address_from_pubkey` recomputes it, which
    removes one field to lie about and makes address and key inseparable by construction rather than
    by check. A value that can be recomputed is not a value to carry.

    `account_number` and `sequence` DO have to travel — they enter the signed document and cannot be
    derived from it. They need no validation beyond their shape: a wrong value rebuilds a different
    document, so the signature does not verify. They authenticate themselves, which is exactly why
    they can come from the client without danger.

    NEVER RAISES — `relay_write` requires it, and for a reason: an exception escaping the
    verification of not-yet-authenticated input turns a malformed signature, which anyone can send,
    into an outage of the relay. Every anomaly returns False, which is the correct verdict: "I cannot
    believe this."
    """
    def verif(pubkey, message, signature) -> bool:
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec

            from .cosmos_addr import address_from_pubkey

            digest = hashlib.sha256(message).digest()
            doc = document_amino(digest, address_from_pubkey(bytes(pubkey)),
                                 account_number, sequence, chain_id)
            pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), bytes(pubkey))
            pk.verify(signature_en_der(bytes(signature)), doc, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:  # noqa: BLE001 — any anomaly is a refusal, never an outage
            return False

    return verif


def signature_en_der(sig64: bytes) -> bytes:
    """`r||s` (64 bytes) -> DER, the form `cryptography` expects. Measured, not assumed."""
    from cryptography.hazmat.primitives.asymmetric import utils
    if len(sig64) != 64:
        raise InvalidCarrier(f"signature of {len(sig64)} bytes, 64 expected")
    return utils.encode_dss_signature(int.from_bytes(sig64[:32], "big"),
                                      int.from_bytes(sig64[32:], "big"))


def digest_document(doc_amino: bytes) -> bytes:
    return hashlib.sha256(doc_amino).digest()
