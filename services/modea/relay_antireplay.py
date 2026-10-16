"""Replay protection for a signed write to the relay - and freshness, which is a different property.

=== Two properties, not one =====================================================================
"Height-bounded replay protection" folds two distinct guarantees into one phrase. Kept apart, they
do not ask for the same things:

| property      | what it prevents                                 | does it need the chain?        |
|---------------|--------------------------------------------------|--------------------------------|
| REPLAY    | a captured message being re-submitted        | no - monotonicity suffices |
| Freshness | a legitimate but stale write getting through | yes - the head is required |

The relay keeps, per writing key, the highest accepted height (`high-water`) and the exact digest
of recent messages. A replay necessarily carries a height <= high-water and an already-seen
digest: it is refused without querying any chain. And an attacker cannot forge a future height -
it cannot sign.

    NOMINAL  = high-water + digest + |height - head| <= window
    DEGRADED = high-water + digest             (only the window drops out)

Never fail closed on the head being unavailable. A halted chain must not destroy audit evidence.
In degraded mode the write is accepted, but not silently:

The regime is returned with every verdict, and the caller must log it. Without it, a post-mortem
cannot tell an entry validated in full from an entry validated blind - *a guard that does not say
which regime it ran in produces a verdict nobody can read back.* This is the same requirement as the
NEVER_READ / fees / stale triple of the registry cache.

Acceptance at `>=`, not at `>`. A single writer may legitimately produce **several messages at the
same height** (several reveals inside one block). Requiring `>` would refuse all but the first. It is
therefore the digest that discriminates at equal height, and the high-water only forbids going
backwards.

This module verifies no signature and reads no registry. It answers "have I seen this before, and is
it recent enough". The rest - valid signature, `address(pubkey) == operator` - is the caller's work,
and neither half is sufficient on its own.

=== Where the height comes from, and why that is not a detail ===================================
the high-water is anchored on the height carried by the signed note. Never on a sequence number.
A sequence number is an account counter: it says nothing about chain time, and **the writer
controls it**. Anchoring monotonicity on it would give a guard that a writer can advance or freeze at
will - which is to say, not a guard.

The rule is not written here alone: `verify()` refuses a bare height. It requires a
`SealedHeight`, which only `seal_height()` produces, and which is read **from the signed
bytes**. An `int` - or the string `"160"` a binary returns for a sequence - is refused under a named
reason, never silently converted.

Honesty about what this type does: `SealedHeight(42)` is still constructible by hand. The type does
not make the mistake impossible, it makes it visible - a sequence can no longer be wired in
absent-mindedly, only by writing down in plain sight that one is sealing a value that did not come
from a message. A type guard is not a cryptographic guard, and confusing the two would be exactly the
over-claiming this project refuses elsewhere.
"""
from __future__ import annotations

NOMINAL = "NOMINAL"
DEGRADED = "DEGRADED"

# refusal reasons - named, because a silent refusal cannot be debugged.
# The value travels: it is the head of the relay's 401 `why` and a key of /stats `signature.refusals`.
# An older relay sends French values (`REJEU` for REPLAY); a reader that must work
# against both relays names the old spelling itself, as `miner_selftest.py` does for REPLAY.
REPLAY = "REPLAY"                         # digest already seen
HEIGHT_REGRESSION = "HEIGHT_REGRESSION"   # height below this key's high-water
TOO_OLD = "TOO_OLD"                       # outside the window, on the past side
TOO_FAR_AHEAD = "TOO_FAR_AHEAD"           # outside the window, on the future side
MALFORMED = "MALFORMED"
UNSEALED_HEIGHT = "UNSEALED_HEIGHT"       # bare height: provenance not proven


class SealedHeight(int):
    """A height whose provenance is named: it was read back from the signed bytes.

    The type carries no more data than an `int` - it carries an origin. That is all that is
    needed: `verify()` can then refuse anything that did not go through `seal_height()`, and an
    account sequence can no longer reach the high-water by mere resemblance.
    """

    __slots__ = ()

    def __repr__(self):
        return f"SealedHeight({int(self)})"


def seal_height(message: bytes, read_height) -> SealedHeight:
    """Reads the height back from the signed message and marks it as such.

    `read_height(message) -> int` is injected (it is `relay_canon.message_height`): this module
    does not know the shape of the message, and has no business knowing it.

    Call only after signature verification. A height read back from a message nobody authenticated is
    no more trustworthy than a height supplied by the caller; it merely looks like it is. Sealing
    does not create trust, it carries it from the signature - and it carries nothing if there is no
    signature upstream.
    """
    if not callable(read_height):
        raise ValueError("read_height must be callable")
    if not isinstance(message, (bytes, bytearray)):
        raise ValueError(f"message: bytes expected, got {type(message).__name__}")
    h = read_height(bytes(message))
    if isinstance(h, bool) or not isinstance(h, int):
        raise ValueError(f"height read back is not an integer: {type(h).__name__} - non-canonical message?")
    if h < 0 or h > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"height read back is out of U64 bounds: {h}")
    return SealedHeight(h)


class Verdict:
    __slots__ = ("accepted", "regime", "reason", "detail")

    def __init__(self, accepted, regime, reason=None, detail=""):
        self.accepted = accepted
        self.regime = regime
        self.reason = reason
        self.detail = detail

    def __repr__(self):
        return (f"Verdict({'ACCEPTED' if self.accepted else 'REFUSED'} regime={self.regime}"
                f"{'' if self.reason is None else ' reason=' + self.reason})")


class Antireplay:
    """The clock is injected, so this module can be exercised without waiting for time to pass.

    `retention` must equal the relay store's own retention. It is not a free setting: a digest
    forgotten before its entry is evicted reopens exactly the replay window this class claims to
    close.
    """

    def __init__(self, clock, retention: float, window: int = 200):
        if not callable(clock):
            raise ValueError("clock must be callable")
        if retention <= 0:
            raise ValueError(f"retention must be > 0 (got {retention})")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            raise ValueError(f"window must be an integer > 0 (got {window!r})")
        self._clock = clock
        self.retention = float(retention)
        self.window = window
        self._high: dict[bytes, int] = {}       # writing key -> highest accepted height
        self._seen: dict[bytes, float] = {}     # digest -> acceptance time
        self.accepted_nominal = 0
        self.accepted_degraded = 0
        self.refusals: dict[str, int] = {}

    # -- purge ------------------------------------------------------------------------------------------------
    def _purge(self):
        """Forgets digests older than the retention. The high-water is never forgotten: it costs one
        integer per key, and forgetting it would authorize going backwards.

        The scan stops at the first still-young entry; it does not sweep the whole dictionary.
        Digests are inserted in time order and a Python `dict` preserves insertion order, so the
        oldest sit at the front. Cost is O(what expires), not O(everything). This is exactly the
        shape already used by the relay's `_put()` (`relay.py`), read from the source rather than
        reinvented. A version that sweeps everything on every call measures at ~171 us per
        verification and grows with occupancy, for work that is almost always nil - and a guard whose
        cost grows with traffic eventually becomes the reason someone removes it.
        """
        cutoff = float(self._clock()) - self.retention
        v = self._seen
        while v:
            e = next(iter(v))
            if v[e] >= cutoff:
                break
            del v[e]

    def _refuse(self, regime, reason, detail=""):
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return Verdict(False, regime, reason, detail)

    # -- the verdict --------------------------------------------------------------------------------------------
    def verify(self, writing_key: bytes, height: SealedHeight, digest: bytes,
                 head=None) -> Verdict:
        """`head` is the known chain height, or None when the cache does not know (=> DEGRADED).

        `height` must be a `SealedHeight` - see the module header: the high-water is anchored on
        the height carried by the signed note, never on an account sequence.

        The write is recorded only if it is accepted: a refusal must not consume the high-water,
        otherwise one out-of-window message would block every message after it.
        """
        regime = NOMINAL if head is not None else DEGRADED
        if not isinstance(writing_key, (bytes, bytearray)) or not writing_key:
            return self._refuse(regime, MALFORMED, "writing key empty or not bytes")
        if not isinstance(digest, (bytes, bytearray)) or len(digest) != 32:
            return self._refuse(regime, MALFORMED, f"digest of {len(digest or b'')} bytes, 32 expected")
        # Provenance before value: a bare `int` - or the string `"160"` of a sequence - is refused
        #    under a named reason. Nothing is converted, nothing is guessed.
        if not isinstance(height, SealedHeight):
            return self._refuse(
                regime, UNSEALED_HEIGHT,
                f"bare height ({type(height).__name__} {height!r:.20}): provenance not proven. "
                f"The high-water is anchored on the height carried by the signed note - an account "
                f"sequence resembles it and has no right to enter. Go through seal_height().")
        if height < 0:      # defence in depth: the type is constructible by hand
            return self._refuse(regime, MALFORMED, f"negative sealed height: {height!r}")

        self._purge()
        key = bytes(writing_key)
        dig = bytes(digest)

        # 1. Has this exact digest already been accepted? The exact discriminant, no clock involved.
        if dig in self._seen:
            return self._refuse(regime, REPLAY, "digest already accepted")

        # 2. Per-key monotonicity, at `>=` and not `>`: several messages at the same height are
        #    legitimate (several reveals in one block) and the digest already tells them apart.
        h = self._high.get(key)
        if h is not None and height < h:
            return self._refuse(regime, HEIGHT_REGRESSION, f"height {height} < high-water {h}")

        # 3. Freshness - only when the head is known. Its absence is not a refusal.
        if head is not None:
            if not isinstance(head, int) or isinstance(head, bool) or head < 0:
                return self._refuse(NOMINAL, MALFORMED, f"invalid head: {head!r}")
            if height < head - self.window:
                return self._refuse(NOMINAL, TOO_OLD, f"height {height} < head {head} - {self.window}")
            if height > head + self.window:
                return self._refuse(NOMINAL, TOO_FAR_AHEAD, f"height {height} > head {head} + {self.window}")

        # accepted: record it, and say in which regime
        self._seen[dig] = float(self._clock())
        if h is None or height > h:
            # `int(...)`: the internal state keeps only a number. The provenance marker is
            # meaningful at the entry point; handing it back out of internal state would make it
            # claim something it no longer proves.
            self._high[key] = int(height)
        if regime == NOMINAL:
            self.accepted_nominal += 1
        else:
            self.accepted_degraded += 1
        return Verdict(True, regime)

    # -- observability ---------------------------------------------------------------------------------------
    def summary(self) -> str:
        r = " ".join(f"{k}={v}" for k, v in sorted(self.refusals.items())) or "none"
        return (f"ANTIREPLAY nominal={self.accepted_nominal} degraded={self.accepted_degraded} "
                f"writers={len(self._high)} digests={len(self._seen)} refusals[{r}]")
