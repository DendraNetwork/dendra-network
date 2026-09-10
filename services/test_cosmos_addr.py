"""Exercises `modea.cosmos_addr` against an EXTERNAL truth, not against itself.

Why this test is written this way
--------------------------------
A round trip (encode then decode) proves NOTHING: an implementation that is wrong CONSISTENTLY
passes it without complaint. At least one anchor has to come from outside.

Two anchors, of different natures:

  (1) BIP-173 itself, through its example SegWit addresses: the witness program (the bytes) and the
      address (the string) are published TOGETHER in the specification. Encoding the bytes and
      recovering the exact string exercises the polymod, the HRP expansion, the 8-to-5 bit
      conversion and the character table in one shot — none of the four can be wrong without the
      string differing. These vectors come from ANOTHER chain (Bitcoin) deliberately: a vector
      copied from our own output would only be a mirror.

  (2) the `dendrad` binary itself: `keys list -o json` and `query auth accounts -o json` publish the
      public key AND the address the chain associates with it. That is the ONLY witness proving that
      `ripemd160(sha256(pk))` is DENDRA's rule and not merely Bitcoin's. Without such a file, that
      anchor is declared NOT MEASURED; it is never green by default.

Pairing. Ground-truth harvesting refuses any object carrying SEVERAL public-key fields. In the
output of `query staking validators`, `operator_address` (dendravaloper1...) and `consensus_pubkey`
(ed25519, which yields a dendravalcons1... address) live in the SAME object without being paired.
Bringing them together would produce a perfectly false red.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modea.cosmos_addr import (  # type: ignore
        address_from_pubkey, bech32_decode, bech32_encode, pubkey_matches_address, _convertbits)
except ImportError:  # run directly from the module directory
    from cosmos_addr import (  # type: ignore
        address_from_pubkey, bech32_decode, bech32_encode, pubkey_matches_address, _convertbits)


# ── ground-truth harvesting ─────────────────────────────────────────────────────────────────────
def _is_key_field(name):
    """Does a field carry a public key? A MECHANICAL test on the name, not an allow-list.

    An allow-list such as `("pub_key","pubkey","public_key")` silently omits `consensus_pubkey` while
    the docstring above it claims to cover it: the comment then promises a guard the code does not
    implement. A test on the NAME cannot drift out of sync with a list somebody forgets to update.
    """
    n = str(name).lower()
    return "pubkey" in n or "pub_key" in n or "public_key" in n


def _key_material(pk):
    """(algorithm, raw bytes) from a public-key object, identified by its SHAPE, not by its name.

    Reading `pk["key"]` and `pk["@type"]` follows the JSON-proto convention, but the binary emits the
    amino form:

        {"type": "/cosmos.crypto.secp256k1.PubKey", "value": "AnI/XUJ..."}

    so `pk.get("key")` always returns None, yields zero pairs out of sixteen accounts that carry one,
    and turns the bench red against the derivation when only the READING was wrong.

    Replacing `key` with `value` would just be another copy that diverges again. The material is
    therefore identified by WHAT IT IS: the only value that decodes under STRICT base64 to 32 bytes
    (ed25519) or 33 (compressed secp256k1). A type string such as `/cosmos.crypto....PubKey` contains
    `.` and `/`, so it fails strict decoding and cannot be confused with key material. The algorithm
    is taken from the DECLARED type when present; the length is only a fallback.
    """
    if not isinstance(pk, dict):
        return None
    algo = raw = None
    for v in pk.values():
        if not isinstance(v, str) or not v:
            continue
        low = v.lower()
        if "ed25519" in low:
            algo = "ed25519"
        elif "secp256k1" in low:
            algo = "secp256k1"
        else:
            try:
                d = base64.b64decode(v, validate=True)
            except Exception:  # noqa: BLE001
                continue
            if len(d) in (32, 33):
                raw = d
    if raw is None:
        return None
    return (algo or ("ed25519" if len(raw) == 32 else "secp256k1")), raw


def _harvest(o, out, ambiguous):
    """(address, algorithm, bytes) triples found in `dendrad` JSON, whatever its shape.

    Accepts `keys list` (key as a nested JSON string) as well as `query auth accounts` (key as an
    object), in proto form (`@type`/`key`) as well as amino form (`type`/`value`). No schema is
    hard-coded: the exact shape changes between subcommands and between SDK versions, and a reader
    that assumes the shape silently returns "0 pairs" the day it moves.

    Pairing. An object carrying SEVERAL key fields is counted as ambiguous and DISCARDED, never
    guessed: in `query staking validators`, `operator_address` and `consensus_pubkey` coexist without
    being paired, and bringing them together would produce a perfectly false red. Only the field
    named exactly `address` is retained; it is the only one whose pairing is guaranteed.
    """
    if isinstance(o, dict):
        addr = o.get("address")
        fields = [k for k in o if _is_key_field(k) and o.get(k)]
        if isinstance(addr, str) and "1" in addr and fields:
            if len(fields) > 1:
                ambiguous.append(addr)
            else:
                pk = o[fields[0]]
                if isinstance(pk, str):
                    try:
                        pk = json.loads(pk)
                    except Exception:  # noqa: BLE001
                        pk = None
                mat = _key_material(pk)
                if mat is not None:
                    out.append((addr, mat[0], mat[1]))
        for v in o.values():
            _harvest(v, out, ambiguous)
    elif isinstance(o, list):
        for v in o:
            _harvest(v, out, ambiguous)
    return out


def run_suite(verbose=True):
    greens = reds = not_measured = 0

    def ok(name, cond, detail=""):
        nonlocal greens, reds
        if cond:
            greens += 1
            if verbose:
                print(f"  ✓ {name}")
        else:
            reds += 1
            if verbose:
                print(f"  ✗ {name} {detail}")

    def nm(name, why):
        nonlocal not_measured
        not_measured += 1
        if verbose:
            print(f"  ~ {name}: NOT MEASURED — {why}")

    # ── (1) external anchor: BIP-173 SegWit vectors ─────────────────────────────────────────────
    if verbose:
        print("(1) external anchor (BIP-173, SegWit witness programs)")
    segwit = [
        ("bc", 0, "751e76e8199196d454941c45d1b3a323f1433bd6",
         "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"),
        ("tb", 0, "751e76e8199196d454941c45d1b3a323f1433bd6",
         "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"),
        # P2WSH: the HRP of this vector is `tb`, NOT `bc`. Transcribed as `bc`, the test goes red and
        # the fault is in the TRANSCRIPTION, not in the code: the first 52 characters already match,
        # and only the checksum, which depends on the HRP, differs.
        ("tb", 0, "1863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262",
         "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"),
    ]
    for hrp, ver, prog_hex, attendu in segwit:
        got = bech32_encode(hrp, [ver] + _convertbits(binascii.unhexlify(prog_hex), 8, 5))
        ok(f"{attendu[:18]}...", got == attendu, f"\n      got      {got}\n      expected {attendu}")
        d = bech32_decode(attendu)
        ok(f"decoding {hrp}1... returns the HRP", d is not None and d[0] == hrp)

    # ── (2) checksum and rejected forms ─────────────────────────────────────────────────────────
    if verbose:
        print("(2) checksum")
    base = segwit[0][3]
    ok("one altered character -> refused",
       bech32_decode(base[:-1] + ("5" if base[-1] != "5" else "4")) is None)
    ok("mixed case -> refused", bech32_decode("bc1QW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4") is None)
    ok("no separator -> refused", bech32_decode("pzry9x0s0muk") is None)
    ok("character outside the alphabet -> refused",
       bech32_decode("bc1bw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4") is None)
    ok("empty HRP -> refused", bech32_decode("1" + base.split("1", 1)[1]) is None)
    # Length: CONSTRUCTED, not transcribed. 90 is the upper bound in BIP-173.
    data90 = _convertbits(b"\x01" * 30, 8, 5)
    a90 = bech32_encode("d" * 20, data90)
    ok(f"length {len(a90)} <= 90 accepted", len(a90) <= 90 and bech32_decode(a90) is not None)
    a91 = bech32_encode("d" * (20 + (91 - len(a90))), data90)
    ok(f"length {len(a91)} > 90 refused", len(a91) > 90 and bech32_decode(a91) is None)

    # ── (3) derivation over real secp256k1 keys ─────────────────────────────────────────────────
    if verbose:
        print("(3) derivation over real secp256k1 keys (200 draws)")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        def _pub():
            return ec.generate_private_key(ec.SECP256K1()).public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)

        mauvais = faux_positifs = 0
        for _ in range(200):
            pub = _pub()
            a = address_from_pubkey(pub, hrp="dendra")
            if not (a.startswith("dendra1") and bech32_decode(a) is not None):
                mauvais += 1
            if not pubkey_matches_address(pub, a):
                mauvais += 1
            i = len(a) - 3  # one altered character INSIDE the data: must no longer match
            if pubkey_matches_address(pub, a[:i] + ("q" if a[i] != "q" else "p") + a[i + 1:]):
                faux_positifs += 1
        ok("200 valid addresses plus self-match", mauvais == 0, f"({mauvais} anomalies)")
        ok("200 altered addresses -> refused", faux_positifs == 0, f"({faux_positifs} false positives)")

        if verbose:
            print("(4) the HRP is read from the address, not assumed")
        pub = _pub()
        a_d, a_c = address_from_pubkey(pub, hrp="dendra"), address_from_pubkey(pub, hrp="cosmos")
        ok("same key, 2 HRPs -> 2 distinct addresses", a_d != a_c)
        ok("the key matches its dendra1... address", pubkey_matches_address(pub, a_d))
        ok("the key matches its cosmos1... address", pubkey_matches_address(pub, a_c))
    except ImportError:
        nm("derivation over real keys", "`cryptography` is absent from THIS environment")
        nm("HRP read from the address", "`cryptography` is absent from THIS environment")

    # ── (5) malformed input: a NAMED refusal, never an approximate result ────────────────────────
    if verbose:
        print("(5) malformed input")
    for name, algo, arg in [
        ("32-byte secp256k1", "secp256k1", b"\x02" * 32),
        ("uncompressed secp256k1 (0x04)", "secp256k1", b"\x04" + b"\x01" * 32),
        ("31-byte ed25519", "ed25519", b"\x00" * 31),
        ("unknown algorithm", "rsa", b"\x02" * 33),
    ]:
        try:
            address_from_pubkey(arg, hrp="dendra", algo=algo)
            ok(name + " -> ValueError", False, "(accepted)")
        except ValueError as e:
            ok(name + " -> ValueError", len(str(e)) > 10, "(empty message)")
    ok("unreadable address -> False, no exception",
       pubkey_matches_address(b"\x02" * 33, "not-an-address") is False)
    ok("malformed key -> False, no exception",
       pubkey_matches_address(b"\x02" * 5, segwit[0][3]) is False)

    # ── (6) DENDRA ANCHOR: two accounts recorded from a live chain ───────────────────────────────
    # What the ground-truth check below can only prove when the chain answers, this one proves
    # always. These two pairs were recorded from the network (`query auth accounts`, public data).
    # The key-to-address binding is a MATHEMATICAL fact, not a chain state: it stays true after a
    # fresh genesis, after a purge, after a validator set change. It is the only constant transcribed
    # into this file, and it is verifiable in one line.
    if verbose:
        print("(6) Dendra anchor (accounts recorded from a live chain)")
    for addr, key_b64 in [
        ("dendra13gaa2dd2hv4xhphfy6nc4pnyztmf2etuxl9ntm",
         "AnI/XUJjCk29quNkQryC53xkYGq2OEd3oqJB+ccMl7tS"),
        ("dendra1qjr85xgm4ceuksaf7wjnadlracsz609s25h0ss",
         "Al+YF9PgkaXXrs9KnR7fvUnmkW8B5CpMyOibzJlnSKdl"),
    ]:
        raw = base64.b64decode(key_b64)
        ok(f"{addr[:26]}... -> ripemd160(sha256(pk))",
           address_from_pubkey(raw, hrp="dendra") == addr and pubkey_matches_address(raw, addr))

    # ── (7) GROUND TRUTH: the chain binary, or nothing ───────────────────────────────────────────
    if verbose:
        print("(7) ground truth (`dendrad`)")
    path_ = os.environ.get("DENDRA_VERITE", "")
    if not path_:
        nm("derivation matches `dendrad`", "DENDRA_VERITE was not provided")
    elif not os.path.exists(path_):
        ok("ground-truth file readable", False, f"({path_} not found)")
    else:
        try:
            with open(path_, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            ambiguous = []
            paires, vus = _harvest(raw, [], ambiguous), set()
            if ambiguous and verbose:
                print(f"  note: {len(ambiguous)} object(s) discarded, several key fields present and "
                      f"pairing not guaranteed ({', '.join(ambiguous[:3])})")
            n = 0
            for addr, algo, cle in paires:
                if (addr, cle) in vus:
                    continue
                vus.add((addr, cle))
                try:
                    # The HRP is everything before the LAST separator: `1` is absent from the bech32
                    # alphabet, but an HRP may contain it, so `rfind` and never `index`.
                    calc = address_from_pubkey(cle, hrp=addr[:addr.rfind("1")], algo=algo)
                    ok(f"{addr} ({algo})", calc == addr, f"\n      computed {calc}")
                except ValueError as e:
                    # A declared type disagreeing with the key length is a fact, not a detail.
                    ok(f"{addr} ({algo})", False, f"\n      key/type inconsistency: {e}")
                n += 1
            if n == 0:
                ok("ground truth usable", False,
                   "(0 key/address pair; an account that never signed publishes no key)")
            elif verbose:
                print(f"  -> {n} real account(s) confronted with the derivation")
        except Exception as e:  # noqa: BLE001
            ok("ground-truth read", False, f"({type(e).__name__}: {e})")

    return greens, reds, not_measured


def test_cosmos_addr():
    """pytest entry point. A NOT MEASURED result does not fail: it is not a red."""
    _, reds, _ = run_suite(verbose=False)
    assert reds == 0, f"{reds} check(s) red — run this file directly for the detail"


if __name__ == "__main__":
    v, r, nmeas = run_suite()
    print()
    print(f"SUMMARY_COSMOS_ADDR green={v} red={r} not_measured={nmeas}")
    sys.exit(1 if r else 0)
