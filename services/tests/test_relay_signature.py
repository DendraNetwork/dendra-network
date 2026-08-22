"""The writer side of a signed write — everything about it that can be measured without the binary.

WHAT IS AND IS NOT COVERED, because the boundary is the point.
`headers()` ends in a call to `dendrad tx sign`, which no test here can run: there is no binary, no
key and no chain in this environment. So this file covers the two things that surround that call and
that a wrong invocation would corrupt silently:

  · the DIGEST the headers commit to is the one `relay_canon` computes from the exact body sent;
  · the headers round-trip through the relay's own adapter — the producer and the verifier agree.

The call itself is closed by `relay_signature.autotest()`, run once on a machine that has the binary.
Naming that gap here is deliberate: a reader who assumed this file proved compatibility with `dendrad`
would be trusting the one link nobody measured.
"""
import base64
import binascii
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modea import relay_canon, relay_carrier, relay_signature as rs  # noqa: E402
from modea.cosmos_addr import address_from_pubkey  # noqa: E402

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

KIND, KEY, MINER, HEIGHT = "res", "j1__dm1abc", "dm1abc", 77
BODY = b'{"result":"ok"}'
ACCT, SEQ = "10", "160"


def _binary_stub(sk, address):
    """Stands in for `dendrad tx sign`: same amino document, same compact signature shape.

    It replaces the binary, and ONLY the binary. Everything it feeds the rest of the pipeline is what
    the binary feeds it, which is what makes the substitution legitimate rather than convenient —
    `test_relay_carrier.py` pins that document against a real captured signature.
    """
    def sign(cmd, entree=None):
        path = cmd[3]
        tx = json.load(open(path, encoding="utf-8"))
        memo = tx["body"]["memo"]
        doc = relay_carrier.document_amino(binascii.unhexlify(memo), address, ACCT, SEQ)
        der = sk.sign(doc, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        pub = sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                           format=serialization.PublicFormat.CompressedPoint)
        return json.dumps({
            "body": tx["body"],
            "auth_info": {"signer_infos": [{
                "public_key": {"@type": "/cosmos.crypto.secp256k1.PubKey",
                               "key": base64.b64encode(pub).decode()},
                "mode_info": {"single": {"mode": "SIGN_MODE_LEGACY_AMINO_JSON"}},
                "sequence": SEQ}]},
            "signatures": [base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()],
        })
    return sign


@pytest.fixture
def workshop(monkeypatch):
    sk = ec.generate_private_key(ec.SECP256K1())
    pub = sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                       format=serialization.PublicFormat.CompressedPoint)
    address = address_from_pubkey(pub)
    monkeypatch.setattr(rs, "_sortie", _binary_stub(sk, address))
    return sk, pub, address


def _headers(address, **kw):
    return rs.headers(kw.pop("kind", KIND), kw.pop("key", KEY), kw.pop("body", BODY),
                      kw.pop("miner_id", MINER), kw.pop("height", HEIGHT),
                      key_name="key-name", adresse=address, account_number=ACCT, sequence=SEQ, **kw)


def test_every_header_the_relay_needs_is_produced(workshop):
    _, _, address = workshop
    h = _headers(address)
    assert set(rs.HEADERS) == set(h), "producer and consumer must agree on the exact field set"


def test_the_address_is_not_transported(workshop):
    """It is recomputed from the key. Sending it would create a second place to lie."""
    _, _, address = workshop
    assert address not in json.dumps(_headers(address))


def test_the_headers_verify_through_the_relays_own_adapter(workshop):
    """Producer and verifier agree — measured through the very code the relay runs, not a copy."""
    _, _, address = workshop
    h = _headers(address)
    message = relay_canon.canonical_message(KIND, KEY, BODY, MINER, HEIGHT)
    verif = relay_carrier.verifier(h[rs.HEADER_ACCT], h[rs.HEADER_SEQ])
    assert verif(base64.b64decode(h[rs.HEADER_PUBKEY]), message,
                 base64.b64decode(h[rs.HEADER_SIG])) is True


@pytest.mark.parametrize("name,kw", [
    ("another body", {"body": b'{"result":"cheat"}'}),
    ("another key", {"key": "j1__dm1other"}),
    ("another miner", {"miner_id": "dm1other"}),
    ("another height", {"height": 78}),
])
def test_headers_signed_for_one_write_do_not_authorise_another(workshop, name, kw):
    """The point of signing a deposit: the signature travels with ITS write and no other."""
    _, _, address = workshop
    h = _headers(address)
    other = relay_canon.canonical_message(kw.get("kind", KIND), kw.get("key", KEY),
                                          kw.get("body", BODY), kw.get("miner_id", MINER),
                                          kw.get("height", HEIGHT))
    verif = relay_carrier.verifier(h[rs.HEADER_ACCT], h[rs.HEADER_SEQ])
    assert verif(base64.b64decode(h[rs.HEADER_PUBKEY]), other,
                 base64.b64decode(h[rs.HEADER_SIG])) is False


def test_the_signed_memo_is_the_digest_of_the_exact_body(workshop):
    """A body re-serialised between signing and sending is a different body, and is refused."""
    sk, pub, address = workshop
    seen = {}
    original = rs._sortie

    def spy(cmd, entree=None):
        seen["memo"] = json.load(open(cmd[3], encoding="utf-8"))["body"]["memo"]
        return original(cmd, entree)

    rs._sortie = spy
    try:
        _headers(address)
    finally:
        rs._sortie = original
    attendu = hashlib.sha256(relay_canon.canonical_message(KIND, KEY, BODY, MINER, HEIGHT)).hexdigest()
    assert seen["memo"] == attendu


def test_an_unsigned_deposit_is_impossible_to_confuse_with_a_signed_one():
    """No header set at all must be distinguishable from a partial one, by name.

    The relay refuses a partial set naming the missing field; discovering it mid-verification would
    surface as a signature error and send the operator looking at the wrong thing.
    """
    assert len(rs.HEADERS) == 6
    assert len(set(rs.HEADERS)) == 6, "a duplicated header name would silently drop a field"


def test_a_missing_account_number_is_an_error_not_a_zero(monkeypatch):
    """No account_number means the chain has never seen this account. Inventing 0 signs for another."""
    monkeypatch.setattr(rs, "_sortie", lambda cmd, entree=None: json.dumps({"account": {}}))
    with pytest.raises(rs.SignatureIndisponible):
        rs.numero_et_sequence("dendra1whatever")


def test_an_ABSENT_sequence_is_ZERO_not_an_error(monkeypatch):
    """The exact shape a fresh miner's account has, and the one that must NOT be refused.

    proto3 omits a field at its zero value, so an account that has never sent a transaction returns
    `{"account_number": "11", "address": "..."}` — no `sequence` at all. Reading that as a failure
    refuses to sign for precisely the accounts that have never signed, which is every new operator.

    It is safe to default here, and the reason is specific rather than general: the sequence enters
    the SIGNED document, so a wrong value yields a signature that does not verify. A field that
    cannot lie undetected may be defaulted; one that can, may not — that is the whole asymmetry.
    """
    monkeypatch.setattr(rs, "_sortie", lambda cmd, entree=None: json.dumps(
        {"account": {"account_number": "11", "address": "dendra1frais"}}))
    assert rs.numero_et_sequence("dendra1frais") == ("11", "0")


def test_a_sequence_explicitly_zero_reads_the_same_as_an_absent_one(monkeypatch):
    """"Absent" and "zero" must converge, because on the wire they are the same account."""
    monkeypatch.setattr(rs, "_sortie", lambda cmd, entree=None: json.dumps(
        {"account": {"account_number": "11", "sequence": "0", "address": "dendra1frais"}}))
    assert rs.numero_et_sequence("dendra1frais") == ("11", "0")


def test_the_address_is_derived_from_the_key_name_not_asked_for(monkeypatch):
    """One input cannot disagree with itself.

    Asking for a key name AND its address lets a caller supply a mismatched pair, and the failure then
    points at neither. It also invites the worse case: a documented command with two placeholders, run
    with the placeholders still in it.
    """
    monkeypatch.setattr(rs, "_sortie", lambda cmd, entree=None: "dendra1derivee\n")
    assert rs.address_from_key("ma-cle") == "dendra1derivee"


def test_the_client_signs_in_ONE_place(monkeypatch):
    """Six deposit sites, one place that knows how to sign — and it stays inert unconfigured.

    Teaching six call sites to sign is six chances to forget one, and the forgotten one fails only
    once enforcement is armed, as a refusal this client reports as a bare False.
    """
    import importlib
    monkeypatch.delenv("DENDRA_SIGN_KEY", raising=False)
    rc = importlib.reload(importlib.import_module("relay_client"))
    assert rc._signature("res", "k", b"{}", "dm1x", 1) == {}, "unconfigured must stay inert"

    monkeypatch.setenv("DENDRA_SIGN_KEY", "ma-cle")
    rc = importlib.reload(importlib.import_module("relay_client"))
    monkeypatch.setattr(rc, "_signature", lambda *a: {"X-Dendra-Sig": "test"})
    seen = {}

    class _Resp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=0):
        seen.update(req.headers)
        return _Resp()

    monkeypatch.setattr(rc.urllib.request, "urlopen", _urlopen)
    assert rc.put("http://x", "res", "k", {"a": 1}) is True
    assert any(k.lower() == "x-dendra-sig" for k in seen), seen


def test_the_account_is_read_ONCE_not_per_deposit(monkeypatch):
    """A chain round-trip per deposit, bought for a property that was already free.

    The relay rebuilds the document from the sequence CARRIED IN THE HEADER, so writer and verifier
    agree whatever the chain has moved on to. The pair must be consistent, not current — and the
    signature still proves what matters, because the digest of this exact write is in the signed memo.
    """
    import importlib
    monkeypatch.setenv("DENDRA_SIGN_KEY", "ma-cle")
    rc = importlib.reload(importlib.import_module("relay_client"))
    reads = {"n": 0}

    def _account(address, node=None, dendrad="dendrad"):
        reads["n"] += 1
        return ("11", "0")

    monkeypatch.setattr(rs, "address_from_key", lambda *a, **k: "dendra1x")
    monkeypatch.setattr(rs, "numero_et_sequence", _account)
    monkeypatch.setattr(rs, "headers", lambda *a, **k: {"X-Dendra-Sig": "ok"})
    for _ in range(5):
        rc._signature("res", "k", b"{}", "dm1x", 1)
    assert reads["n"] == 1, f"{reads['n']} account reads for 5 deposits"


def test_a_signing_failure_does_not_stop_the_miner(monkeypatch, capsys):
    """A miner that was working a second ago must not die because the keyring moved.

    The deposit leaves unsigned and is refused only if the relay is armed — a visible 401, not a
    crash. But it is said ONCE: a silent fallback to unsigned is how an operator ends up armed, mute
    and convinced the network is quiet.
    """
    import importlib
    monkeypatch.setenv("DENDRA_SIGN_KEY", "ma-cle")
    rc = importlib.reload(importlib.import_module("relay_client"))
    monkeypatch.setattr(rs, "address_from_key",
                        lambda *a, **k: (_ for _ in ()).throw(rs.SignatureIndisponible("keyring gone")))
    assert rc._signature("res", "k", b"{}", "dm1x", 1) == {}
    assert "UNSIGNED" in capsys.readouterr().out
    assert rc._signature("res", "k", b"{}", "dm1x", 1) == {}, "still inert, and silent the second time"


def test_a_signing_failure_says_why(monkeypatch):
    """A bare False here becomes "0 jobs" in an operator's terminal, which explains nothing."""
    def echoue(cmd, entree=None):
        raise rs.SignatureIndisponible("keyring locked")
    monkeypatch.setattr(rs, "_sortie", echoue)
    with pytest.raises(rs.SignatureIndisponible, match="keyring locked"):
        rs.numero_et_sequence("dendra1whatever")
