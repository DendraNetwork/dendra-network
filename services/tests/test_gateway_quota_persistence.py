"""The free-tier quota must survive a restart, and must CLOSE rather than reopen when unreadable.

These counters bound a tap that spends the project's own subsidy -- `FREE_TOK_DAY` globally,
`FREE_TOK_IP` per device. `_quota_load` already carries a fail-closed branch for a corrupt file,
written because resetting the counters to zero "would reopen the free tap". Nothing ever exercised it,
and nothing pinned the property it protects. Both halves are pinned here, plus the ordering they
depend on: `_quota_roll` zeroes whenever the stored day differs from today, so a restored day is as
load-bearing as a restored count.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _restart(quota_file):
    """Reload the module from scratch -- a restart -- with the quota bound to `quota_file`.

    `_quota_load()` is called explicitly because the module does not call it at import: `main()` does,
    once, before serving (`gateway.py:502`). A test that only reloaded would exercise a gateway
    that never reads its own file.
    """
    if quota_file is None:
        os.environ.pop("DENDRA_QUOTA_FILE", None)
    else:
        os.environ["DENDRA_QUOTA_FILE"] = quota_file
    import gateway
    gw = importlib.reload(gateway)
    gw._quota_load()
    return gw


def _path():
    return os.path.join(tempfile.mkdtemp(prefix="dendra-quota-"), "quota.json")


def test_the_counters_SURVIVE_a_restart_when_a_file_is_configured():
    p = _path()
    gw = _restart(p)
    gw._quota_roll()
    gw._QUOTA["glob"] = 123456
    gw._QUOTA["ip"]["203.0.113.7"] = 4321
    gw._quota_save()
    day = gw._QUOTA["day"]

    gw = _restart(p)
    assert gw._QUOTA["glob"] == 123456, gw._QUOTA
    assert gw._QUOTA["ip"]["203.0.113.7"] == 4321, gw._QUOTA
    assert gw._QUOTA["day"] == day, gw._QUOTA


def test_the_first_roll_after_a_restart_does_not_wipe_what_the_load_restored():
    """A restored count is worthless without its day: `_quota_roll` runs on the FIRST request, and it
    resets whenever the stored day differs from today -- the empty day of a fresh module included."""
    p = _path()
    gw = _restart(p)
    gw._quota_roll()
    gw._QUOTA["glob"] = 999
    gw._quota_save()

    gw = _restart(p)
    gw._quota_roll()
    assert gw._QUOTA["glob"] == 999, gw._QUOTA


def test_an_UNREADABLE_file_keeps_the_tap_closed_THROUGH_the_first_request():
    """Fail-closed is a property of the SERVED gateway, not of one function's return.

    A truncated file (crash, full disk) must never grant free service. `_quota_load` marks the global
    bucket exhausted -- but the very next `_quota_roll`, on the first request, compares days and would
    zero it again. The assertion is therefore taken AFTER a roll, which is the only place the property
    is worth anything.
    """
    p = _path()
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"day": "2026-01-01", "glob": 12')     # truncated mid-write
    gw = _restart(p)
    assert gw._QUOTA["glob"] >= gw.FREE_TOK_DAY, gw._QUOTA

    gw._quota_roll()
    assert gw._QUOTA["glob"] >= gw.FREE_TOK_DAY, gw._QUOTA


def test_WITHOUT_a_configured_file_the_counters_are_MEMORY_ONLY():
    """Why the compose wiring exists. Not a defect of these functions -- a defect of leaving them
    unwired: with no path the fail-closed branch above cannot run at all, and every restart reopens
    the tap in silence, which is precisely what that branch was written to prevent."""
    gw = _restart(None)
    gw._quota_roll()
    gw._QUOTA["glob"] = 555
    gw._quota_save()                                    # a no-op without a path

    gw = _restart(None)
    assert gw._QUOTA["glob"] == 0, gw._QUOTA
