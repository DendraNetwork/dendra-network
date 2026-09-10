"""Reading of public-exposure flags and listen addresses — ONE definition for the whole stack.

Invariant: a flag that drives a security guard is read HERE and nowhere else. Every service that
writes its own reading produces a stack where `DENDRA_PUBLIC=true` arms some refusals and not
others, with no message saying so, while the operator believes the whole stack is armed.

Two rules carry the module.

1. The vocabulary is closed. `1/true/yes/on` arm; `0/false/no/off` and the empty string do not;
   case and surrounding whitespace are irrelevant. Nothing else has a meaning.

2. A value outside the vocabulary is never interpreted — it raises `DrapeauInvalide` and the caller
   REFUSES to start. Falling back to the permissive default would turn a typo (`DENDRA_PUBLIC=ture`)
   into a silent exposure: the service starts, guards disarmed, believing itself public. On a
   security criterion, a value that is not understood never counts as compliant.

`ecoute_locale()` closes the same trap on the address side: `bind("")` is INADDR_ANY, hence ALL
interfaces. An empty string classified as loopback is a network exposure mistaken for a dev box.
"""
from __future__ import annotations

import os

ARME = ("1", "true", "yes", "on")
INERTE = ("0", "false", "no", "off", "")
# Listen addresses confined to the local machine. The EMPTY string is deliberately absent.
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


class DrapeauInvalide(ValueError):
    """Unrecognised value on a security flag: the service must refuse to start."""


def drapeau_securite(valeur, name: str = "DENDRA_PUBLIC") -> bool:
    """Return the armed/inert state of a security flag, or raise `DrapeauInvalide`.

    `None` (variable absent) and the empty string are INERT: they are the two ways of not setting
    the variable and must read identically. Any other unrecognised value raises.
    """
    brut = ("" if valeur is None else str(valeur)).strip().lower()
    if brut in ARME:
        return True
    if brut in INERTE:
        return False
    raise DrapeauInvalide(
        f"{name}={valeur!r}: unrecognised value. Set {'/'.join(ARME)} to arm, "
        f"{'/'.join(v for v in INERTE if v)} or nothing otherwise. A security guard does not guess "
        f"the intent behind an unknown value.")


def public_actif(valeur=None) -> bool:
    """Is `DENDRA_PUBLIC` armed? Raises `DrapeauInvalide` on an unexpected value."""
    return drapeau_securite(valeur, "DENDRA_PUBLIC")


def public_from_env(env=None) -> bool:
    """Same reading, taken straight from the environment (`env` is injectable for tests)."""
    return public_actif((os.environ if env is None else env).get("DENDRA_PUBLIC"))


def ecoute_locale(host) -> bool:
    """Is the listen address confined to the local machine?

    `""`, `0.0.0.0` and `::` are NETWORK listens: `bind("")` is INADDR_ANY. Classifying the empty
    string as loopback would silence the guard that depends on this address, exactly where it counts.
    """
    return ("" if host is None else str(host)).strip().lower() in LOOPBACK
