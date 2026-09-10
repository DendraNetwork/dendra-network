"""The adapter that joins the two halves of a signed relay write — proven in both directions.

WHAT THIS FILE COVERS, AND WHAT IT DELIBERATELY DOES NOT.
`relay_write` verifies a signature over the CANONICAL MESSAGE; `dendrad` signs the AMINO DOCUMENT
whose memo is `hex(digest)`. Both halves were implemented and tested separately, and nothing joined
them, so no signature the binary can produce would have passed verification. `relay_carrier.
verifier` is that join, and this file measures it.

THE PROOF IS SPLIT ON PURPOSE, because the two parts cannot be proven the same way:
  · that the DOCUMENT FORMAT and the ECDSA path are the ones `dendrad` really produces is settled by
    `test_relay_carrier.py`, against a signature captured from the binary on a live chain. No test can
    re-derive that without the binary, and inventing a signature here would prove only self-consistency.
  · that the ADAPTER builds the right document FROM THE RIGHT DIGEST — that a lie about any signed
    field is refused — is what this file settles, with an ephemeral key. That part needs no binary:
    it is about which bytes are handed to a verifier already known to be good.

Stating the split matters more than the split itself: a reader who assumed the ephemeral key proved
compatibility with `dendrad` would be trusting something nobody measured.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea import relay_canon, relay_carrier  # noqa: E402
from modea.cosmos_addr import address_from_pubkey  # noqa: E402

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

ACCOUNT_NUMBER, SEQUENCE = "10", "160"
KIND, KEY, MINER, HEIGHT = "res", "j1__dm1abcdef", "dm1abcdef", 4242
BODY = b'{"result":"ok"}'


def _key():
    """An ephemeral secp256k1 key, and its 33-byte compressed public form."""
    sk = ec.generate_private_key(ec.SECP256K1())
    pub = sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                       format=serialization.PublicFormat.CompressedPoint)
    return sk, pub


def _sign(sk, doc: bytes) -> bytes:
    """DER -> the compact r||s form a Cosmos signature actually carries."""
    from cryptography.hazmat.primitives.asymmetric import utils
    der = sk.sign(doc, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _write(sk, pub, *, kind=KIND, key=KEY, body=BODY, miner=MINER, height=HEIGHT,
           account_number=ACCOUNT_NUMBER, sequence=SEQUENCE,
           chain_id=relay_carrier.DOMAINE_CHAIN_ID):
    """Produces (message, signature) exactly as an honest writer would."""
    message = relay_canon.canonical_message(kind, key, body, miner, height)
    doc = relay_carrier.document_amino(hashlib.sha256(message).digest(),
                                       address_from_pubkey(pub), account_number, sequence, chain_id)
    return message, _sign(sk, doc)


def test_an_honest_write_is_accepted():
    sk, pub = _key()
    message, sig = _write(sk, pub)
    assert relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE)(pub, message, sig) is True


@pytest.mark.parametrize("name,changed", [
    # Each case changes ONE field that entered the signed document. The rebuilt document differs, so
    # the signature no longer verifies — that is what makes those fields self-authenticating, and why
    # they can travel from the client without being validated on arrival.
    ("another body", {"body": b'{"result":"cheat"}'}),
    ("another key", {"key": "j1__dm1other"}),
    ("another miner", {"miner": "dm1other"}),
    ("another height", {"height": 4243}),
    ("another kind", {"kind": "reveal"}),
])
def test_lying_about_a_signed_field_invalidates_the_signature(name, changed):
    sk, pub = _key()
    honest, sig = _write(sk, pub)
    forged = relay_canon.canonical_message(changed.get("kind", KIND), changed.get("key", KEY),
                                           changed.get("body", BODY), changed.get("miner", MINER),
                                           changed.get("height", HEIGHT))
    assert honest != forged, f"this case measures nothing: {name} yields the same message"
    assert relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE)(pub, forged, sig) is False


def test_account_number_and_sequence_cannot_be_lied_about():
    """They travel from the client, and that is safe precisely because a wrong value refuses itself."""
    sk, pub = _key()
    message, sig = _write(sk, pub)
    assert relay_carrier.verifier("11", SEQUENCE)(pub, message, sig) is False
    assert relay_carrier.verifier(ACCOUNT_NUMBER, "161")(pub, message, sig) is False


def test_the_dedicated_chain_id_is_what_makes_the_document_unbroadcastable():
    """A signature made under the dedicated chain-id must not verify as though made under another.

    This is the domain separation the carrier exists for: the document cannot be broadcast, and a
    document signed elsewhere cannot be replayed here.
    """
    sk, pub = _key()
    message, sig = _write(sk, pub, chain_id="dendra/relay-write/v1")
    assert relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE,
                                      chain_id="dendra/relay-write/v1")(pub, message, sig) is True
    with pytest.raises(relay_carrier.InvalidCarrier):
        # The builder REFUSES a real chain-id outright: the guard sits upstream of any signature.
        relay_carrier.document_amino(b"\x00" * 32, address_from_pubkey(pub),
                                     ACCOUNT_NUMBER, SEQUENCE, "dendra")


def test_someone_elses_key_does_not_pass():
    sk, pub = _key()
    _, other_pub = _key()
    message, sig = _write(sk, pub)
    assert relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE)(other_pub, message, sig) is False


@pytest.mark.parametrize("pub,sig", [
    (b"", b"\x00" * 64),                    # no key
    (b"\x02" * 33, b""),                    # no signature
    (b"\x02" * 33, b"\x00" * 63),           # truncated signature
    (b"\x02" * 10, b"\x00" * 64),           # key of the wrong size
    (b"\x02" * 33, b"\xff" * 64),           # right-sized signature meaning nothing
    (None, None),                           # not even bytes
])
def test_absurd_input_returns_False_and_NEVER_raises(pub, sig):
    """`relay_write` requires this callback never to raise, and the requirement is load-bearing.

    It runs on input that has not been authenticated yet: an exception escaping here turns a malformed
    signature — something anyone can send — into a fault of the relay itself. A refusal is a verdict;
    a traceback is an outage.
    """
    assert relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE)(pub, b"whatever", sig) is False


def test_the_verifier_matches_what_relay_write_injects():
    """Signature compatibility with the injection point, so the join cannot rot silently."""
    import inspect
    verif = relay_carrier.verifier(ACCOUNT_NUMBER, SEQUENCE)
    assert len(inspect.signature(verif).parameters) == 3
    sk, pub = _key()
    message, sig = _write(sk, pub)
    assert verif(pub, message, sig) is True
