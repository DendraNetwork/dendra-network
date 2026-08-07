"""The relay's signed-write policy, exercised through real HTTP in all three regimes.

The unit tests cover the pieces; this one covers the DECISION — that `off` changes nothing, that
`observe` measures without refusing, and that `enforce` actually refuses. A policy is the one thing
that cannot be proven by reading it: every regime has to be watched doing its job and, more
importantly, watched NOT doing the others'.
"""
import base64
import binascii
import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DENDRA_RELAY_STORE", tempfile.mkdtemp(prefix="dendra-signed-"))

from modea import relay_canon, relay_carrier  # noqa: E402
from modea import relay_signature as rs  # noqa: E402
from modea.cosmos_addr import address_from_pubkey  # noqa: E402

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

KIND, KEY, HEIGHT = "res", "j1__dm1abc", 77
BODY = b'{"result":"ok"}'
ACCT, SEQ = "10", "160"


def _identity():
    sk = ec.generate_private_key(ec.SECP256K1())
    pub = sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                       format=serialization.PublicFormat.CompressedPoint)
    return sk, pub, address_from_pubkey(pub)


def _headers(sk, pub, address, *, kind=KIND, key=KEY, body=BODY, miner="dm1abc", height=HEIGHT):
    """Builds the headers an honest writer sends, without going through `dendrad`."""
    message = relay_canon.canonical_message(kind, key, body, miner, height)
    doc = relay_carrier.document_amino(hashlib.sha256(message).digest(), address, ACCT, SEQ)
    der = sk.sign(doc, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return {
        rs.HEADER_MINER: miner, rs.HEADER_HEIGHT: str(height),
        rs.HEADER_PUBKEY: base64.b64encode(pub).decode(),
        rs.HEADER_SIG: base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode(),
        rs.HEADER_ACCT: ACCT, rs.HEADER_SEQ: SEQ,
    }


class Registry:
    """The miner registry the relay attributes a signed write against, injected so this file needs no
    chain. A verified signature only says "a key signed this"; it names an operator ADDRESS, and only
    the registry says which miner_id that address is allowed to write for. Tests that expect a write
    to be ACCEPTED must therefore supply one — without it the relay is right to refuse, and the two
    cases below prove it does."""

    def __init__(self, table):
        self._table = table

    def operator(self, miner_id):
        from modea import registry_cache
        return self._table.get(miner_id), registry_cache.FEES


class Relay:
    """A real relay on an ephemeral port, in a chosen regime.

    `registry=None` leaves the relay with no readable registry — the state in which nothing can be
    attributed to anyone, and in which a signed write MUST be refused."""

    def __init__(self, mode, registry=None):
        os.environ["DENDRA_RELAY_SIGN"] = mode
        os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix=f"dendra-{mode}-")
        os.environ.pop("DENDRA_RELAY_TOKEN", None)
        self.mod = importlib.reload(importlib.import_module("relay"))
        if registry is not None:
            self.mod.REGISTRY = Registry(registry)
        self.srv = self.mod.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def deposit(self, body=BODY, headers=None, kind=KIND, key=KEY):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{kind}/{key}",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stats(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/stats", timeout=5) as r:
            return json.load(r)["signature"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("DENDRA_RELAY_SIGN", None)


def test_off_changes_nothing():
    """The default must be inert: an unsigned deposit passes exactly as before."""
    with Relay("off") as r:
        assert r.deposit()[0] == 200
        assert r.stats()["mode"] == "off"


def test_observe_measures_without_refusing():
    """The regime that turns "it should work" into a number, before anything can block."""
    sk, pub, address = _identity()
    with Relay("observe") as r:
        assert r.deposit()[0] == 200, "an unsigned deposit must still pass in observe"
        assert r.deposit(headers=_headers(sk, pub, address))[0] == 200
        s = r.stats()
        assert s["unsigned"] == 1 and s["signed_ok"] == 1, s


def test_enforce_refuses_an_unsigned_deposit():
    with Relay("enforce") as r:
        code, payload = r.deposit()
        assert code == 401
        # The refusal NAMES the missing headers: a client that is merely too old must not be sent
        # looking at its key material.
        assert b"missing" in payload and rs.HEADER_SIG.encode() in payload, payload


def test_enforce_accepts_an_honest_write():
    """A guard that refuses everything is not a guard; the witness matters as much as the case."""
    sk, pub, address = _identity()
    with Relay("enforce", registry={"dm1abc": address}) as r:
        assert r.deposit(headers=_headers(sk, pub, address))[0] == 200
        assert r.stats()["signed_ok"] == 1


def test_enforce_refuses_a_signature_that_ATTRIBUTES_TO_NOBODY():
    """The other half of the same rule, and the one that costs a reveal when it is missing: the key is
    valid and the signature verifies, but the registry does not say this address may write for this
    miner. Verifying is not attributing. With no readable registry at all, the answer is the same —
    a guard that cannot identify must refuse rather than open."""
    sk, pub, address = _identity()
    other, _, other_addr = _identity()
    with Relay("enforce", registry={"dm1abc": other_addr}) as r:
        assert r.deposit(headers=_headers(sk, pub, address))[0] == 401
    with Relay("enforce") as r:
        assert r.deposit(headers=_headers(sk, pub, address))[0] == 401


def test_enforce_refuses_a_signature_made_for_ANOTHER_write():
    """The whole point: the signature travels with its write and authorises no other.

    This is the impersonation the shared token allows today — writing under someone else's key.
    """
    sk, pub, address = _identity()
    headers = _headers(sk, pub, address)
    with Relay("enforce") as r:
        assert r.deposit(body=b'{"result":"cheat"}', headers=headers)[0] == 401
        assert r.deposit(key="j1__dm1victim", headers=headers)[0] == 401
        assert r.stats()["signed_ko"] == 2


def test_enforce_refuses_someone_elses_key():
    sk, pub, address = _identity()
    _, other_pub, _ = _identity()
    headers = _headers(sk, pub, address)
    headers[rs.HEADER_PUBKEY] = base64.b64encode(other_pub).decode()
    with Relay("enforce") as r:
        assert r.deposit(headers=headers)[0] == 401


@pytest.mark.parametrize("field", list(rs.HEADERS))
def test_enforce_refuses_an_INCOMPLETE_header_set(field):
    """Each field is load-bearing: dropping any single one must refuse, and say which."""
    sk, pub, address = _identity()
    headers = _headers(sk, pub, address)
    headers.pop(field)
    with Relay("enforce") as r:
        code, payload = r.deposit(headers=headers)
        assert code == 401
        assert field.encode() in payload, f"the refusal must name {field}"


def test_an_unreadable_signature_does_not_BRING_DOWN_the_relay():
    """Malformed input arrives unauthenticated: it must be a verdict, never an outage."""
    sk, pub, address = _identity()
    headers = _headers(sk, pub, address)
    headers[rs.HEADER_SIG] = "not base64 at all !!"
    with Relay("enforce", registry={"dm1abc": address}) as r:
        assert r.deposit(headers=headers)[0] == 401
        assert r.deposit(headers=_headers(sk, pub, address))[0] == 200, "the relay must still serve"


def test_the_mode_is_published_NEXT_TO_its_counters():
    """`signed_ok: 0` means opposite things under `off` and under `enforce`."""
    with Relay("observe") as r:
        assert r.stats()["mode"] == "observe"


def test_an_unknown_mode_refuses_to_START():
    """A typo must not silently mean `off` — that is a disarmed guard nobody can see."""
    os.environ["DENDRA_RELAY_SIGN"] = "enfoce"      # the plausible typo
    try:
        with pytest.raises(SystemExit):
            importlib.reload(importlib.import_module("relay"))
    finally:
        os.environ["DENDRA_RELAY_SIGN"] = "off"
        importlib.reload(importlib.import_module("relay"))
