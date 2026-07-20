"""Tests : ledger persistant + règlement/réputation + liveness coordinateur."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modea.ledger import Ledger
from modea.coordinator import MinerRegistry


def test_ledger_save_load_roundtrip(tmp_path):
    led = Ledger()
    led.record("j1", "m1", "c" * 64, "r" * 64, "k" * 64)
    led.record("j2", "m2", "a" * 64)
    p = tmp_path / "ledger.json"
    led.save(p)
    re = Ledger.load(p)
    assert len(re.dump_public()) == 2
    assert re.get("j1").miner_id == "m1"


def test_ledger_persists_only_hashes(tmp_path):
    led = Ledger()
    led.record("j1", "m1", "deadbeef" * 8)
    p = tmp_path / "ledger.json"
    led.save(p)
    text = p.read_text(encoding="utf-8")
    assert "4471" not in text and "prompt" not in text  # aucun contenu


def test_registry_reputation_bump():
    reg = MinerRegistry()
    reg.register({"miner_id": "m1", "region": "eu", "operator": "x"})
    reg.bump("m1"); reg.bump("m1")
    h = {m["miner_id"]: m for m in reg.healthy()}
    assert h["m1"]["jobs_served"] == 2


def test_registry_liveness_drop():
    reg = MinerRegistry(ttl=0.05)
    reg.register({"miner_id": "m1", "region": "eu", "operator": "x"})
    assert len(reg.healthy()) == 1
    time.sleep(0.1)
    assert reg.healthy() == []            # expire sans heartbeat
    reg.heartbeat("m1")
    assert len(reg.healthy()) == 1        # revient apres battement
