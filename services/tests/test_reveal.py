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
    """In-memory transport: replaces the relay_client write/read calls (key = "<kind>/<key>").

    The reveal_helpers module does `import relay_client as relay` and then calls the transport through
    that alias, which is the same module object -- so patching the attributes of `relay_client` covers
    it. BOTH write entry points are faked: `put_status`, which returns a THREE-VALUED status, and the
    boolean `put` that wraps it. A fake that covers only one of them leaves the other reaching the
    network."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def put_status(self, base, kind, key, obj, **kw):
        if f"{kind}/{key}" in self.store:
            return "exists"          # the relay's write-once guard: already stored, and readable
        self.store[f"{kind}/{key}"] = obj
        return "ok"

    def put(self, base, kind, key, obj, **kw):
        return self.put_status(base, kind, key, obj, **kw) == "ok"

    def get(self, base, kind, key, retries=1):
        return self.store.get(f"{kind}/{key}")


class _ReseauInterdit(BaseException):
    """Raised when a test reaches the real network. Derives from BaseException so that the broad
    `except Exception` guards inside relay_client cannot turn it back into a quiet return value."""


@pytest.fixture
def fake_relay(monkeypatch):
    """Fakes the transport AND makes the network unreachable, which is the load-bearing half.

    A relay call that is not faked does not raise here: `put_status` swallows its exception and
    answers "refused", `get` answers None. Both are ordinary values, so an incomplete fake reads as a
    delivery that FAILED rather than as a test that stopped testing -- the suite goes red naming the
    wrong thing, and the reader looks for a bug in the reveal path. Cutting `urlopen` inside the module
    makes any unfaked transport name itself at the call site.

    ⛔ AND IT RAISES A BaseException, WHICH IS THE WHOLE POINT. `relay_client.put_status` and `get`
    both catch `Exception` to turn a network incident into a value; an `AssertionError` raised here is
    an `Exception`, so it would be caught and converted into that same silent "refused" -- a guard that
    changes nothing, indistinguishable from one that works. Only a BaseException crosses those handlers
    intact."""
    fr = FakeRelay()
    monkeypatch.setattr(relay, "put_status", fr.put_status)
    monkeypatch.setattr(relay, "put", fr.put)
    monkeypatch.setattr(relay, "get", fr.get)

    def _interdit(*a, **k):
        raise _ReseauInterdit(
            "relay_client reached the network: a transport entry point is not faked by fake_relay")

    monkeypatch.setattr(relay.urllib.request, "urlopen", _interdit)
    return fr


RELAY_URL = "http://relay.invalid"   # never contacted (transport is simulated)
JOB_ID = "job-deadbeef"
# ADR-045 (14): a reveal key now names its AUTHOR as the last segment, so the cases must say WHO
# sealed it. Without that parameter they would test a key the code no longer writes.
PRIMARY_ID = "m-primaire-de-test"
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
    n = reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()},
                                  author_id=PRIMARY_ID)
    assert n == 1, "exactly one reveal sealed and posted"

    # the relay sees ONLY ciphertext: no clear-text prompt or answer in the stored blob
    blob = fake_relay.store[f"reveal/{JOB_ID}__{member_id}__{PRIMARY_ID}"]
    assert set(blob) == {"client_eph_pk", "nonce", "ct"}
    assert PROMPT not in str(blob) and ANSWER not in str(blob)

    # (c) the member opens with ITS private key -> identical round-trip
    opened = reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk, PRIMARY_ID)
    assert opened == {"prompt": PROMPT, "answer": ANSWER}

    # guard: these variables document the roles (the primary's public key is not needed here)
    assert prim_pk and prim_sk and my_id


def test_reveal_tampered_ciphertext_rejected(fake_relay):
    """(d) a ciphertext altered by one byte is REJECTED by the AEAD -> open_reveal returns None, never raises."""
    _, prim_pk = crypto.gen_keypair()
    memb_sk, memb_pk = crypto.gen_keypair()
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()},
                                  author_id=PRIMARY_ID)

    # tampering: flip the last byte of the GCM ciphertext/tag
    key = f"reveal/{JOB_ID}__{member_id}__{PRIMARY_ID}"
    blob = dict(fake_relay.store[key])
    ct = bytearray.fromhex(blob["ct"])
    ct[-1] ^= 0x01
    blob["ct"] = ct.hex()
    fake_relay.store[key] = blob

    # GCM authentication fails -> open_reveal swallows the exception and returns None; never doubtful plaintext
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk, PRIMARY_ID) is None


def test_reveal_wrong_recipient_cannot_open(fake_relay):
    """(e) a different recipient (wrong private key) CANNOT decrypt the reveal."""
    _, memb_pk = crypto.gen_keypair()                # LEGITIMATE recipient (the key used to seal)
    intruder_sk, _ = crypto.gen_keypair()            # INTRUDER: a different private key
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()},
                                  author_id=PRIMARY_ID)

    # the intruder reads the blob addressed to someone else with ITS key -> different ECDH -> AEAD fails -> None
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, intruder_sk, PRIMARY_ID) is None


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
