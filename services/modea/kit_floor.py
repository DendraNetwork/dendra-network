"""The kit floor, read in one place.

The relay (relay.py), the faucet (faucet.py) and the season rules (final_season_rules.py) read
DENDRA_KIT_MIN, and the first two DENDRA_KIT_DEADLINE, through this module: a value one of them accepts is a value
all of them accept, and the deadline is compared the same way everywhere.

The forms are the ones deploy/testnet/publish_network.sh checks before it publishes KIT_MIN and KIT_DEADLINE in
network-info.txt:
  DENDRA_KIT_MIN       a kit number: ASCII digits, no leading zero, at most nine of them (1 to 999999999). A floor of
                       0 is refused, since it would refuse nothing but a body without the field.
  DENDRA_KIT_DEADLINE  a UTC instant YYYY-MM-DDThh:mm:ssZ, two digits in every field but the year, that exists on the
                       calendar (no 30 February, no hour 24, no second 60).
Whitespace around a value is ignored. Anything else is an error, never read as a default.

The floor applies from the deadline on, the deadline itself included (`floor_in_force`).
"""
from __future__ import annotations

import datetime
import re

KIT_MIN_ENV = "DENDRA_KIT_MIN"
KIT_DEADLINE_ENV = "DENDRA_KIT_DEADLINE"
KIT_DEADLINE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The form is the bound (1 to 999999999), the one chain/cmd/dendra-alert/checks.go::kitMinRE applies too.
KIT_MIN_FORM = re.compile("[1-9][0-9]{0,8}")
# strptime alone reads one-digit fields ("2026-1-2T3:4:5Z"), which publish_network.sh refuses: the form comes first.
KIT_DEADLINE_FORM = re.compile("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _text(raw, name: str) -> str:
    """The value without its surrounding whitespace; None reads as unset. A value that is not text is refused."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{name}={raw!r} is not text")
    return raw.strip()


def parse_kit_min(raw) -> int | None:
    """The floor `raw` names: None when it is empty or unset, the integer when it is a kit number, ValueError for
    anything else."""
    s = _text(raw, KIT_MIN_ENV)
    if not s:
        return None
    if not KIT_MIN_FORM.fullmatch(s):
        raise ValueError(f"{KIT_MIN_ENV}={raw!r} is not a kit number (a positive integer of at most nine ASCII "
                         f"digits, no leading zero)")
    return int(s)


def parse_kit_deadline(raw) -> float | None:
    """The deadline `raw` names, in UTC seconds: None when it is empty or unset, ValueError for anything but an
    instant YYYY-MM-DDThh:mm:ssZ that exists."""
    s = _text(raw, KIT_DEADLINE_ENV)
    if not s:
        return None
    if not KIT_DEADLINE_FORM.fullmatch(s):
        raise ValueError(f"{KIT_DEADLINE_ENV}={raw!r} does not read YYYY-MM-DDThh:mm:ssZ in UTC")
    try:
        when = datetime.datetime.strptime(s, KIT_DEADLINE_FORMAT)
    except ValueError:
        raise ValueError(f"{KIT_DEADLINE_ENV}={raw!r} is not an instant that exists") from None
    return when.replace(tzinfo=datetime.timezone.utc).timestamp()


def policy_from_env(min_raw, deadline_raw):
    """`(floor, deadline_ts, error)` from the two raw values. Both unset: dormant, `(None, None, None)`. Both set and
    well formed: armed from `deadline_ts` (UTC seconds), `(int, float, None)`. Anything else: `(None, None, error)`,
    and the service refuses to start on it rather than guess a floor or a date."""
    try:
        m, d = _text(min_raw, KIT_MIN_ENV), _text(deadline_raw, KIT_DEADLINE_ENV)
        if not m and not d:
            return None, None, None
        if not m or not d:
            return None, None, (f"{KIT_MIN_ENV} and {KIT_DEADLINE_ENV} are set together or not at all "
                                f"(KIT_MIN={m!r}, KIT_DEADLINE={d!r})")
        return parse_kit_min(m), parse_kit_deadline(d), None
    except ValueError as e:
        return None, None, str(e)


def floor_in_force(floor, deadline_ts, now):
    """The floor a kit is held to at `now` (UTC seconds): None while the policy is dormant or before the deadline,
    the floor from the deadline on, the deadline itself included. A floor without a deadline, or the reverse, is no
    policy `policy_from_env` returns, and it is refused rather than read as dormant."""
    if floor is None and deadline_ts is None:
        return None
    if floor is None or deadline_ts is None:
        raise ValueError(f"kit floor {floor!r} and deadline {deadline_ts!r}: one without the other is not a policy")
    return floor if now >= deadline_ts else None
