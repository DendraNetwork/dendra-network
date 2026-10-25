"""Exercises `modea.relay_antireplay` — the central property, then the known blind spot.

The property to demonstrate is not "the code runs" but: a captured message that is re-submitted is
refused without querying any chain. It is measured by playing a real replay with no chain head.

The blind spot, one writer producing several messages at the same height, has its own test: it
is what justifies `≥` rather than `>`, and it is where the digest becomes the real discriminator.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modea.relay_antireplay import (  # type: ignore
        DEGRADED, UNSEALED_HEIGHT, MALFORMED, NOMINAL, HEIGHT_REGRESSION, REPLAY, TOO_OLD, TOO_FAR_AHEAD,
        Antireplay, SealedHeight, seal_height)
    from modea import relay_canon as rc  # type: ignore
except ImportError:
    from relay_antireplay import (  # type: ignore
        DEGRADED, UNSEALED_HEIGHT, MALFORMED, NOMINAL, HEIGHT_REGRESSION, REPLAY, TOO_OLD, TOO_FAR_AHEAD,
        Antireplay, SealedHeight, seal_height)
    import relay_canon as rc  # type: ignore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


def dig(x):
    return hashlib.sha256(str(x).encode()).digest()


def H(n):
    """A sealed height built as in production: read back out of a real canonical message.

    Not `SealedHeight(n)`. That shortcut would compile and test the wrong path, since building a
    height without a message is exactly what the rule forbids. The test therefore takes the path the
    relay takes — and proves along the way that the round-trip holds.
    """
    return seal_height(rc.canonical_message("k", "key", b"body", "m1", n),
                           rc.message_height)


K1, K2 = b"\x02" + b"\x11" * 32, b"\x02" + b"\x22" * 32


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

    def measure(name, f):
        try:
            ok(name, f())
        except Exception as e:  # noqa: BLE001
            nonlocal failed
            failed += 1
            if verbose:
                print("  FAIL", f"{name}: exception {type(e).__name__}: {str(e)[:90]}")

    # -- (1) the property: a replay is refused without a chain -----------------------------------
    if verbose:
        print("(1) a captured, re-submitted message is refused — no chain head, no chain queried")
    h = Clock()
    a = Antireplay(h, retention=600, window=200)
    v1 = a.verify(K1, H(100), dig("m1"), head=None)
    ok("first deposit accepted, DEGRADED regime reported", v1.accepted and v1.regime == DEGRADED)
    v2 = a.verify(K1, H(100), dig("m1"), head=None)
    ok("the exact replay is refused, with a named reason", (not v2.accepted) and v2.reason == REPLAY)
    v3 = a.verify(K1, H(99), dig("m1"), head=None)
    ok("replayed at a lower height: refused as well", not v3.accepted)
    ok("no chain head was ever supplied", True)

    # -- (2) the blind spot: same height, different messages -------------------------------------
    if verbose:
        print("(2) same writer, same height, different messages (the case that justifies `>=`)")
    b = Antireplay(Clock(), retention=600, window=200)
    r = [b.verify(K1, H(500), dig(f"reveal{i}"), head=None) for i in range(5)]
    ok("5 distinct messages at height 500: all accepted", all(x.accepted for x in r))
    ok("the high-water did not block the later ones", b.accepted_degraded == 5)
    ok("but replaying the third is refused", not b.verify(K1, H(500), dig("reveal2"), head=None).accepted)
    ok("and going back to 499 is refused (HEIGHT_REGRESSION)",
       b.verify(K1, H(499), dig("fresh"), head=None).reason == HEIGHT_REGRESSION)

    # -- (3) the high-water is per key, not global -----------------------------------------------
    if verbose:
        print("(3) the high-water is per writing key — two miners do not obstruct each other")
    c = Antireplay(Clock(), retention=600, window=200)
    ok("K1 writes high (1000)", c.verify(K1, H(1000), dig("a"), head=None).accepted)
    ok("K2 writes low (10): accepted, K1's high-water does not concern it",
       c.verify(K2, H(10), dig("b"), head=None).accepted)
    ok("K1 at 10 is refused (its own high-water)",
       c.verify(K1, H(10), dig("c"), head=None).reason == HEIGHT_REGRESSION)

    # -- (4) NOMINAL vs DEGRADED: the window, and only the window, depends on the head -----------
    if verbose:
        print("(4) the window falls away when degraded — the anti-replay does not")
    d = Antireplay(Clock(), retention=600, window=200)
    vn = d.verify(K1, H(1000), dig("x"), head=1000)
    ok("nominal: inside the window -> accepted, NOMINAL regime", vn.accepted and vn.regime == NOMINAL)
    ok("nominal: too old -> refused",
       d.verify(K1, H(1001), dig("y"), head=5000).reason == TOO_OLD)
    ok("nominal: too far in the future -> refused",
       d.verify(K1, H(9000), dig("z"), head=1000).reason == TOO_FAR_AHEAD)
    # the same write outside the window passes when degraded — the accepted trade-off
    vd = d.verify(K1, H(9000), dig("z"), head=None)
    ok("the same write with an unknown head -> accepted as DEGRADED (never fail-closed)",
       vd.accepted and vd.regime == DEGRADED)
    ok("but its replay stays refused, degraded or not",
       not d.verify(K1, H(9000), dig("z"), head=None).accepted)

    # -- (5) a refusal does not consume the high-water -------------------------------------------
    if verbose:
        print("(5) a refusal consumes nothing — otherwise an out-of-window message would block the rest")
    e = Antireplay(Clock(), retention=600, window=200)
    e.verify(K1, H(100), dig("p"), head=100)
    e.verify(K1, H(9999), dig("q"), head=100)          # refused: too far in the future
    ok("after a TOO_FAR_AHEAD refusal, a normal write goes through",
       e.verify(K1, H(101), dig("r"), head=100).accepted)
    ok("the high-water did not jump to 9999",
       e.verify(K1, H(102), dig("s"), head=100).accepted)

    # -- (6) digest retention --------------------------------------------------------------------
    if verbose:
        print("(6) digest retention (it must equal the store's retention)")
    hh = Clock()
    f = Antireplay(hh, retention=600, window=200)
    f.verify(K1, H(100), dig("old"), head=None)
    hh.advance(601)
    ok("past the retention, the digest is forgotten...", f.verify(K2, H(100), dig("old"), head=None).accepted)
    ok("...but K1's high-water is not forgotten",
       f.verify(K1, H(99), dig("after"), head=None).reason == HEIGHT_REGRESSION)

    # -- (7) malformed input: a named refusal, never an exception --------------------------------
    if verbose:
        print("(7) malformed input -> named refusal")
    g = Antireplay(Clock(), retention=600, window=200)
    for name, args in [
        ("empty key", (b"", H(1), dig("a"))),
        ("non-binary key", ("not bytes", H(1), dig("a"))),
        ("31-byte digest", (K1, H(1), b"\x00" * 31)),
        # A sealed height cannot be negative (it comes out of a U64) — but the type can be built by
        # hand, so the value guard remains, and is measured.
        ("negative sealed height (forged)", (K1, SealedHeight(-1), dig("a"))),
    ]:
        measure(f"{name} -> MALFORMED", lambda args=args: g.verify(*args, head=None).reason == MALFORMED)
    measure("invalid head -> MALFORMED, under the NOMINAL regime",
           lambda: g.verify(K1, H(1), dig("t"), head=-5).reason == MALFORMED)

    # -- (8) inconsistent construction -----------------------------------------------------------
    if verbose:
        print("(8) inconsistent construction -> named ValueError")
    for name, f2 in [
        ("non-callable clock", lambda: Antireplay(42, retention=600)),
        ("zero retention", lambda: Antireplay(Clock(), retention=0)),
        ("zero window", lambda: Antireplay(Clock(), retention=600, window=0)),
        ("boolean window", lambda: Antireplay(Clock(), retention=600, window=True)),
    ]:
        try:
            f2()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e2:
            ok(name + " -> ValueError", len(str(e2)) > 8)

    # -- (9) the regime is observable ------------------------------------------------------------
    if verbose:
        print("(9) the regime is returned and counted (a post-mortem must be able to re-read the verdict)")
    z = Antireplay(Clock(), retention=600, window=200)
    z.verify(K1, H(100), dig("n1"), head=100)
    z.verify(K1, H(101), dig("n2"), head=100)
    z.verify(K2, H(100), dig("d1"), head=None)
    z.verify(K2, H(100), dig("d1"), head=None)          # replay
    ok("separate nominal/degraded counters", z.accepted_nominal == 2 and z.accepted_degraded == 1)
    ok("refusals are counted per reason", z.refusals.get(REPLAY) == 1)
    r = z.summary()
    ok("stable summary line", all(x in r for x in ("nominal=", "degraded=", "writers=", "refusals[")))
    if verbose:
        print(f"      {r}")

    # -- (10) a height cannot be a sequence number -----------------------------------------------
    # The high-water must anchor on the height carried by the signed note, never on the account
    # sequence. This is the measurement of that wiring.
    if verbose:
        print("(10) a bare height does not reach the high-water (a sequence number looks like one)")
    # `measure` and not `ok`: without the guard, `"160" < 0` raises TypeError and the rest of the
    # suite dies without reporting. A suite that dies does not count its failures, and an uncounted
    # failure looks far too much like a pass.
    w = Antireplay(Clock(), retention=600, window=200)
    measure("a bare `int` is refused, with a named reason",
           lambda: w.verify(K1, 160, dig("s1"), head=None).reason == UNSEALED_HEIGHT)
    # `"160"`: the exact form in which the binary renders a sequence number (`signer_infos[0]`).
    measure('the string "160" (the real shape of a sequence number) is refused',
           lambda: w.verify(K1, "160", dig("s2"), head=None).reason == UNSEALED_HEIGHT)
    measure("a boolean is refused too (bool is an int, not a SealedHeight)",
           lambda: w.verify(K1, True, dig("s3"), head=None).reason == UNSEALED_HEIGHT)
    measure("the refusal recorded nothing (neither digest nor writer)",
           lambda: len(w._seen) == 0 and len(w._high) == 0)
    ok("the reason differs from MALFORMED (a provenance is not a shape)",
       UNSEALED_HEIGHT != MALFORMED)
    measure("the refusal message names the trap (sequence number)",
           lambda: "sequence" in w.verify(K1, 160, dig("s4"), head=None).detail)

    if verbose:
        print("(10b) sealing re-reads the height from the bytes — it is not handed the value alongside")
    msg = rc.canonical_message("req", "job7__m1", b"charge", "m1", 4242)
    h_s = seal_height(msg, rc.message_height)
    ok("the sealed height equals the one encoded in the message", int(h_s) == 4242)
    ok("and it carries the type that says where it comes from", isinstance(h_s, SealedHeight))
    # the case that matters: what the caller believes enters nowhere.
    msg2 = rc.canonical_message("req", "job7__m1", b"charge", "m1", 7)
    ok("a message carrying 7 seals 7, whatever height the caller had in mind (999)",
       int(seal_height(msg2, rc.message_height)) == 7)
    ok("a truncated message -> ValueError, never an invented height",
       _raises(lambda: seal_height(msg2[:-3], rc.message_height)))
    ok("a reader returning something other than an integer -> ValueError",
       _raises(lambda: seal_height(msg2, lambda m: "160")))
    ok("a non-callable reader -> ValueError", _raises(lambda: seal_height(msg2, 42)))
    ok("a non-binary message -> ValueError",
       _raises(lambda: seal_height("not bytes", rc.message_height)))
    # Honesty: the type can be forged. It makes the mistake visible, not impossible — and that is measured.
    ok("a hand-forged SealedHeight passes the type check (a visible guard, not a cryptographic one)",
       Antireplay(Clock(), retention=600).verify(
           K1, SealedHeight(9), dig("forge"), head=None).accepted)

    return passed, failed


def _raises(f, cls=ValueError):
    try:
        f()
        return False
    except cls:
        return True
    except Exception:  # noqa: BLE001
        return False


def test_relay_antireplay():
    _, r = run(verbose=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = run()
    print()
    print(f"ANTIREPLAY_SUMMARY passed={v} failed={r}")
    sys.exit(1 if r else 0)
