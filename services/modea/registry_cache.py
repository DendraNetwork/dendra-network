"""Miner registry cache: it reports a state and decides nothing.

It periodically reads the registry (`miner_id -> operator`, and `miner_id -> creator`) from the node's
REST gateway, keeps it in memory, and can say which state it is in. It authorises, refuses and evicts
nothing: the policy belongs to the caller.

Two addresses per miner. The chain records the creator (the key that signed `create-miner`: the identifier
derives from it, it holds the bond, it alone updates or deletes the miner) and the operator (named by the
creator: it signs the miner's work). They are the same key for a miner that registered itself, and
different keys in the miner kit's owner mode. Which of the two a write must come from is the caller's
policy; this module reads both and decides neither.

The policy ("what to do with a stale cache") is a judgement; the state ("has this cache ever been read,
and when") is a measurement. Keeping them apart means a change of rule never rewrites this module.

Three states:
    NEVER_READ  no read has succeeded since startup
    FEES        the last successful read is less than `expiry` seconds old
    STALE       it was known once, and has not been known for long enough

NEVER_READ is kept apart from a stale empty cache. The relay must not fail closed, because a halted
chain must not destroy audit evidence; applied to a registry that could never be read (a REST gateway
down, the default on a freshly joined node), that rule would give a relay that attributes nothing,
accepts everything, and looks healthy. The caller is therefore made to handle the two states
separately.

It never raises on a network failure: a failed refresh is counted, not propagated. `last_error` and
the counters report it, and `state()` eventually turns to STALE.
"""
from __future__ import annotations

import json

NEVER_READ = "NEVER_READ"
FEES = "FEES"
STALE = "STALE"


class RegistryCache:
    """`reader`: callable() -> bytes|str (the JSON body). `clock`: callable() -> float.

    Both are injected so that this module can be tested without a network and without real waiting.
    """

    def __init__(self, reader, clock, expiry: float = 300.0):
        if not callable(reader) or not callable(clock):
            raise ValueError("reader and clock must be callable")
        if expiry <= 0:
            raise ValueError(f"expiry must be > 0 (received {expiry})")
        self._reader = reader
        self._clock = clock
        self.expiry = float(expiry)
        self._operators: dict[str, str] = {}
        self._creators: dict[str, str] = {}
        self._last_success_at: float | None = None
        self.reads_ok = 0
        self.reads_failed = 0
        self.last_error: str | None = None

    # -- read ------------------------------------------------------------------------------------
    def refresh(self) -> bool:
        """Attempts a read. Returns True when it succeeded. Never raises.

        On failure the previous content is kept: clearing it would turn a network outage into a loss of
        information and stop an attribution that is still possible.
        """
        try:
            raw = self._reader()
            operators = self._extract_operators(raw)
            creators = self._extract_creators(raw)
        except Exception as e:  # noqa: BLE001
            self.reads_failed += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            return False
        # Both maps come from the same read and are replaced together: an operator from one read paired
        # with a creator from another would describe a miner that existed at neither moment.
        self._operators = operators
        self._creators = creators
        self._last_success_at = float(self._clock())
        self.reads_ok += 1
        self.last_error = None
        return True

    @staticmethod
    def _extract_operators(raw) -> dict:
        """`miner_id -> operator` from the gateway JSON. Raises when the shape does not allow it.

        A miner without an `operator` is dropped, not stored with an empty address: an empty entry
        could one day compare equal to an empty string and attribute to everyone.
        An empty registry raises: zero entries means a gateway that answers but does not serve what the
        caller assumes, and accepting it would make `operator()` always return None, a silent loss of
        attribution.
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        d = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(d, dict):
            raise ValueError(f"root is {type(d).__name__}, object expected")
        entries = None
        for key in ("miner", "Miner", "miners"):
            if isinstance(d.get(key), list):
                entries = d[key]
                break
        if entries is None:
            raise ValueError(f"no miner list (keys seen: {sorted(d)[:6]})")
        out = {}
        for m in entries:
            if not isinstance(m, dict):
                continue
            mid, op = m.get("miner_id"), m.get("operator")
            if isinstance(mid, str) and mid and isinstance(op, str) and op:
                out[mid] = op
        if not out:
            raise ValueError(f"{len(entries)} entry/entries but no usable miner_id/operator pair")
        return out

    @staticmethod
    def _extract_creators(raw) -> dict:
        """`miner_id -> creator` from the same JSON. Never raises, on purpose.

        Attribution relies on the operator map above, so a read that yields it must not be thrown away
        because the creators are missing. A miner without a creator is absent from this map and
        `creator()` answers None for it, "the owner is unknown", which a caller deciding on ownership must
        refuse on and never replace with the operator. A body that is not a miner registry at all
        (`job_registry.JobRegistry` reuses this class for the job list) yields {} the same way.
        """
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            d = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(d, dict):
            return {}
        entries = None
        for key in ("miner", "Miner", "miners"):
            if isinstance(d.get(key), list):
                entries = d[key]
                break
        out = {}
        for m in entries or []:
            if not isinstance(m, dict):
                continue
            mid, creator = m.get("miner_id"), m.get("creator")
            if isinstance(mid, str) and mid and isinstance(creator, str) and creator:
                out[mid] = creator
        return out

    # -- state -----------------------------------------------------------------------------------
    def state(self) -> str:
        if self._last_success_at is None:
            return NEVER_READ
        return FEES if self.age() <= self.expiry else STALE

    def age(self) -> float | None:
        """Seconds since the last success, or None when NEVER_READ."""
        if self._last_success_at is None:
            return None
        return float(self._clock()) - self._last_success_at

    def operator(self, miner_id: str):
        """`(operator, state)`. `operator` is None when unknown, so the caller must read the state too.

        `None` alone would be ambiguous: "this miner does not exist" and "the registry could never be
        read" call for opposite decisions.
        """
        return self._operators.get(miner_id), self.state()

    def creator(self, miner_id: str):
        """`(creator, state)`, read from the same snapshot as `operator()`. `creator` is None when the
        registry read names none for this miner -- unknown, never "the operator"."""
        return self._creators.get(miner_id), self.state()

    def size(self) -> int:
        return len(self._operators)

    def summary(self) -> str:
        """Stable line, meant to be read by a probe or an exporter."""
        a = self.age()
        return (f"REGISTRY_CACHE state={self.state()} miners={self.size()} "
                f"age={'-' if a is None else f'{a:.0f}'} ok={self.reads_ok} failed={self.reads_failed}")
