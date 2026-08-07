"""Test suite for `modea.relay_canon` — injectivity is DEMONSTRATED, not argued.

The property that matters: two distinct writes cannot produce the same signed message. Rereading the
code does not prove it; two measurements do:

  (1) **round trip**: `decoder(encoder(x)) == x` over adversarial inputs. If two distinct inputs
      produced the same message, the decoder could not return both;
  (2) **absence of collisions** over a large set of neighbouring inputs built specifically to be
      confusable — including the exact case a separator-based encoding would collide:
      `kind="a|b", key="c"` against `kind="a", key="b|c"`.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modea.relay_canon import (  # type: ignore
        DOMAINE, MAX_CHAMP, decoder, empreinte, canonical_message)
except ImportError:
    from relay_canon import (  # type: ignore
        DOMAINE, MAX_CHAMP, decoder, empreinte, canonical_message)


def lancer(verbeux=True):
    verts = rouges = 0

    def ok(name, cond, detail=""):
        nonlocal verts, rouges
        if cond:
            verts += 1
            if verbeux:
                print(f"  ✓ {name}")
        else:
            rouges += 1
            if verbeux:
                print(f"  ✗ {name} {detail}")

    def mesure(name, f, detail=""):
        """Evaluates `f()` and counts an EXCEPTION as a named failure, never as a crash.

        Sabotaging the encoder (length prefix replaced by a separator) must yield "1 failure", not
        an uncaught `ValueError` that kills the run WITHOUT a summary line. A reader that only greps
        `RESUME_RELAY_CANON` would then find nothing at all — and an absence of measurement reads far
        too easily as an absence of problem. A suite must be able to report its own breakdown.
        """
        try:
            ok(name, f(), detail)
        except Exception as e:  # noqa: BLE001
            nonlocal rouges
            rouges += 1
            if verbeux:
                print(f"  ✗ {name} — EXCEPTION {type(e).__name__}: {str(e)[:90]}")

    # -- (1) round trip: the proof of injectivity ------------------------------------------------
    if verbeux:
        print("(1) round trip (the decoder returns the EXACT input)")
    cas = [
        ("req", "job7__miner3", b"payload", "miner3", 96437),
        ("", "", b"", "", 0),                                   # everything empty
        ("a|b", "c", b"x", "m", 1),                             # the confusable pair, half 1
        ("a", "b|c", b"x", "m", 1),                             # the confusable pair, half 2
        ("k" * 300, "K" * 300, b"\x00" * 1000, "m" * 300, 2**64 - 1),
        ("ünicode", "key__äccented", "unicode body ✓".encode(), "mïner", 42),  # multi-byte UTF-8 in EVERY
        #   field: the encoder counts BYTES, not characters, so a length prefix computed on characters
        #   would desynchronise the decoder here and nowhere else.
        ("\x00\xff", "\n\t", b"\x00\x01\x02", "\x00", 7),       # control bytes
    ]
    for kind, key, body, mid, h in cas:
        def _ar(kind=kind, key=key, body=body, mid=mid, h=h):
            got = decoder(canonical_message(kind, key, body, mid, h))
            att = (DOMAINE, kind.encode(), key.encode(), mid.encode(),
                   hashlib.sha256(body).digest(), h)
            return got == att
        mesure(f"round trip {kind[:12]!r}/{key[:12]!r}", _ar)

    # -- (2) no collision over inputs built to be confusable -------------------------------------
    if verbeux:
        print("(2) absence of collisions (inputs built to be confusable)")
    morceaux = ["", "a", "b", "|", "a|", "|a", "a|b", "__", "a__b", "\x00", "0", "00"]
    state = {}

    def _collisions():
        vus, collisions, n = {}, [], 0
        for kind, key, mid in itertools.product(morceaux, morceaux, morceaux[:6]):
            e = empreinte(kind, key, b"D", mid, 5)
            cle = (kind, key, mid)
            if e in vus and vus[e] != cle:
                collisions.append((vus[e], cle))
            vus[e] = cle
            n += 1
        state["n"], state["u"], state["c"] = n, len(vus), collisions
        return not collisions
    mesure("neighbouring triples -> all digests distinct", _collisions)
    if verbeux and state:
        print(f"      ({state.get('n')} triples, {state.get('u')} digests, "
              f"{len(state.get('c', []))} collision(s))")

    # the named case, isolated and explicit
    mesure('kind="a|b",key="c"  !=  kind="a",key="b|c"',
           lambda: empreinte("a|b", "c", b"x", "m", 1) != empreinte("a", "b|c", b"x", "m", 1))
    mesure('same, with the relay\'s REAL separator ("__")',
           lambda: empreinte("a__b", "c", b"x", "m", 1) != empreinte("a", "b__c", b"x", "m", 1))

    # -- (3) every field counts: a single bit changing changes the digest ------------------------
    if verbeux:
        print("(3) every field really enters the digest")
    base = ("req", "job7__miner3", b"charge", "miner3", 96437)
    e0 = empreinte(*base)
    variantes = [
        ("kind", ("rep",) + base[1:]),
        ("key", (base[0], "job7__miner4") + base[2:]),
        ("body (1 byte)", base[:2] + (b"charge!",) + base[3:]),
        ("miner_id", base[:3] + ("miner4", base[4])),
        ("height (+1)", base[:4] + (96438,)),
    ]
    for name, v in variantes:
        mesure(f"{name} changed -> different digest", lambda v=v: empreinte(*v) != e0)

    # -- (4) domain separation -------------------------------------------------------------------
    if verbeux:
        print("(4) domain separation")
    mesure("the domain comes first and is prefixed by its length",
           lambda: canonical_message(*base).startswith(len(DOMAINE).to_bytes(4, "big") + DOMAINE))
    ok("the domain carries a version (v1)", DOMAINE.endswith(b"/v1"))
    # A message built under another domain cannot be confused with this one; that is simulated by
    # decoding and comparing the domain field, the only place where the distinction must live.
    mesure("the decoder returns the domain, it does not assume it",
           lambda: decoder(canonical_message(*base))[0] == DOMAINE)

    # -- (5) named refusals ----------------------------------------------------------------------
    if verbeux:
        print("(5) malformed inputs: NAMED refusal")
    refus = [
        ("field beyond the ceiling", lambda: canonical_message("k" * (MAX_CHAMP + 1), "a", b"", "m", 1)),
        ("negative height", lambda: canonical_message("k", "a", b"", "m", -1)),
        ("height beyond U64", lambda: canonical_message("k", "a", b"", "m", 2**64)),
        ("boolean height", lambda: canonical_message("k", "a", b"", "m", True)),
        ("kind of a forbidden type", lambda: canonical_message(42, "a", b"", "m", 1)),
    ]
    for name, f in refus:
        try:
            f()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e:
            ok(name + " -> ValueError", len(str(e)) > 8, "(empty message)")

    if verbeux:
        print("(6) the decoder refuses a truncated or extended message")
    m = canonical_message(*base)
    for name, mm in [("truncated by 1 byte", m[:-1]), ("extended by 1 byte", m + b"\x00"),
                    ("empty", b""), ("lying length", b"\xff\xff\xff\xff" + m)]:
        try:
            decoder(mm)
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError:
            ok(name + " -> ValueError", True)

    # -- (7) end to end: sign, verify, and BIND to an operator address ---------------------------
    if verbeux:
        print("(7) end to end: signature plus binding to the address (what the relay will do)")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        try:
            from modea.cosmos_addr import address_from_pubkey, pubkey_matches_address  # type: ignore
        except ImportError:
            from cosmos_addr import address_from_pubkey, pubkey_matches_address  # type: ignore

        priv = ec.generate_private_key(ec.SECP256K1())
        pub = priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        operator = address_from_pubkey(pub, hrp="dendra")
        msg = canonical_message(*base)
        sig = priv.sign(msg, ec.ECDSA(hashes.SHA256()))

        priv.public_key().verify(sig, msg, ec.ECDSA(hashes.SHA256()))
        ok("valid signature over the canonical message", True)
        ok("the public key does yield the operator address", pubkey_matches_address(pub, operator))

        # one byte of the BODY changes => the signature no longer holds (the point of the digest)
        msg2 = canonical_message(base[0], base[1], b"charge!", base[3], base[4])
        try:
            priv.public_key().verify(sig, msg2, ec.ECDSA(hashes.SHA256()))
            ok("body changed -> signature REFUSED", False, "(accepted!)")
        except InvalidSignature:
            ok("body changed -> signature REFUSED", True)

        # A DIFFERENT key, same message: the signature is valid but the address is not the operator's.
        # What refuses is the binding, not the signature.
        autre = ec.generate_private_key(ec.SECP256K1())
        pub_a = autre.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        sig_a = autre.sign(msg, ec.ECDSA(hashes.SHA256()))
        autre.public_key().verify(sig_a, msg, ec.ECDSA(hashes.SHA256()))  # valid on its own
        ok("foreign key: valid signature BUT address is not the operator's -> refused",
           not pubkey_matches_address(pub_a, operator))
    except ImportError:
        ok("end to end", False, "(`cryptography` or `cosmos_addr` missing — not measured)")

    return verts, rouges


def test_relay_canon():
    _, r = lancer(verbeux=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = lancer()
    print()
    print(f"RESUME_RELAY_CANON verts={v} rouges={r}")
    sys.exit(1 if r else 0)
