"""Writer side of a signed relay write: turn a deposit into the headers the relay can verify.

WHY THIS FILE EXISTS
`relay_write` (verification) and `relay_carrier` (the carrier) were both implemented and tested, and
nothing produced a signed write. This module is the producer, and it is deliberately the ONLY place
that knows the wire format: the relay reads exactly what is written here, so the format lives in one
file rather than in an agreement between two.

WHAT TRAVELS, AND WHAT DOES NOT
    X-Dendra-Miner    the identifier the write claims
    X-Dendra-Height   the block height the signed message carries
    X-Dendra-Pubkey   base64, 33 bytes, compressed secp256k1
    X-Dendra-Sig      base64, 64 bytes, compact r||s
    X-Dendra-Acct     account_number
    X-Dendra-Seq      sequence
The ADDRESS is NOT sent: the relay recomputes it from the public key. A value that can be recomputed
is not a value to carry — carrying it would create a second place to lie.

None of these fields needs to be trusted on arrival. Every one of them enters the signed document, so
a wrong value rebuilds a different document and the signature simply fails. That is the property that
lets them come from the client at all.

THE SIGNATURE IS PRODUCED BY `dendrad`, NOT HERE
No private key is read by this process: the key stays in the keyring and `dendrad tx sign` is asked
for a signature over a transaction that CANNOT BE BROADCAST — its chain-id is `dendra/relay-write/v1`,
which no chain will accept. That is what stops a captured write from becoming a denial of service
against the account's next real settlement (see `relay_carrier`, which measured it against a witness).

⚠️ WHAT IS NOT PROVEN HERE. The exact `dendrad tx sign` invocation below cannot be exercised without
the binary, a funded account and a live chain — none of which exist in the environment where this file
was written. What IS proven is everything downstream of it: `test_relay_carrier.py` verifies a REAL
binary signature against a rebuilt document, and `tests/test_relay_carrier_verifier.py` verifies
that the relay's adapter rebuilds the right document. The unproven link is the invocation itself, and
`autotest()` below exists to close it in one command before anyone relies on it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import subprocess
import tempfile

from . import relay_canon, relay_carrier

HEADER_MINER = "X-Dendra-Miner"
HEADER_HEIGHT = "X-Dendra-Height"
HEADER_PUBKEY = "X-Dendra-Pubkey"
HEADER_SIG = "X-Dendra-Sig"
HEADER_ACCT = "X-Dendra-Acct"
HEADER_SEQ = "X-Dendra-Seq"

#: Every header the relay needs. Named once so the server can refuse a partial set by name rather
#: than discovering a missing field halfway through verification.
HEADERS = (HEADER_MINER, HEADER_HEIGHT, HEADER_PUBKEY, HEADER_SIG, HEADER_ACCT, HEADER_SEQ)


class SignatureIndisponible(RuntimeError):
    """Signing failed, and the caller must be told WHY rather than shown a silent False.

    A deposit that cannot be signed is a deposit that will be refused. Returning a bare failure here
    is how an operator ends up reading "0 jobs" and concluding the network is quiet.
    """


def _sortie(cmd, entree=None) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, input=entree, timeout=60)
    if p.returncode != 0:
        raise SignatureIndisponible(f"{' '.join(cmd[:3])} -> code {p.returncode}: "
                                    f"{(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def numero_et_sequence(adresse: str, node: str | None = None, dendrad: str = "dendrad"):
    """Reads (account_number, sequence) for an address. The two are NOT treated the same way.

    ⚠️ THE ZERO RULE, AND THE SIDE EACH FIELD FALLS ON. proto3 omits a field at its zero value, so
    `sequence` is ABSENT from the JSON of an account that has never sent a transaction — which is the
    normal state of a fresh miner, not an anomaly. Reading that absence as an error refuses to sign
    for exactly the accounts that most need to.

      · `account_number` ABSENT  -> ERROR. The chain has never seen this account; there is no number
        to guess, and inventing 0 would build a document for a different account.
      · `sequence` ABSENT        -> 0, the zero value of its type. It is legitimate and it is safe
        here for a reason this module already relies on: the sequence enters the SIGNED document, so
        a wrong value produces a signature that simply does not verify. It authenticates itself.

    The asymmetry is the point. "Never default anything" and "always default to zero" are both wrong;
    what decides is whether the value can lie undetected — and a signed field cannot.
    """
    cmd = [dendrad, "query", "auth", "account", adresse, "--output", "json"]
    if node:
        cmd += ["--node", node]
    brut = json.loads(_sortie(cmd))
    compte = brut.get("account", brut)
    for cle in ("value", "base_account"):          # the shape depends on the account type
        if isinstance(compte.get(cle), dict):
            compte = compte[cle]
    an = compte.get("account_number")
    if an is None:
        raise SignatureIndisponible(
            f"account {adresse} carries no account_number (keys: {sorted(compte)[:8]}). "
            f"The chain has never seen this account: fund it, then sign.")
    return str(an), str(compte.get("sequence") or 0)


def headers(kind: str, key: str, body: bytes, miner_id: str, height: int, *,
            key_name: str, adresse: str, account_number, sequence,
            keyring_backend: str = "test", keyring_dir: str | None = None,
            dendrad: str = "dendrad") -> dict:
    """Produces the headers for one signed deposit. `body` must be the EXACT bytes that will be sent.

    "Exact" is not a caution, it is the contract: the signature covers a digest of the body, so a body
    re-serialised between signing and sending — a different key order, a space after a colon — is a
    different body, and the relay refuses it. The caller therefore signs the bytes it will transmit,
    never the object it will re-encode.
    """
    empreinte = relay_canon.empreinte(kind, key, body, miner_id, height)
    doc = {
        "body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend",
                               "from_address": adresse, "to_address": adresse,
                               "amount": [{"denom": "udndr", "amount": "1"}]}],
                 "memo": binascii.hexlify(empreinte).decode(),
                 "timeout_height": "0", "extension_options": [],
                 "non_critical_extension_options": []},
        "auth_info": {"signer_infos": [],
                      "fee": {"amount": [], "gas_limit": "200000", "payer": "", "granter": ""}},
        "signatures": [],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(doc, f)
        path = f.name
    try:
        cmd = [dendrad, "tx", "sign", path, "--from", key_name,
               "--chain-id", relay_carrier.DOMAINE_CHAIN_ID,
               "--sign-mode", "amino-json", "--offline",
               "--account-number", str(account_number), "--sequence", str(sequence),
               "--keyring-backend", keyring_backend, "--output", "json"]
        if keyring_dir:
            cmd += ["--keyring-dir", keyring_dir]
        signe = json.loads(_sortie(cmd))
    finally:
        os.unlink(path)

    pub, sig, _ = relay_carrier.elements_signature(signe)
    return {
        HEADER_MINER: miner_id,
        HEADER_HEIGHT: str(height),
        HEADER_PUBKEY: base64.b64encode(pub).decode(),
        HEADER_SIG: base64.b64encode(sig).decode(),
        HEADER_ACCT: str(account_number),
        HEADER_SEQ: str(sequence),
    }


def address_from_key(key_name: str, *, keyring_backend: str = "test",
                   keyring_dir: str | None = None, dendrad: str = "dendrad") -> str:
    """The address of a keyring key. Derived, never asked for.

    A caller that has to supply BOTH a key name and its address can supply a mismatched pair, and the
    failure surfaces as a signature error pointing at neither. One input cannot disagree with itself.
    """
    cmd = [dendrad, "keys", "show", key_name, "-a", "--keyring-backend", keyring_backend]
    if keyring_dir:
        cmd += ["--keyring-dir", keyring_dir]
    return _sortie(cmd).strip()


def autotest(key_name: str, adresse: str | None = None, *, node: str | None = None,
             keyring_backend: str = "test", keyring_dir: str | None = None,
             dendrad: str = "dendrad") -> bool:
    """Closes the one link no unit test can reach: does `dendrad` really sign what the relay rebuilds?

    Run this ONCE, on a machine that has the binary and the key, BEFORE arming enforcement on the
    relay. It signs a throwaway write and verifies it with the relay's own adapter — the same code
    path, not an imitation. If it prints OK, a real deposit will verify; if it does not, arming
    enforcement would refuse every write and the symptom would be an empty network.

    ONE argument, and that is deliberate. The address is derived from the key name; asking for both
    would let a caller paste a mismatched pair — or, as happened, paste the placeholders themselves.
    A command with a hole to fill is a command that gets run with the hole still in it.
    """
    kind, key, body, height = "res", "autotest__" + key_name, b'{"autotest":true}', 1
    if adresse is None:
        adresse = address_from_key(key_name, keyring_backend=keyring_backend,
                                 keyring_dir=keyring_dir, dendrad=dendrad)
        print(f"  cle={key_name} -> {adresse}")
    an, seq = numero_et_sequence(adresse, node=node, dendrad=dendrad)
    h = headers(kind, key, body, key_name, height, key_name=key_name, adresse=adresse,
                account_number=an, sequence=seq, keyring_backend=keyring_backend,
                keyring_dir=keyring_dir, dendrad=dendrad)
    message = relay_canon.canonical_message(kind, key, body, key_name, height)
    verif = relay_carrier.verifier(h[HEADER_ACCT], h[HEADER_SEQ])
    ok = verif(base64.b64decode(h[HEADER_PUBKEY]), message, base64.b64decode(h[HEADER_SIG]))
    print(f"  account_number={an} sequence={seq}")
    print(f"  digest         ={hashlib.sha256(message).hexdigest()}")
    print("  " + ("OK — dendrad signs exactly what the relay rebuilds; enforcement can be armed."
                  if ok else
                  "FAILED — do NOT arm enforcement: every write would be refused. "
                  "Check the chain-id, the sign mode, and the account/sequence pair."))
    return ok
