"""Exercises `modea.relay_antireplay` — the central property, then the known blind spot.

The property to demonstrate is not "the code runs" but: a captured message that is re-submitted is
REFUSED WITHOUT querying any chain. It is measured by playing a real replay with no chain head.

⚠️ The blind spot — one writer producing several messages at the SAME height — has its own test: it
is what justifies `≥` rather than `>`, and it is where the digest becomes the real discriminator.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modea.relay_antireplay import (  # type: ignore
        DEGRADE, UNSEALED_HEIGHT, MAL_FORME, NOMINAL, RECUL, REJEU, TROP_ANCIEN, TROP_FUTUR,
        Antireplay, HauteurScellee, seal_height)
    from modea import relay_canon as rc  # type: ignore
except ImportError:
    from relay_antireplay import (  # type: ignore
        DEGRADE, UNSEALED_HEIGHT, MAL_FORME, NOMINAL, RECUL, REJEU, TROP_ANCIEN, TROP_FUTUR,
        Antireplay, HauteurScellee, seal_height)
    import relay_canon as rc  # type: ignore


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avance(self, d):
        self.t += d


def emp(x):
    return hashlib.sha256(str(x).encode()).digest()


def H(n):
    """A sealed height BUILT AS IN PRODUCTION: read back out of a real canonical message.

    ⚠️ Not `HauteurScellee(n)`. That shortcut would compile and test the wrong path, since building a
    height without a message is exactly what the rule forbids. The test therefore takes the path the
    relay takes — and proves along the way that the round-trip holds.
    """
    return seal_height(rc.canonical_message("k", "key", b"body", "m1", n),
                           rc.message_height)


K1, K2 = b"\x02" + b"\x11" * 32, b"\x02" + b"\x22" * 32


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

    def mesure(name, f):
        try:
            ok(name, f())
        except Exception as e:  # noqa: BLE001
            nonlocal rouges
            rouges += 1
            if verbeux:
                print(f"  ✗ {name} — EXCEPTION {type(e).__name__}: {str(e)[:90]}")

    # ── (1) THE property: a replay is refused WITHOUT a chain ───────────────────────────────────
    if verbeux:
        print("(1) a captured, re-submitted message is refused — no chain head, no chain queried")
    h = Horloge()
    a = Antireplay(h, retention=600, fenetre=200)
    v1 = a.verifier(K1, H(100), emp("m1"), tete=None)
    ok("first deposit accepted, DEGRADED regime reported", v1.accepte and v1.regime == DEGRADE)
    v2 = a.verifier(K1, H(100), emp("m1"), tete=None)
    ok("the exact REPLAY is REFUSED, with a named reason", (not v2.accepte) and v2.motif == REJEU)
    v3 = a.verifier(K1, H(99), emp("m1"), tete=None)
    ok("replayed at a lower height: refused as well", not v3.accepte)
    ok("no chain head was ever supplied", True)

    # ── (2) the blind spot: SAME height, DIFFERENT messages ─────────────────────────────────────
    if verbeux:
        print("(2) same writer, SAME height, different messages (the case that justifies `>=`)")
    b = Antireplay(Horloge(), retention=600, fenetre=200)
    r = [b.verifier(K1, H(500), emp(f"reveal{i}"), tete=None) for i in range(5)]
    ok("5 distinct messages at height 500: ALL accepted", all(x.accepte for x in r))
    ok("the high-water did not block the later ones", b.acceptes_degrade == 5)
    ok("but replaying the third is refused", not b.verifier(K1, H(500), emp("reveal2"), tete=None).accepte)
    ok("and going back to 499 is refused (RECUL)",
       b.verifier(K1, H(499), emp("neuf"), tete=None).motif == RECUL)

    # ── (3) the high-water is PER KEY, not global ───────────────────────────────────────────────
    if verbeux:
        print("(3) the high-water is per writing key — two miners do not obstruct each other")
    c = Antireplay(Horloge(), retention=600, fenetre=200)
    ok("K1 writes high (1000)", c.verifier(K1, H(1000), emp("a"), tete=None).accepte)
    ok("K2 writes low (10): ACCEPTED, K1's high-water does not concern it",
       c.verifier(K2, H(10), emp("b"), tete=None).accepte)
    ok("K1 at 10 is refused (its own high-water)",
       c.verifier(K1, H(10), emp("c"), tete=None).motif == RECUL)

    # ── (4) NOMINAL vs DEGRADED: the window, and only the window, depends on the head ───────────
    if verbeux:
        print("(4) the window falls away when degraded — the anti-replay does not")
    d = Antireplay(Horloge(), retention=600, fenetre=200)
    vn = d.verifier(K1, H(1000), emp("x"), tete=1000)
    ok("nominal: inside the window -> accepted, NOMINAL regime", vn.accepte and vn.regime == NOMINAL)
    ok("nominal: too old -> refused",
       d.verifier(K1, H(1001), emp("y"), tete=5000).motif == TROP_ANCIEN)
    ok("nominal: too far in the future -> refused",
       d.verifier(K1, H(9000), emp("z"), tete=1000).motif == TROP_FUTUR)
    # the SAME write outside the window passes when degraded — the accepted trade-off
    vd = d.verifier(K1, H(9000), emp("z"), tete=None)
    ok("the same write with an unknown head -> ACCEPTED as DEGRADED (never fail-closed)",
       vd.accepte and vd.regime == DEGRADE)
    ok("but its replay stays refused, degraded or not",
       not d.verifier(K1, H(9000), emp("z"), tete=None).accepte)

    # ── (5) a refusal does not consume the high-water ───────────────────────────────────────────
    if verbeux:
        print("(5) a refusal consumes nothing — otherwise an out-of-window message would block the rest")
    e = Antireplay(Horloge(), retention=600, fenetre=200)
    e.verifier(K1, H(100), emp("p"), tete=100)
    e.verifier(K1, H(9999), emp("q"), tete=100)          # refused: too far in the future
    ok("after a TROP_FUTUR refusal, a normal write goes through",
       e.verifier(K1, H(101), emp("r"), tete=100).accepte)
    ok("the high-water did not jump to 9999",
       e.verifier(K1, H(102), emp("s"), tete=100).accepte)

    # ── (6) digest retention ────────────────────────────────────────────────────────────────────
    if verbeux:
        print("(6) digest retention (it MUST equal the store's retention)")
    hh = Horloge()
    f = Antireplay(hh, retention=600, fenetre=200)
    f.verifier(K1, H(100), emp("vieux"), tete=None)
    hh.avance(601)
    ok("past the retention, the digest is forgotten...", f.verifier(K2, H(100), emp("vieux"), tete=None).accepte)
    ok("...but K1's high-water is NOT forgotten",
       f.verifier(K1, H(99), emp("apres"), tete=None).motif == RECUL)

    # ── (7) malformed input: a NAMED refusal, never an exception ────────────────────────────────
    if verbeux:
        print("(7) malformed input -> NAMED refusal")
    g = Antireplay(Horloge(), retention=600, fenetre=200)
    for name, args in [
        ("empty key", (b"", H(1), emp("a"))),
        ("non-binary key", ("pas des octets", H(1), emp("a"))),
        ("31-byte digest", (K1, H(1), b"\x00" * 31)),
        # ⚠️ A sealed height CANNOT be negative (it comes out of a U64) — but the type can be built by
        # hand, so the value guard remains, and is measured.
        ("negative sealed height (forged)", (K1, HauteurScellee(-1), emp("a"))),
    ]:
        mesure(f"{name} -> MAL_FORME", lambda args=args: g.verifier(*args, tete=None).motif == MAL_FORME)
    mesure("invalid head -> MAL_FORME, under the NOMINAL regime",
           lambda: g.verifier(K1, H(1), emp("t"), tete=-5).motif == MAL_FORME)

    # ── (8) inconsistent construction ───────────────────────────────────────────────────────────
    if verbeux:
        print("(8) inconsistent construction -> named ValueError")
    for name, f2 in [
        ("non-callable clock", lambda: Antireplay(42, retention=600)),
        ("zero retention", lambda: Antireplay(Horloge(), retention=0)),
        ("zero window", lambda: Antireplay(Horloge(), retention=600, fenetre=0)),
        ("boolean window", lambda: Antireplay(Horloge(), retention=600, fenetre=True)),
    ]:
        try:
            f2()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e2:
            ok(name + " -> ValueError", len(str(e2)) > 8)

    # ── (9) the regime is OBSERVABLE ────────────────────────────────────────────────────────────
    if verbeux:
        print("(9) the regime is returned AND counted (a post-mortem must be able to re-read the verdict)")
    z = Antireplay(Horloge(), retention=600, fenetre=200)
    z.verifier(K1, H(100), emp("n1"), tete=100)
    z.verifier(K1, H(101), emp("n2"), tete=100)
    z.verifier(K2, H(100), emp("d1"), tete=None)
    z.verifier(K2, H(100), emp("d1"), tete=None)          # replay
    ok("separate nominal/degraded counters", z.acceptes_nominal == 2 and z.acceptes_degrade == 1)
    ok("refusals are counted PER REASON", z.refus.get(REJEU) == 1)
    r = z.resume()
    ok("stable summary line", all(x in r for x in ("nominal=", "degrade=", "ecrivains=", "refus[")))
    if verbeux:
        print(f"      {r}")

    # ── (10) A HEIGHT CANNOT BE A SEQUENCE NUMBER ───────────────────────────────────────────────
    # The high-water must anchor on the HEIGHT CARRIED BY THE SIGNED NOTE, never on the account
    # sequence. This is the measurement of that wiring.
    if verbeux:
        print("(10) a BARE height does not reach the high-water (a sequence number looks like one)")
    # ⚠️ `mesure` and not `ok`: without the guard, `"160" < 0` raises TypeError and the rest of the
    # suite DIES without reporting. A suite that dies does not count its failures, and an uncounted
    # failure looks far too much like a pass.
    w = Antireplay(Horloge(), retention=600, fenetre=200)
    mesure("a bare `int` is REFUSED, with a named reason",
           lambda: w.verifier(K1, 160, emp("s1"), tete=None).motif == UNSEALED_HEIGHT)
    # `"160"`: the EXACT form in which the binary renders a sequence number (`signer_infos[0]`).
    mesure('the string "160" (the real shape of a sequence number) is REFUSED',
           lambda: w.verifier(K1, "160", emp("s2"), tete=None).motif == UNSEALED_HEIGHT)
    mesure("a boolean is refused too (bool is an int, not a HauteurScellee)",
           lambda: w.verifier(K1, True, emp("s3"), tete=None).motif == UNSEALED_HEIGHT)
    mesure("the refusal recorded NOTHING (neither digest nor writer)",
           lambda: len(w._vues) == 0 and len(w._high) == 0)
    ok("the reason differs from MAL_FORME (a provenance is not a shape)",
       UNSEALED_HEIGHT != MAL_FORME)
    mesure("the refusal message NAMES the trap (sequence number)",
           lambda: "sequence" in w.verifier(K1, 160, emp("s4"), tete=None).detail)

    if verbeux:
        print("(10b) sealing re-reads the height FROM the bytes — it is not handed the value alongside")
    msg = rc.canonical_message("req", "job7__m1", b"charge", "m1", 4242)
    h_s = seal_height(msg, rc.message_height)
    ok("the sealed height equals the one ENCODED in the message", int(h_s) == 4242)
    ok("and it carries the type that says where it comes from", isinstance(h_s, HauteurScellee))
    # ⭐ the case that matters: what the caller BELIEVES enters nowhere.
    msg2 = rc.canonical_message("req", "job7__m1", b"charge", "m1", 7)
    ok("⭐ a message carrying 7 seals 7 — even if the caller was talking about 999",
       int(seal_height(msg2, rc.message_height)) == 7)
    ok("a truncated message -> ValueError, never an invented height",
       _leve(lambda: seal_height(msg2[:-3], rc.message_height)))
    ok("a reader returning something other than an integer -> ValueError",
       _leve(lambda: seal_height(msg2, lambda m: "160")))
    ok("a non-callable reader -> ValueError", _leve(lambda: seal_height(msg2, 42)))
    ok("a non-binary message -> ValueError",
       _leve(lambda: seal_height("pas des octets", rc.message_height)))
    # Honesty: the type can be forged. It makes the mistake VISIBLE, not impossible — and that is measured.
    ok("a hand-forged HauteurScellee PASSES the type check (a visible guard, not a cryptographic one)",
       Antireplay(Horloge(), retention=600).verifier(
           K1, HauteurScellee(9), emp("forge"), tete=None).accepte)

    return verts, rouges


def _leve(f, cls=ValueError):
    try:
        f()
        return False
    except cls:
        return True
    except Exception:  # noqa: BLE001
        return False


def test_relay_antireplay():
    _, r = lancer(verbeux=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = lancer()
    print()
    print(f"RESUME_ANTIREPLAY verts={v} rouges={r}")
    sys.exit(1 if r else 0)
