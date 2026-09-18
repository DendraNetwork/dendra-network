"""Integration tests: the Mode A bridge to the state machine (confidentiality AND economics)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from modea.chain_bridge import CHAIN_AVAILABLE, OnChainInference
from modea.client import Client
from modea.miner import Miner

# The bridge settles against the reference state machine, which ships in the sibling `chain-modules`
# package of the development tree and is absent from a services-only distribution. These tests
# exercise that machine, so they are SKIPPED (never failed, never silently dropped) where it is
# missing: the reason line names the missing package, so a reader can tell an unavailable dependency
# apart from a broken bridge.
pytestmark = pytest.mark.skipif(
    not CHAIN_AVAILABLE,
    reason="the reference state machine package `chain` is not part of this distribution",
)


def _setup():
    b = OnChainInference()
    b.chain.mint("client", 50_000)
    b.chain.mint("opA", 10_000)
    m = Miner("m1", backend="mock")
    b.onboard_miner(m, operator="opA", region="eu", stake=2000, funder="opA")
    return b, m


def test_confidential_job_pays_and_hides_content():
    b, m = _setup()
    out = b.run_job(job_id="j1", client=Client(), client_addr="client", miner=m,
                    prompt="secret number SUPER-TOPSECRET-9", fee=1000)
    # the client does decrypt the miner's output (encrypted round trip)
    assert out.plaintext.startswith("[mock:")
    # settlement: 85 % to the miner = 850, of which 30 % vests -> 595 liquid; cut 150 -> team 30 / treasury 45
    assert b.chain.balances["m1"] == 595 and b.chain.locked_balance("m1") == 255
    assert b.chain.balances["_team"] == 30 and b.chain.treasury == 45
    # CONFIDENTIALITY: the sensitive data appears nowhere on-chain or in the ledger
    assert "SUPER-TOPSECRET-9" not in b.privacy_dump()
    assert len(b.ledger.dump_public()) == 1
    assert all(len(r["client_commit"]) == 64 for r in b.ledger.dump_public())


def test_unbonded_miner_is_rejected():
    b = OnChainInference()
    b.chain.mint("client", 10_000)
    ghost = Miner("ghost", backend="mock")
    with pytest.raises(ValueError):
        b.run_job(job_id="j1", client=Client(), client_addr="client", miner=ghost,
                  prompt="x", fee=1000)


def test_demand_recorded_and_subsidy_gated():
    b, m = _setup()
    epoch = b.chain.epoch_of()
    for jid in ("j1", "j2"):
        b.run_job(job_id=jid, client=Client(), client_addr="client", miner=m, prompt="p", fee=1000)
    b.chain.add_work_emission(1_000_000)
    b.chain.advance(b.chain.p.epoch_blocks)
    # non-recoverable 75/job, 2 jobs from the same client -> capped at 100; subsidy 100 * 1.5 = 150
    assert b.chain.counted_demand("m1", epoch) == 100
    assert b.claim_subsidy("m1", epoch) == 150
    assert b.claim_subsidy("m1", epoch) == 0          # already paid


def test_leak_canary_slashes_miner_onchain():
    b, m = _setup()
    out = b.run_job(job_id="j1", client=Client(), client_addr="client", miner=m,
                    prompt="confidential data", fee=1000, with_canary=True)
    assert out.canary_token                            # job watermarked
    res = b.report_leak(f"stolen text ... [[ref:{out.canary_token}]] ...", reporter="whistle")
    assert res["miner_id"] == "m1" and res["slashed"] == 1600
    assert b.chain.miners["m1"]["jailed"] and b.chain.balances["whistle"] == 480
