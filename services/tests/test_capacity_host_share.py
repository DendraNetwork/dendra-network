# -*- coding: utf-8 -*-
"""ONE IDENTITY PER CARD, ONE MACHINE: the capacity registry counts the machine's RAM and cores ONCE.

With deploy/join.sh --gpus every card of a machine publishes its own signed report, and each report carries
the same machine's RAM and cores. Summed as they are, three cards would triple the machine's memory on the
public page -- an over-count, the one direction this registry refuses everywhere else. So the reports of the
extra slots say `host_share: "secondary"`, and:
  * a secondary report adds its CARD (its VRAM, one to gpu_nodes) and not the machine's RAM and cores, in the
    declared totals AND in the verified ones;
  * an ABSENT host_share is "primary" -- the zero value, and what every older kit sends: counted as before;
  * host_share is NEVER published: it would tie identities to one box, which is what dropping `machine` from
    the public rows already refuses.
The shipped `_clean` and `aggregate` are driven in-process, the on-chain registry reader stubbed. The subject
can be pointed at a MUTATED copy (DENDRA_CAPACITY_SERVER_FILE) so a harness can show each case turns red.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("DENDRA_CAPACITY_SERVER_FILE"):
    _spec = importlib.util.spec_from_file_location("capacity_server", os.environ["DENDRA_CAPACITY_SERVER_FILE"])
    cs = importlib.util.module_from_spec(_spec)
    sys.modules["capacity_server"] = cs
    _spec.loader.exec_module(cs)
else:
    import capacity_server as cs  # noqa: E402

IDS = ["dm1slotzero000000000000000", "dm1slotone0000000000000000", "dm1slottwo0000000000000000"]


def _posted(node_id, miner_id, share=None, vram=12288):
    body = {"node_id": node_id, "machine": "abcdef123456", "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": vram, "ram_mb": 65536, "cpu_cores": 16, "tier": 3,
            "model": "mistral-nemo", "can_judge": False, "judge_backend": "none", "miner_ids": [miner_id]}
    if share is not None:
        body["host_share"] = share
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = miner_id
    return rep


def _store(*reps):
    return {cs.storage_key(r): r for r in reps}


def _rig(monkeypatch):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    return _store(_posted("m-rig-0", IDS[0]), _posted("m-rig-0-g1", IDS[1], "secondary"),
                  _posted("m-rig-0-g2", IDS[2], "secondary", vram=8192))


def test_three_cards_one_machine_count_its_ram_and_cores_once(monkeypatch):
    a = cs.aggregate(_rig(monkeypatch))
    assert a["ram_total_mb"] == cs._round_mb(65536) and a["cpu_cores_total"] == cs._band_down(16)
    assert a["verified"]["ram_total_mb"] == cs._round_mb(65536)
    assert a["verified"]["cpu_cores_total"] == cs._band_down(16)


def test_each_card_still_adds_its_vram_and_one_gpu_node(monkeypatch):
    a = cs.aggregate(_rig(monkeypatch))
    want = 2 * cs._round_mb(12288) + cs._round_mb(8192)
    assert a["vram_total_mb"] == want and a["verified"]["vram_total_mb"] == want
    assert a["gpu_nodes"] == 3 and a["verified"]["gpu_nodes"] == 3


def test_an_absent_host_share_is_primary_as_every_older_kit(monkeypatch):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(IDS))
    a = cs.aggregate(_store(_posted("m-old-a", IDS[0]), _posted("m-old-b", IDS[1])))
    assert a["ram_total_mb"] == 2 * cs._round_mb(65536)
    assert cs._clean({"node_id": "x"})["host_share"] == "primary"
    assert cs._clean({"node_id": "x", "host_share": "SECONDARY!"})["host_share"] == "primary"


def test_host_share_is_never_published(monkeypatch):
    a = cs.aggregate(_rig(monkeypatch))
    for row in a["nodes"]:
        assert "host_share" not in row, row
    assert "secondary" not in json.dumps(a)
