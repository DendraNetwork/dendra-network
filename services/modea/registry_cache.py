"""Miner registry cache — it REPORTS a state, it DECIDES nothing.

WHAT THIS MODULE IS, AND WHAT IT IS NOT
It periodically reads the registry (`miner_id -> operator`) from the node's REST gateway, keeps it in
memory, and can say **which state it is in**. It authorises, refuses and evicts nothing: the policy
belongs to the caller.

That separation is not cosmetic. The policy ("what do we do with a stale cache?") is a JUDGEMENT
CALL; the state ("has this cache ever been read, and when?") is a MEASUREMENT. Mixing the two yields
a module that must be rewritten at every change of rule.

THREE STATES, AND THE DISTINCTION IS THE POINT
    NEVER_READ  — no read has ever succeeded since startup
    FEES      — last successful read less than `expiry` seconds ago
    STALE     — it was known once, and has not been known for long enough

WHY NEVER_READ IS NOT "STALE WITH AN EMPTY CACHE". The relay must not fail closed: a halted chain
must not destroy audit evidence. Applied as-is to a registry **that could never be read** — REST
gateway down, which is the default on a freshly joined node — that would give a relay which starts,
attributes nothing, accepts everything, and that **nothing distinguishes from a healthy relay**. An
absent guard that looks present. It is the same shape as a truncated `list-job`, which only ever
produces *good* news. The two states are therefore SEPARATE, and the caller is forced to handle them
distinctly.

NEVER RAISES on a network failure: a failed refresh is a FACT to count, not an exception to
propagate. It is not swallowed either — `derniere_erreur` and the counters report it, and `state()`
eventually flips to STALE.
"""
from __future__ import annotations

import json

NEVER_READ = "NEVER_READ"
FEES = "FEES"
STALE = "STALE"


class RegistryCache:
    """`lecteur`: callable() -> bytes|str (the JSON body). `horloge`: callable() -> float.

    Both are INJECTED so this module can be exercised without a network and without real waiting — a
    test that sleeps 60 seconds to check an expiry will never be run twice.
    """

    def __init__(self, lecteur, horloge, expiry: float = 300.0):
        if not callable(lecteur) or not callable(horloge):
            raise ValueError("lecteur and horloge must be callable")
        if expiry <= 0:
            raise ValueError(f"expiry must be > 0 (received {expiry})")
        self._lecteur = lecteur
        self._horloge = horloge
        self.expiry = float(expiry)
        self._operators: dict[str, str] = {}
        self._t_dernier_succes: float | None = None
        self.lectures_ok = 0
        self.lectures_ko = 0
        self.derniere_erreur: str | None = None

    # -- read ------------------------------------------------------------------------------------
    def refresh(self) -> bool:
        """Attempts a read. Returns True when it succeeded. NEVER raises.

        On failure the previous content is KEPT. Clearing it would turn a network outage into a loss
        of information: attribution would still be possible, and we would choose to stop doing it.
        """
        try:
            brut = self._lecteur()
            operators = self._extraire(brut)
        except Exception as e:  # noqa: BLE001
            self.lectures_ko += 1
            self.derniere_erreur = f"{type(e).__name__}: {str(e)[:120]}"
            return False
        self._operators = operators
        self._t_dernier_succes = float(self._horloge())
        self.lectures_ok += 1
        self.derniere_erreur = None
        return True

    @staticmethod
    def _extraire(brut) -> dict:
        """`miner_id -> operator` from the gateway JSON. Raises when the shape does not allow it.

        A miner WITHOUT an `operator` is DROPPED, not stored with an empty address: an empty entry
        would one day compare equal to an empty string and attribute to everyone.
        An EMPTY registry raises: a registry with zero entries signals a gateway that answers but does
        not serve what the caller assumes — accepting that case would make `operator()` always
        return None, i.e. a silent absence of attribution.
        """
        if isinstance(brut, (bytes, bytearray)):
            brut = brut.decode("utf-8")
        d = json.loads(brut) if isinstance(brut, str) else brut
        if not isinstance(d, dict):
            raise ValueError(f"root is {type(d).__name__}, object expected")
        liste = None
        for cle in ("miner", "Miner", "miners"):
            if isinstance(d.get(cle), list):
                liste = d[cle]
                break
        if liste is None:
            raise ValueError(f"no miner list (keys seen: {sorted(d)[:6]})")
        out = {}
        for m in liste:
            if not isinstance(m, dict):
                continue
            mid, op = m.get("miner_id"), m.get("operator")
            if isinstance(mid, str) and mid and isinstance(op, str) and op:
                out[mid] = op
        if not out:
            raise ValueError(f"{len(liste)} entry/entries but no usable miner_id/operator pair")
        return out

    # -- state -----------------------------------------------------------------------------------
    def state(self) -> str:
        if self._t_dernier_succes is None:
            return NEVER_READ
        return FEES if self.age() <= self.expiry else STALE

    def age(self) -> float | None:
        """Seconds since the last success, or None when NEVER_READ."""
        if self._t_dernier_succes is None:
            return None
        return float(self._horloge()) - self._t_dernier_succes

    def operator(self, miner_id: str):
        """`(operator, state)`. `operator` is None when unknown — the caller MUST read the state too.

        Returning `None` alone would be ambiguous: "this miner does not exist" and "the registry could
        never be read" are opposite answers that call for opposite decisions.
        """
        return self._operators.get(miner_id), self.state()

    def size(self) -> int:
        return len(self._operators)

    def resume(self) -> str:
        """Stable line, meant to be read by a probe or an exporter."""
        a = self.age()
        return (f"REGISTRY_CACHE state={self.state()} mineurs={self.size()} "
                f"age={'-' if a is None else f'{a:.0f}'} ok={self.lectures_ok} ko={self.lectures_ko}")
