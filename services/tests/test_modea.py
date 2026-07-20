"""Tests du prototype Mode A. Lancer : pytest -q (depuis services/)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea import crypto, canary as canary_mod
from modea.client import Client
from modea.ledger import Ledger
from modea.miner import Miner

SECRET = "donnee secrete: dossier 4471-A medical"


def test_crypto_roundtrip():
    a_sk, a_pk = crypto.gen_keypair()
    b_sk, b_pk = crypto.gen_keypair()
    ka = crypto.derive_session_key(a_sk, b_pk, info=b"job")
    kb = crypto.derive_session_key(b_sk, a_pk, info=b"job")
    assert ka == kb
    sealed = crypto.encrypt(ka, b"hello", aad=b"job")
    assert crypto.decrypt(kb, sealed, aad=b"job") == b"hello"


def test_aead_rejects_wrong_aad():
    _, pk = crypto.gen_keypair()
    sk, _ = crypto.gen_keypair()
    k = crypto.derive_session_key(sk, pk, info=b"x")
    sealed = crypto.encrypt(k, b"data", aad=b"job-1")
    import pytest
    with pytest.raises(Exception):
        crypto.decrypt(k, sealed, aad=b"job-2")


def test_end_to_end_mock():
    ledger, client, miner = Ledger(), Client(), Miner("m1", backend="mock")
    sub, key = client.submit("j1", miner.pub, SECRET)
    ledger.open_job("j1", "m1", sub.client_commit)
    res = miner.handle_job("j1", sub.client_eph_pk, sub.sealed_prompt)
    out = client.open_result("j1", key, res.sealed_result)
    assert isinstance(out, str) and out


def test_no_plaintext_in_ledger():
    ledger, client, miner = Ledger(), Client(), Miner("m1", backend="mock")
    sub, key = client.submit("j1", miner.pub, SECRET)
    ledger.open_job("j1", "m1", sub.client_commit)
    res = miner.handle_job("j1", sub.client_eph_pk, sub.sealed_prompt)
    ledger.settle_job("j1", res.result_commit)
    blob = json.dumps(ledger.dump_public())
    assert "4471-A" not in blob and "medical" not in blob


def test_ciphertext_hides_plaintext():
    client, miner = Client(), Miner("m1", backend="mock")
    sub, _ = client.submit("j1", miner.pub, SECRET)
    assert b"4471-A" not in sub.sealed_prompt.ct
    assert b"medical" not in sub.sealed_prompt.ct


def test_ephemeral_keys_unique():
    client, miner = Client(), Miner("m1", backend="mock")
    s1, _ = client.submit("a", miner.pub, "x")
    s2, _ = client.submit("b", miner.pub, "x")
    assert s1.client_eph_pk != s2.client_eph_pk


def test_canary_traces_leak():
    reg = canary_mod.CanaryRegistry()
    can = canary_mod.make_canary()
    reg.register(can, "miner-XYZ")
    leaked = canary_mod.embed("un prompt fuite", can)
    assert reg.detect(leaked) == ["miner-XYZ"]
    assert reg.detect("texte sans canari") == []


def test_canary_stripped_from_output():
    can = canary_mod.make_canary()
    txt = canary_mod.embed("reponse", can)
    assert can.token not in canary_mod.strip(txt)
