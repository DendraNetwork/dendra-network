"""Tests for coordinator selection (pure logic, Mode B anti-collusion)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea.coordinator import MinerRegistry, select_mode_a, select_mode_b


def _miners():
    return [
        {"miner_id": "m1", "region": "eu", "operator": "alice", "healthy": True},
        {"miner_id": "m2", "region": "eu", "operator": "bob", "healthy": True},
        {"miner_id": "m3", "region": "eu", "operator": "carol", "healthy": True},
        {"miner_id": "m4", "region": "us", "operator": "alice", "healthy": True},
        {"miner_id": "m5", "region": "us", "operator": "dave", "healthy": True},
    ]


def test_mode_a_deterministic():
    ms = _miners()
    a1 = select_mode_a(ms, "seed-1")
    a2 = select_mode_a(ms, "seed-1")
    assert a1 == a2 and a1 in ms


def test_mode_a_varies_with_seed():
    ms = _miners()
    picks = {select_mode_a(ms, f"s{i}")["miner_id"] for i in range(20)}
    assert len(picks) > 1  # the VRF spreads the selection


def test_mode_b_group_constraints():
    g = select_mode_b(_miners(), "seed-1", group=3)
    assert g is not None and len(g) == 3
    assert len({m["operator"] for m in g}) == 3       # distinct operators
    assert len({m["region"] for m in g}) == 1         # same region (low RTT)


def test_mode_b_insufficient_operators():
    # a single region holding only 2 distinct operators -> no group
    ms = [
        {"miner_id": "a", "region": "eu", "operator": "x", "healthy": True},
        {"miner_id": "b", "region": "eu", "operator": "x", "healthy": True},
        {"miner_id": "c", "region": "eu", "operator": "y", "healthy": True},
    ]
    assert select_mode_b(ms, "s", group=3) is None


def test_registry_health_ttl():
    reg = MinerRegistry(ttl=0.0001)
    reg.register({"miner_id": "m", "region": "eu", "operator": "x"})
    import time
    time.sleep(0.01)
    assert reg.healthy() == []  # expired
    assert reg.heartbeat("m") is True
    assert len(reg.healthy()) == 1
