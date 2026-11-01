"""Test suite for `modea.relay_canon` — injectivity is demonstrated, not argued.

The property that matters: two distinct writes cannot produce the same signed message. Rereading the
code does not prove it; two measurements do:

  (1) round trip: `decoder(encoder(x)) == x` over adversarial inputs. If two distinct inputs
      produced the same message, the decoder could not return both;
  (2) absence of collisions over a large set of neighbouring inputs built specifically to be
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
        DOMAIN, MAX_FIELD, decoder, digest, canonical_message)
except ImportError:
    from relay_canon import (  # type: ignore
        DOMAIN, MAX_FIELD, decoder, digest, canonical_message)


def run(verbose=True):
    passed = failed = 0

    def ok(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  OK   {name}")
        else:
            failed += 1
            if verbose:
                print(f"  FAIL {name} {detail}")

    def measure(name, f, detail=""):
        """Evaluates `f()` and counts an exception as a named failure, never as a crash.

        Sabotaging the encoder (length prefix replaced by a separator) must yield "1 failure", not
        an uncaught `ValueError` that kills the run without a summary line. A reader that only greps
        `RELAY_CANON_SUMMARY` would then find nothing at all — and an absence of measurement reads far
        too easily as an absence of problem. A suite must be able to report its own breakdown.
        """
        try:
            ok(name, f(), detail)
        except Exception as e:  # noqa: BLE001
            nonlocal failed
            failed += 1
            if verbose:
                print("  FAIL", f"{name}: exception {type(e).__name__}: {str(e)[:90]}")

    # -- (1) round trip: the proof of injectivity ------------------------------------------------
    if verbose:
        print("(1) round trip (the decoder returns the exact input)")
    cases = [
        ("req", "job7__miner3", b"payload", "miner3", 96437),
        ("", "", b"", "", 0),                                   # everything empty
        ("a|b", "c", b"x", "m", 1),                             # the confusable pair, half 1
        ("a", "b|c", b"x", "m", 1),                             # the confusable pair, half 2
        ("k" * 300, "K" * 300, b"\x00" * 1000, "m" * 300, 2**64 - 1),
        ("ünicode", "key__äccented", "unicode body €".encode(), "mïner", 42),  # multi-byte UTF-8 in every
        #   field: the encoder counts bytes, not characters, so a length prefix computed on characters
        #   would desynchronise the decoder here and nowhere else.
        ("\x00\xff", "\n\t", b"\x00\x01\x02", "\x00", 7),       # control bytes
    ]
    for kind, key, body, mid, h in cases:
        def _ar(kind=kind, key=key, body=body, mid=mid, h=h):
            got = decoder(canonical_message(kind, key, body, mid, h))
            expected = (DOMAIN, kind.encode(), key.encode(), mid.encode(),
                   hashlib.sha256(body).digest(), h)
            return got == expected
        measure(f"round trip {kind[:12]!r}/{key[:12]!r}", _ar)

    # -- (2) no collision over inputs built to be confusable -------------------------------------
    if verbose:
        print("(2) absence of collisions (inputs built to be confusable)")
    pieces = ["", "a", "b", "|", "a|", "|a", "a|b", "__", "a__b", "\x00", "0", "00"]
    state = {}

    def _collisions():
        seen, collisions, n = {}, [], 0
        for kind, key, mid in itertools.product(pieces, pieces, pieces[:6]):
            e = digest(kind, key, b"D", mid, 5)
            triple = (kind, key, mid)
            if e in seen and seen[e] != triple:
                collisions.append((seen[e], triple))
            seen[e] = triple
            n += 1
        state["n"], state["u"], state["c"] = n, len(seen), collisions
        return not collisions
    measure("neighbouring triples -> all digests distinct", _collisions)
    if verbose and state:
        print(f"      ({state.get('n')} triples, {state.get('u')} digests, "
              f"{len(state.get('c', []))} collision(s))")

    # the named case, isolated and explicit
    measure('kind="a|b",key="c"  !=  kind="a",key="b|c"',
           lambda: digest("a|b", "c", b"x", "m", 1) != digest("a", "b|c", b"x", "m", 1))
    measure('same, with the relay\'s real separator ("__")',
           lambda: digest("a__b", "c", b"x", "m", 1) != digest("a", "b__c", b"x", "m", 1))

    # -- (3) every field counts: a single bit changing changes the digest ------------------------
    if verbose:
        print("(3) every field really enters the digest")
    base = ("req", "job7__miner3", b"charge", "miner3", 96437)
    e0 = digest(*base)
    variants = [
        ("kind", ("rep",) + base[1:]),
        ("key", (base[0], "job7__miner4") + base[2:]),
        ("body (1 byte)", base[:2] + (b"charge!",) + base[3:]),
        ("miner_id", base[:3] + ("miner4", base[4])),
        ("height (+1)", base[:4] + (96438,)),
    ]
    for name, v in variants:
        measure(f"{name} changed -> different digest", lambda v=v: digest(*v) != e0)

    # -- (4) domain separation -------------------------------------------------------------------
    if verbose:
        print("(4) domain separation")
    measure("the domain comes first and is prefixed by its length",
           lambda: canonical_message(*base).startswith(len(DOMAIN).to_bytes(4, "big") + DOMAIN))
    ok("the domain carries a version (v1)", DOMAIN.endswith(b"/v1"))
    # A message built under another domain cannot be confused with this one; that is simulated by
    # decoding and comparing the domain field, the only place where the distinction must live.
    measure("the decoder returns the domain, it does not assume it",
           lambda: decoder(canonical_message(*base))[0] == DOMAIN)

    # -- (5) named refusals ----------------------------------------------------------------------
    if verbose:
        print("(5) malformed inputs: named refusal")
    bad_inputs = [
        ("field beyond the ceiling", lambda: canonical_message("k" * (MAX_FIELD + 1), "a", b"", "m", 1)),
        ("negative height", lambda: canonical_message("k", "a", b"", "m", -1)),
        ("height beyond U64", lambda: canonical_message("k", "a", b"", "m", 2**64)),
        ("boolean height", lambda: canonical_message("k", "a", b"", "m", True)),
        ("kind of a forbidden type", lambda: canonical_message(42, "a", b"", "m", 1)),
    ]
    for name, f in bad_inputs:
        try:
            f()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e:
            ok(name + " -> ValueError", len(str(e)) > 8, "(empty message)")

    if verbose:
        print("(6) the decoder refuses a truncated or extended message")
    m = canonical_message(*base)
    for name, mm in [("truncated by 1 byte", m[:-1]), ("extended by 1 byte", m + b"\x00"),
                    ("empty", b""), ("lying length", b"\xff\xff\xff\xff" + m)]:
        try:
            decoder(mm)
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError:
            ok(name + " -> ValueError", True)

    # -- (7) end to end: sign, verify, and bind to an operator address ---------------------------
    if verbose:
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

        # one byte of the body changes => the signature no longer holds (the point of the digest)
        msg2 = canonical_message(base[0], base[1], b"charge!", base[3], base[4])
        try:
            priv.public_key().verify(sig, msg2, ec.ECDSA(hashes.SHA256()))
            ok("body changed -> signature refused", False, "(accepted!)")
        except InvalidSignature:
            ok("body changed -> signature refused", True)

        # A different key, same message: the signature is valid but the address is not the operator's.
        # What refuses is the binding, not the signature.
        other = ec.generate_private_key(ec.SECP256K1())
        pub_a = other.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        sig_a = other.sign(msg, ec.ECDSA(hashes.SHA256()))
        other.public_key().verify(sig_a, msg, ec.ECDSA(hashes.SHA256()))  # valid on its own
        ok("foreign key: valid signature but address is not the operator's -> refused",
           not pubkey_matches_address(pub_a, operator))
    except ImportError:
        ok("end to end", False, "(`cryptography` or `cosmos_addr` missing — not measured)")

    return passed, failed


def test_relay_canon():
    _, r = run(verbose=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = run()
    print()
    print(f"RELAY_CANON_SUMMARY passed={v} failed={r}")
    sys.exit(1 if r else 0)
