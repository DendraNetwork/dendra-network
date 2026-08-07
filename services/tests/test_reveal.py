"""Round-trip of the REVEAL to the fresh committee — ADR-026 J1 (reveal_helpers + modea.crypto).

The REAL module path is exercised (`reveal_job` seals to each member, `open_reveal` opens on the
member's side) without touching the network: the `relay_client.put/get` transport is replaced by an
in-memory dict. This tests the real cryptography (X25519 + HKDF + AES-256-GCM) as used in production,
not a re-implementation. Run with: pytest -q (from services/).

Covers:
  (a) two keypairs (the issuing primary, a committee member);
  (b) sealing {prompt, answer} from the primary to the member;
  (c) opening on the member's side yields the identity (round-trip);
  (d) a tampered ciphertext is REJECTED by the AEAD -> open_reveal returns None;
  (e) the wrong recipient (another key) CANNOT open it.
"""
import sys
from pathlib import Path

import pytest

# make the prototype modules importable (modea/, reveal_helpers, relay_client)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modea import crypto
import relay_client as relay
import reveal_helpers


class FakeRelay:
    """In-memory transport: replaces relay_client.put/get (key = "<kind>/<key>") for the test.

    The reveal_helpers module does `import relay_client as relay` and then calls `relay.put(...)` /
    `relay.get(...)`. Patching the `put`/`get` attributes of the `relay_client` module object therefore
    also covers the `reveal_helpers.relay` alias, which is the same module object."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def put(self, base, kind, key, obj):
        self.store[f"{kind}/{key}"] = obj
        return True

    def get(self, base, kind, key, retries=1):
        return self.store.get(f"{kind}/{key}")


@pytest.fixture
def fake_relay(monkeypatch):
    fr = FakeRelay()
    monkeypatch.setattr(relay, "put", fr.put)
    monkeypatch.setattr(relay, "get", fr.get)
    return fr


RELAY_URL = "http://relay.invalid"   # never contacted (transport is simulated)
JOB_ID = "job-deadbeef"
PROMPT = "What is the capital of France?"
ANSWER = "The capital of France is Paris."


def test_reveal_roundtrip(fake_relay):
    """(a) keypairs, (b) primary -> member sealing, (c) member-side opening is an exact round-trip."""
    # (a) X25519 identity keys
    prim_sk, prim_pk = crypto.gen_keypair()          # PRIMARY miner (issuer of the reveal)
    memb_sk, memb_pk = crypto.gen_keypair()          # member of the fresh COMMITTEE (recipient)
    my_id = "miner-primary"
    member_id = "miner-committee-1"

    # (b) the primary seals (prompt + answer) to the member, through the REAL reveal_job
    n = reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})
    assert n == 1, "exactly one reveal sealed and posted"

    # the relay sees ONLY ciphertext: no clear-text prompt or answer in the stored blob
    blob = fake_relay.store[f"reveal/{JOB_ID}__{member_id}"]
    assert set(blob) == {"client_eph_pk", "nonce", "ct"}
    assert PROMPT not in str(blob) and ANSWER not in str(blob)

    # (c) the member opens with ITS private key -> identical round-trip
    opened = reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk)
    assert opened == {"prompt": PROMPT, "answer": ANSWER}

    # guard: these variables document the roles (the primary's public key is not needed here)
    assert prim_pk and prim_sk and my_id


def test_reveal_tampered_ciphertext_rejected(fake_relay):
    """(d) a ciphertext altered by one byte is REJECTED by the AEAD -> open_reveal returns None, never raises."""
    _, prim_pk = crypto.gen_keypair()
    memb_sk, memb_pk = crypto.gen_keypair()
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})

    # tampering: flip the last byte of the GCM ciphertext/tag
    key = f"reveal/{JOB_ID}__{member_id}"
    blob = dict(fake_relay.store[key])
    ct = bytearray.fromhex(blob["ct"])
    ct[-1] ^= 0x01
    blob["ct"] = ct.hex()
    fake_relay.store[key] = blob

    # GCM authentication fails -> open_reveal swallows the exception and returns None; never doubtful plaintext
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk) is None


def test_reveal_wrong_recipient_cannot_open(fake_relay):
    """(e) a different recipient (wrong private key) CANNOT decrypt the reveal."""
    _, memb_pk = crypto.gen_keypair()                # LEGITIMATE recipient (the key used to seal)
    intruder_sk, _ = crypto.gen_keypair()            # INTRUDER: a different private key
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})

    # the intruder reads the blob addressed to someone else with ITS key -> different ECDH -> AEAD fails -> None
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, intruder_sk) is None


def test_reveal_skips_self_and_targets_others():
    """reveal_helpers.committee_pubs never targets itself: a node does not reveal to itself."""
    fr = FakeRelay()
    # two other miners publish their public key to the relay; so does self, which must be excluded
    _, a_pk = crypto.gen_keypair()
    _, b_pk = crypto.gen_keypair()
    fr.store["pub/miner-a"] = {"pub": a_pk.hex()}
    fr.store["pub/miner-b"] = {"pub": b_pk.hex()}
    fr.store["pub/me"] = {"pub": crypto.gen_keypair()[1].hex()}

    import unittest.mock as mock
    with mock.patch.object(relay, "get", fr.get):
        pubs = reveal_helpers.committee_pubs(RELAY_URL, "me", ["miner-a", "miner-b", "me"])

    assert set(pubs) == {"miner-a", "miner-b"}        # "me" excluded
    assert "me" not in pubs


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
