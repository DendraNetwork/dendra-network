"""DENDRA_CAPACITY_NODE_CACHE_S and DENDRA_CAPACITY_ONCHAIN_CACHE_S: how long the on-chain miner list, and the
`onchain` block, are kept before the next read.

Each case starts the shipped `capacity_server` in a fresh interpreter with the variable set (or absent), because the
value is read once, when the module loads. What these cases hold:
  * absent: the cache lasts 60 seconds;
  * set to a number of seconds from CACHE_MIN_S to CACHE_MAX_S (1 to 3600): that number is used;
  * anything else (text, empty, zero, negative, nan, inf, below the minimum, above the maximum, 1e-9, 1e18) stops
    the service at start with a message naming the variable, never read as the default;
  * the interval the `onchain` description publishes is the one in force, not a truncation of it.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

# DENDRA_MODEA_DIR points this bench at a copy of the modules, so a mutation can be checked against it.
MODEA = os.environ.get("DENDRA_MODEA_DIR", str(Path(__file__).resolve().parent.parent))
NODE = ("DENDRA_CAPACITY_NODE_CACHE_S", "NODE_CACHE_S")
ONCHAIN = ("DENDRA_CAPACITY_ONCHAIN_CACHE_S", "ONCHAIN_CACHE_S")
SETTINGS = pytest.mark.parametrize("var,attr", [NODE, ONCHAIN], ids=["node", "onchain"])


def _load(value, var, attr):
    probe = "import sys; sys.path.insert(0, sys.argv[1]); import capacity_server as cs; print(cs.%s)" % attr
    env = {k: v for k, v in os.environ.items() if k != var}
    if value is not None:
        env[var] = value
    return subprocess.run([sys.executable, "-c", probe, MODEA], env=env, capture_output=True, text=True, timeout=60)


@SETTINGS
def test_absent_keeps_the_sixty_second_cache(var, attr):
    r = _load(None, var, attr)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "60.0"


@SETTINGS
@pytest.mark.parametrize("raw,expected", [("1", "1.0"), ("5", "5.0"), ("120", "120.0"), ("3600", "3600.0")])
def test_a_set_value_is_used(var, attr, raw, expected):
    r = _load(raw, var, attr)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


@SETTINGS
@pytest.mark.parametrize("raw", ["sixty", "", "0", "-5", "nan", "inf", "5s", "0.5", "0.999", "3601", "1e-9", "1e18"])
def test_a_value_outside_the_bounds_is_refused_at_start(var, attr, raw):
    r = _load(raw, var, attr)
    assert r.returncode != 0
    assert var in r.stderr
    assert r.stdout.strip() == ""            # it never reached the point of reading a default


def test_the_published_interval_is_the_one_in_force():
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import capacity_server as cs; "
             "cs._onchain_miner_ids = lambda: set(); "
             "print(cs.aggregate({}, chain={})['_provenance']['onchain'])")
    env = dict(os.environ, DENDRA_CAPACITY_ONCHAIN_CACHE_S="2.5")
    r = subprocess.run([sys.executable, "-c", probe, MODEA], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "once every 2.5 seconds" in r.stdout
