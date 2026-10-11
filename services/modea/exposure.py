"""Reading of public-exposure flags and listen addresses — one definition for the whole stack.

Invariant: a flag that drives a security guard is read here and nowhere else. Every service that
writes its own reading produces a stack where `DENDRA_PUBLIC=true` arms some refusals and not
others, with no message saying so, while the operator believes the whole stack is armed.

Two rules carry the module.

1. The vocabulary is closed. `1/true/yes/on` arm; `0/false/no/off` and the empty string do not;
   case and surrounding whitespace are irrelevant. Nothing else has a meaning.

2. A value outside the vocabulary is never interpreted — it raises `InvalidFlag` and the caller
   refuses to start. Falling back to the permissive default would turn a typo (`DENDRA_PUBLIC=ture`)
   into a silent exposure: the service starts, guards disarmed, believing itself public. On a
   security criterion, a value that is not understood never counts as compliant.

`listens_locally()` closes the same trap on the address side: `bind("")` is INADDR_ANY, hence all
interfaces. An empty string classified as loopback is a network exposure mistaken for a dev box.
"""
from __future__ import annotations

import os

ARMED = ("1", "true", "yes", "on")
INERT = ("0", "false", "no", "off", "")
# Listen addresses confined to the local machine. The empty string is deliberately absent.
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


class InvalidFlag(ValueError):
    """Unrecognised value on a security flag: the service must refuse to start."""


def security_flag(value, name: str = "DENDRA_PUBLIC") -> bool:
    """Return the armed/inert state of a security flag, or raise `InvalidFlag`.

    `None` (variable absent) and the empty string are inert: they are the two ways of not setting
    the variable and must read identically. Any other unrecognised value raises.
    """
    raw = ("" if value is None else str(value)).strip().lower()
    if raw in ARMED:
        return True
    if raw in INERT:
        return False
    raise InvalidFlag(
        f"{name}={value!r}: unrecognised value. Set {'/'.join(ARMED)} to arm, "
        f"{'/'.join(v for v in INERT if v)} or nothing otherwise. A security guard does not guess "
        f"the intent behind an unknown value.")


def public_enabled(value=None) -> bool:
    """Is `DENDRA_PUBLIC` armed? Raises `InvalidFlag` on an unexpected value."""
    return security_flag(value, "DENDRA_PUBLIC")


def public_from_env(env=None) -> bool:
    """Same reading, taken straight from the environment (`env` is injectable for tests)."""
    return public_enabled((os.environ if env is None else env).get("DENDRA_PUBLIC"))


def listens_locally(host) -> bool:
    """Is the listen address confined to the local machine?

    `""`, `0.0.0.0` and `::` are network listens: `bind("")` is INADDR_ANY. Classifying the empty
    string as loopback would silence the guard that depends on this address, exactly where it counts.
    """
    return ("" if host is None else str(host)).strip().lower() in LOOPBACK
