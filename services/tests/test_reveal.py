"""Round-trip de la RÉVÉLATION au comité frais — ADR-026 J1 (reveal_helpers + modea.crypto).

On exerce le VRAI chemin du module (`reveal_job` scelle vers chaque membre, `open_reveal` ouvre côté
membre) sans toucher au réseau : on remplace le transport `relay_client.put/get` par un dict mémoire.
Ainsi on teste la crypto réelle (X25519 + HKDF + AES-256-GCM) telle qu'utilisée en production, pas une
réimplémentation. Lancer : pytest -q  (depuis services/).

Couvre :
  (a) 2 keypairs (primaire émetteur, membre du comité) ;
  (b) scellement {prompt, answer} du primaire -> membre ;
  (c) ouverture côté membre == identité (round-trip) ;
  (d) ciphertext falsifié -> REJET (intégrité AEAD) -> open_reveal renvoie None ;
  (e) mauvais destinataire (autre clé) -> ne peut PAS ouvrir.
"""
import sys
from pathlib import Path

import pytest

# rendre importables les modules du prototype (modea/, reveal_helpers, relay_client)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modea import crypto
import relay_client as relay
import reveal_helpers


class FakeRelay:
    """Transport en mémoire : remplace relay_client.put/get (clé = "<kind>/<key>") le temps du test.

    Le module reveal_helpers fait `import relay_client as relay` puis appelle `relay.put(...)` /
    `relay.get(...)`. On patche donc les attributs `put`/`get` de l'objet module `relay_client`,
    ce qui couvre aussi l'alias `reveal_helpers.relay` (même objet module)."""

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


RELAY_URL = "http://relay.invalid"   # jamais contacté (transport simulé)
JOB_ID = "job-deadbeef"
PROMPT = "Quelle est la capitale de la France ?"
ANSWER = "La capitale de la France est Paris."


def test_reveal_roundtrip(fake_relay):
    """(a) keypairs, (b) scellement primaire->membre, (c) ouverture membre == round-trip identique."""
    # (a) clés d'identité X25519
    prim_sk, prim_pk = crypto.gen_keypair()          # mineur PRIMAIRE (émetteur de la révélation)
    memb_sk, memb_pk = crypto.gen_keypair()          # membre du COMITÉ frais (destinataire)
    my_id = "miner-primary"
    member_id = "miner-committee-1"

    # (b) le primaire scelle (prompt+réponse) vers le membre, via le VRAI reveal_job
    n = reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})
    assert n == 1, "exactement une révélation scellée et postée"

    # le relais ne voit QUE du chiffré : pas de prompt/réponse en clair dans le blob stocké
    blob = fake_relay.store[f"reveal/{JOB_ID}__{member_id}"]
    assert set(blob) == {"client_eph_pk", "nonce", "ct"}
    assert PROMPT not in str(blob) and ANSWER not in str(blob)

    # (c) le membre ouvre avec SA clé privée -> round-trip identique
    opened = reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk)
    assert opened == {"prompt": PROMPT, "answer": ANSWER}

    # garde-fou : ces variables servent à documenter les rôles (pub primaire non requise ici)
    assert prim_pk and prim_sk and my_id


def test_reveal_tampered_ciphertext_rejected(fake_relay):
    """(d) un ciphertext modifié d'un octet est REJETÉ par l'AEAD -> open_reveal renvoie None (pas d'exception)."""
    _, prim_pk = crypto.gen_keypair()
    memb_sk, memb_pk = crypto.gen_keypair()
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})

    # falsification : on retourne le dernier octet du tag/ciphertext GCM
    key = f"reveal/{JOB_ID}__{member_id}"
    blob = dict(fake_relay.store[key])
    ct = bytearray.fromhex(blob["ct"])
    ct[-1] ^= 0x01
    blob["ct"] = ct.hex()
    fake_relay.store[key] = blob

    # l'authentification GCM échoue -> open_reveal avale l'exception et renvoie None (jamais de clair douteux)
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, memb_sk) is None


def test_reveal_wrong_recipient_cannot_open(fake_relay):
    """(e) un autre destinataire (mauvaise clé privée) ne peut PAS déchiffrer la révélation."""
    _, memb_pk = crypto.gen_keypair()                # destinataire LÉGITIME (clé utilisée pour sceller)
    intruder_sk, _ = crypto.gen_keypair()            # INTRUS : autre clé privée
    member_id = "miner-committee-1"

    reveal_helpers.reveal_job(RELAY_URL, JOB_ID, PROMPT, ANSWER, {member_id: memb_pk.hex()})

    # l'intrus lit le blob qui M'est adressé mais avec SA clé -> ECDH différent -> AEAD échoue -> None
    assert reveal_helpers.open_reveal(RELAY_URL, JOB_ID, member_id, intruder_sk) is None


def test_reveal_skips_self_and_targets_others():
    """reveal_helpers.committee_pubs ne se cible jamais soi-même (pas de révélation à soi)."""
    fr = FakeRelay()
    # deux autres mineurs publient leur pub au relais ; moi (self) aussi mais je dois être exclu
    _, a_pk = crypto.gen_keypair()
    _, b_pk = crypto.gen_keypair()
    fr.store["pub/miner-a"] = {"pub": a_pk.hex()}
    fr.store["pub/miner-b"] = {"pub": b_pk.hex()}
    fr.store["pub/me"] = {"pub": crypto.gen_keypair()[1].hex()}

    import unittest.mock as mock
    with mock.patch.object(relay, "get", fr.get):
        pubs = reveal_helpers.committee_pubs(RELAY_URL, "me", ["miner-a", "miner-b", "me"])

    assert set(pubs) == {"miner-a", "miner-b"}        # "me" exclu
    assert "me" not in pubs


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
