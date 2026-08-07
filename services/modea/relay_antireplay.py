"""Replay protection for a signed write to the relay - and FRESHNESS, which is a different property.

=== TWO PROPERTIES, NOT ONE =====================================================================
"Height-bounded replay protection" folds two distinct guarantees into one phrase. Kept apart, they
do not ask for the same things:

| property      | what it prevents                                 | does it need the chain?        |
|---------------|--------------------------------------------------|--------------------------------|
| **REPLAY**    | a captured message being **re-submitted**        | **NO** - monotonicity suffices |
| **FRESHNESS** | a legitimate but **stale** write getting through | **yes** - the head is required |

The relay keeps, **per writing key**, the highest accepted height (`high-water`) and the exact digest
of recent messages. A replay necessarily carries a height **<= high-water** AND an **already-seen**
digest: it is refused without querying any chain. And an attacker **cannot forge a future height** -
it cannot sign.

    NOMINAL  = high-water + digest + |height - head| <= window
    DEGRADED = high-water + digest             (only the window drops out)

**NEVER FAIL CLOSED ON THE HEAD BEING UNAVAILABLE.** A halted chain must not destroy audit evidence.
In degraded mode the write is **accepted**, but not silently:

**THE REGIME IS RETURNED WITH EVERY VERDICT, and the caller MUST log it.** Without it, a post-mortem
cannot tell an entry validated in full from an entry validated blind - *a guard that does not say
which regime it ran in produces a verdict nobody can read back.* This is the same requirement as the
NEVER_READ / FEES / STALE triple of the registry cache.

ACCEPTANCE AT `>=`, NOT AT `>`. A single writer may legitimately produce **several messages at the
same height** (several reveals inside one block). Requiring `>` would refuse all but the first. It is
therefore the **digest** that discriminates at equal height, and the high-water only forbids going
backwards.

THIS MODULE VERIFIES NO SIGNATURE and reads no registry. It answers "have I seen this before, and is
it recent enough". The rest - valid signature, `address(pubkey) == operator` - is the caller's work,
and **neither half is sufficient on its own**.

=== WHERE THE HEIGHT COMES FROM, AND WHY THAT IS NOT A DETAIL ===================================
**THE HIGH-WATER IS ANCHORED ON THE HEIGHT CARRIED BY THE SIGNED NOTE. NEVER ON A SEQUENCE NUMBER.**
A sequence number is an **account counter**: it says nothing about chain time, and **the writer
controls it**. Anchoring monotonicity on it would give a guard that a writer can advance or freeze at
will - which is to say, not a guard.

The rule is not written here alone: `verifier()` **REFUSES a bare height**. It requires a
`HauteurScellee`, which only `seal_height()` produces, and which is read **FROM the signed
bytes**. An `int` - or the string `"160"` a binary returns for a sequence - is refused under a NAMED
reason, never silently converted.

HONESTY ABOUT WHAT THIS TYPE DOES: `HauteurScellee(42)` is still constructible by hand. The type does
not make the mistake IMPOSSIBLE, it makes it **VISIBLE** - a sequence can no longer be wired in
absent-mindedly, only by writing down in plain sight that one is sealing a value that did not come
from a message. A type guard is not a cryptographic guard, and confusing the two would be exactly the
over-claiming this project refuses elsewhere.
"""
from __future__ import annotations

NOMINAL = "NOMINAL"
DEGRADE = "DEGRADE"

# refusal reasons - named, because a silent refusal cannot be debugged
REJEU = "REJEU"                    # digest already seen
RECUL = "RECUL"                    # height below this key's high-water
TROP_ANCIEN = "TROP_ANCIEN"        # outside the window, on the past side
TROP_FUTUR = "TROP_FUTUR"          # outside the window, on the future side
MAL_FORME = "MAL_FORME"
UNSEALED_HEIGHT = "UNSEALED_HEIGHT"   # bare height: provenance not proven


class HauteurScellee(int):
    """A height whose PROVENANCE is named: it was read back from the signed bytes.

    The type carries no more data than an `int` - it carries an **origin**. That is all that is
    needed: `verifier()` can then refuse anything that did not go through `seal_height()`, and an
    account sequence can no longer reach the high-water by mere resemblance.
    """

    __slots__ = ()

    def __repr__(self):
        return f"HauteurScellee({int(self)})"


def seal_height(message: bytes, read_height) -> HauteurScellee:
    """Reads the height back FROM the signed message and marks it as such.

    `read_height(message) -> int` is injected (it is `relay_canon.message_height`): this module
    does not know the shape of the message, and has no business knowing it.

    CALL ONLY AFTER SIGNATURE VERIFICATION. A height read back from a message nobody authenticated is
    no more trustworthy than a height supplied by the caller; it merely looks like it is. Sealing
    does not create trust, it CARRIES it from the signature - and it carries nothing if there is no
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
    return HauteurScellee(h)


class Verdict:
    __slots__ = ("accepte", "regime", "motif", "detail")

    def __init__(self, accepte, regime, motif=None, detail=""):
        self.accepte = accepte
        self.regime = regime
        self.motif = motif
        self.detail = detail

    def __repr__(self):
        return (f"Verdict({'ACCEPTED' if self.accepte else 'REFUSED'} regime={self.regime}"
                f"{'' if self.motif is None else ' motif=' + self.motif})")


class Antireplay:
    """The clock is injected, so this module can be exercised without waiting for time to pass.

    `retention` MUST equal the relay store's own retention. It is not a free setting: a digest
    forgotten before its entry is evicted reopens exactly the replay window this class claims to
    close.
    """

    def __init__(self, horloge, retention: float, fenetre: int = 200):
        if not callable(horloge):
            raise ValueError("horloge must be callable")
        if retention <= 0:
            raise ValueError(f"retention must be > 0 (got {retention})")
        if not isinstance(fenetre, int) or isinstance(fenetre, bool) or fenetre <= 0:
            raise ValueError(f"fenetre must be an integer > 0 (got {fenetre!r})")
        self._horloge = horloge
        self.retention = float(retention)
        self.fenetre = fenetre
        self._high: dict[bytes, int] = {}       # writing key -> highest accepted height
        self._vues: dict[bytes, float] = {}     # digest -> acceptance time
        self.acceptes_nominal = 0
        self.acceptes_degrade = 0
        self.refus: dict[str, int] = {}

    # -- purge -----------------------------------------------------------------------------------─────────────
    def _purger(self):
        """Forgets digests older than the retention. The high-water is NEVER forgotten: it costs one
        integer per key, and forgetting it would authorize going backwards.

        THE SCAN STOPS AT THE FIRST STILL-YOUNG ENTRY; it does not sweep the whole dictionary.
        Digests are inserted in time order and a Python `dict` preserves insertion order, so the
        oldest sit at the front. Cost is O(what expires), not O(everything). This is exactly the
        shape already used by the relay's `_put()` (`relay.py`), read from the source rather than
        reinvented. A version that sweeps everything on every call measures at ~171 us per
        verification and grows with occupancy, for work that is almost always nil - and a guard whose
        cost grows with traffic eventually becomes the reason someone removes it.
        """
        limite = float(self._horloge()) - self.retention
        v = self._vues
        while v:
            e = next(iter(v))
            if v[e] >= limite:
                break
            del v[e]

    def _refuser(self, regime, motif, detail=""):
        self.refus[motif] = self.refus.get(motif, 0) + 1
        return Verdict(False, regime, motif, detail)

    # -- the verdict -----------------------------------------------------------------------------───────────────
    def verifier(self, writing_key: bytes, height: HauteurScellee, empreinte: bytes,
                 tete=None) -> Verdict:
        """`tete` is the known chain height, or None when the cache does not know (=> DEGRADED).

        `height` MUST be a `HauteurScellee` - see the module header: the high-water is anchored on
        the height carried by the signed note, never on an account sequence.

        The write is recorded ONLY if it is accepted: a refusal must not consume the high-water,
        otherwise one out-of-window message would block every message after it.
        """
        regime = NOMINAL if tete is not None else DEGRADE
        if not isinstance(writing_key, (bytes, bytearray)) or not writing_key:
            return self._refuser(regime, MAL_FORME, "writing key empty or not bytes")
        if not isinstance(empreinte, (bytes, bytearray)) or len(empreinte) != 32:
            return self._refuser(regime, MAL_FORME, f"digest of {len(empreinte or b'')} bytes, 32 expected")
        # Provenance BEFORE value: a bare `int` - or the string `"160"` of a sequence - is refused
        #    under a named reason. Nothing is converted, nothing is guessed.
        if not isinstance(height, HauteurScellee):
            return self._refuser(
                regime, UNSEALED_HEIGHT,
                f"bare height ({type(height).__name__} {height!r:.20}): provenance not proven. "
                f"The high-water is anchored on the height carried by the SIGNED note - an account "
                f"sequence resembles it and has no right to enter. Go through seal_height().")
        if height < 0:      # defence in depth: the type is constructible by hand
            return self._refuser(regime, MAL_FORME, f"negative sealed height: {height!r}")

        self._purger()
        cle = bytes(writing_key)
        emp = bytes(empreinte)

        # 1. Has this exact digest already been accepted? The exact discriminant, no clock involved.
        if emp in self._vues:
            return self._refuser(regime, REJEU, "digest already accepted")

        # 2. Per-key monotonicity, at `>=` and not `>`: several messages at the SAME height are
        #    legitimate (several reveals in one block) and the digest already tells them apart.
        h = self._high.get(cle)
        if h is not None and height < h:
            return self._refuser(regime, RECUL, f"height {height} < high-water {h}")

        # 3. Freshness - ONLY when the head is known. Its absence is not a refusal.
        if tete is not None:
            if not isinstance(tete, int) or isinstance(tete, bool) or tete < 0:
                return self._refuser(NOMINAL, MAL_FORME, f"invalid head: {tete!r}")
            if height < tete - self.fenetre:
                return self._refuser(NOMINAL, TROP_ANCIEN, f"height {height} < head {tete} - {self.fenetre}")
            if height > tete + self.fenetre:
                return self._refuser(NOMINAL, TROP_FUTUR, f"height {height} > head {tete} + {self.fenetre}")

        # accepted: record it, and say in which regime
        self._vues[emp] = float(self._horloge())
        if h is None or height > h:
            # `int(...)`: the internal state keeps only a number. The provenance marker is
            # meaningful at the ENTRY POINT; handing it back out of internal state would make it
            # claim something it no longer proves.
            self._high[cle] = int(height)
        if regime == NOMINAL:
            self.acceptes_nominal += 1
        else:
            self.acceptes_degrade += 1
        return Verdict(True, regime)

    # -- observability ---------------------------------------------------------------------------────────────
    def resume(self) -> str:
        r = " ".join(f"{k}={v}" for k, v in sorted(self.refus.items())) or "none"
        return (f"ANTIREPLAY nominal={self.acceptes_nominal} degrade={self.acceptes_degrade} "
                f"ecrivains={len(self._high)} empreintes={len(self._vues)} refus[{r}]")
