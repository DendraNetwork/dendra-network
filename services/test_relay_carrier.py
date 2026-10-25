"""Exercise of `modea.relay_carrier` — against a real signature produced by the binary, not a forged one.

The anchor is the document actually signed by `dendrad`, never a test key, under the dedicated
chain-id, the very one the chain refused to broadcast (`code 4: signature verification failed; …
chain-id (dendra): unauthorized`) while a control document signed under the real chain-id passed
(`code 0`).

Two properties are measured here, and neither is a matter of argument:
  (1) the signature verifies when the document is rebuilt under the dedicated chain-id;
  (2) the same signature does not verify under the real chain-id — that is what domain separation is:
      not a convention, an arithmetic impossibility.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _imp(name):
    try:
        return __import__(f"modea.{name}", fromlist=["*"])
    except ImportError:
        return __import__(name)


rp = _imp("relay_carrier")
ca = _imp("cosmos_addr")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

# -- document actually signed by `dendrad`, captured from a live chain ----------------------------
GW = "dendra1hdhn77237xc4zg90ujfslpkk5x24g2rjrq0zza"
PUB_B64 = "Ammp1WJTsaU9/bpBG7CxQED1WF5BAkrgZpmGSQe0s4ks"
SIG_B64 = "RrGimZ3FcHYSqrAiF225yuzvnhXc6AiobpalCPkdXOVdinukXzEeXoqdXz57bwpYnrcRzCIy/JBS0CQ8FlE7iw=="
AN, SEQ, MEMO = "10", "160", "deadbeefcafe"

SIGNED_DOC = {
    "body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend",
                           "from_address": GW, "to_address": GW,
                           "amount": [{"denom": "udndr", "amount": "1"}]}],
             "memo": MEMO},
    "auth_info": {"signer_infos": [{"public_key": {"@type": "/cosmos.crypto.secp256k1.PubKey",
                                                   "key": PUB_B64},
                                    "mode_info": {"single": {"mode": "SIGN_MODE_LEGACY_AMINO_JSON"}},
                                    "sequence": SEQ}]},
    "signatures": [SIG_B64],
}


def _verifies(doc_amino, pub, sig64):
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), pub)
    try:
        pk.verify(rp.signature_to_der(sig64), doc_amino, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:  # noqa: BLE001
        return False


def run(verbose=True):
    passed = failed = 0

    def ok(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  ok {name}")
        else:
            failed += 1
            if verbose:
                print(f"  x  {name} {detail}")

    def refused(name, f, cls=rp.InvalidCarrier):
        nonlocal passed, failed
        try:
            f()
            failed += 1
            if verbose:
                print(f"  x {name} - accepted where it had to be refused")
        except cls as e:
            passed += 1
            if verbose:
                print(f"  ok {name}" + ("" if len(str(e)) > 12 else " (message too short)"))
        except Exception as e:  # noqa: BLE001
            failed += 1
            if verbose:
                print(f"  x {name} - wrong exception {type(e).__name__}")

    # -- (1) the central guard: refuse a real chain-id --------------------------------------------
    if verbose:
        print("(1) the carrier refuses to build under a real chain-id (otherwise the DoS returns)")
    dig = binascii.unhexlify(MEMO + "00" * 26)
    for cid in ("dendra", "dendra-1", "cosmoshub-4", "", "dendra/relaywrite/v1"):
        refused(f"chain_id {cid!r} -> refused", lambda c=cid: rp.document_amino(dig, GW, 10, 160, chain_id=c))
    ok("the dedicated chain-id is accepted",
       rp.document_amino(dig, GW, 10, 160) is not None)
    ok("the domain carries a version", rp.DOMAIN_CHAIN_ID.endswith("/v1"))

    # -- (2) verification against the real signature produced by the binary -----------------------
    if verbose:
        print("(2) the real `dendrad` signature, rebuilt here")
    pub, sig, seq = rp.signature_elements(SIGNED_DOC)
    ok("33-byte public key and 64-byte signature extracted", len(pub) == 33 and len(sig) == 64)
    ok("the sequence is returned", seq == SEQ)
    ok("the address derived from the key is the gateway", ca.address_from_pubkey(pub, hrp="dendra") == GW)

    # The real signature carries the 6-byte memo "deadbeefcafe", not a 32-byte digest. That is why the
    # verifier takes the memo as is: welded to the 32-byte rule, it could not judge a document it did
    # not build itself.
    doc = rp.document_amino_note(MEMO, GW, AN, SEQ)
    ok("the rebuilt document carries the real memo", MEMO.encode() in doc)
    ok("the signature verifies under the dedicated chain-id", _verifies(doc, pub, sig))
    # and the production path does enforce the 32-byte digest
    e32 = binascii.unhexlify(MEMO + "00" * 26)
    ok("the production path puts the 32-byte digest in the memo",
       b'"memo":"' + (MEMO + "00" * 26).encode() + b'"' in rp.document_amino(e32, GW, AN, SEQ))

    # -- (3) the counter-test: domain separation is arithmetic ------------------------------------
    if verbose:
        print("(3) counter-test - the same signature under the real chain-id")
    d = json.loads(doc.decode())
    d["chain_id"] = "dendra"
    real_doc = json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
    ok("it does not verify under `dendra` - unbroadcastable by construction",
       not _verifies(real_doc, pub, sig))
    for field, val in (("account_number", "11"), ("sequence", "161"), ("memo", "00" * 32)):
        d2 = json.loads(doc.decode()); d2[field] = val
        b2 = json.dumps(d2, sort_keys=True, separators=(",", ":")).encode()
        ok(f"{field} altered -> no longer verifies", not _verifies(b2, pub, sig))

    # -- (4) refused shapes -----------------------------------------------------------------------
    if verbose:
        print("(4) malformed documents -> named refusal")
    refused("31-byte digest", lambda: rp.document_amino(b"\x00" * 31, GW, 10, 160))
    refused("invalid address", lambda: rp.document_amino(dig, "not-an-address", 10, 160))
    refused("non-numeric sequence", lambda: rp.document_amino(dig, GW, 10, "abc"))
    refused("boolean account_number", lambda: rp.document_amino(dig, GW, True, 160))
    refused("non-hexadecimal memo", lambda: rp.memo_digest({"body": {"memo": "zz"}}))
    refused("memo of the wrong size", lambda: rp.memo_digest({"body": {"memo": "abcd"}}))
    refused("document without a memo", lambda: rp.memo_digest({"body": {}}))

    wrong_mode = json.loads(json.dumps(SIGNED_DOC))
    wrong_mode["auth_info"]["signer_infos"][0]["mode_info"]["single"]["mode"] = "SIGN_MODE_DIRECT"
    refused("direct mode refused (it would require protobuf)", lambda: rp.signature_elements(wrong_mode))

    two = json.loads(json.dumps(SIGNED_DOC))
    two["auth_info"]["signer_infos"].append(two["auth_info"]["signer_infos"][0])
    refused("two signers refused", lambda: rp.signature_elements(two))

    short = json.loads(json.dumps(SIGNED_DOC))
    short["signatures"] = [base64.b64encode(b"\x00" * 70).decode()]
    refused("70-byte signature refused (r||s = 64)", lambda: rp.signature_elements(short))

    return passed, failed


def test_relay_carrier():
    _, r = run(verbose=False)
    assert r == 0, f"{r} check(s) red - run this file directly for the detail"


if __name__ == "__main__":
    v, r = run()
    print()
    print(f"CARRIER_SUMMARY pass={v} fail={r}")
    sys.exit(1 if r else 0)
