#!/usr/bin/env python3
"""INVARIANTS of the v5 tokenomics model.

This is the only suite covering the canonical model `tokenomics_v5.py`, and it is the only one this
directory can hold: the invariants of the DEPRECATED v1-v4 models (supply grows with emission,
inflation decays) CONTRADICT v5, so a suite written for them cannot coexist with this one. No module
here may import a module that is absent either — an uncollectable file turns `pytest tests/` into a
collection ERROR that runs nothing at all, this file included. A broken test does not merely fail to
protect: it takes its healthy neighbours down with it.

This file locks the properties that belong to v5:
  - supply CAPPED at 10 M, never exceeded
  - Reserve >= 0 and monotonically decreasing (zero mint: the model only RELEASES pre-allocated tokens)
  - consistent distribution sums (alloc = 10000 bps, emit_split = 1, protocol_split = 1)
  - real deflation under positive demand
  - the miner keeps the majority of the fee (a property v5 inherits from the earlier models)
  - NO on-chain APR floor — see the test, and ADR-029

Run with `python3 test_tokenomics_v5.py` (standalone, assert-based) or with `pytest`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tokenomics_v5 import V5Params, simulate_v5

P = V5Params()


# ---------- distribution sums ----------
def test_alloc_sums_to_10000_bps():
    assert sum(P.alloc.values()) == 10000


def test_emit_split_sums_to_one():
    assert abs(sum(P.emit_split.values()) - 1.0) < 1e-9


def test_protocol_split_sums_to_one():
    assert abs(sum(P.protocol_split.values()) - 1.0) < 1e-9


# ---------- capped supply / no mint ----------
def test_supply_never_exceeds_cap():
    for r in simulate_v5(P, 10):
        assert r["supply"] <= P.max_supply, f"supply {r['supply']} above the cap in year {r['year']}"


def test_supply_is_deflationary_under_demand():
    rows = simulate_v5(P, 10)
    assert rows[-1]["supply"] < rows[0]["supply"], "supply must be deflationary when there is demand"


def test_reserve_nonnegative_and_monotone():
    rows = simulate_v5(P, 10)
    res = [r["reserve_left"] for r in rows]
    assert all(x >= 0 for x in res), "the Reserve must never go negative"
    assert all(res[i] >= res[i + 1] - 1 for i in range(len(res) - 1)), \
        "the Reserve must only decrease: there is no mint"


def test_no_mint_total_emitted_at_most_reserve_alloc():
    """Everything released from the Reserve stays <= the initial Reserve allocation: no token is created."""
    rows = simulate_v5(P, 30)
    reserve0 = P.max_supply * P.alloc["reserve"] / 10000
    total_emitted = reserve0 - rows[-1]["reserve_left"]
    assert total_emitted <= reserve0 + 1, "cumulative emission exceeds the Reserve: a mint slipped in"


# ---------- APR floor: the branch MUST fire under weak demand ----------
def _low_demand():
    # Disappointing demand: few jobs, low prices, demand RECEDING -> APR under the floor.
    return replace(P, jobs_day_y1=1_000, job_growth=0.9, avg_fee_dndr=0.001)


def test_there_is_no_on_chain_apr_floor_by_default():
    """⛔ THERE IS NO ON-CHAIN APR FLOOR, AND THIS TEST IS WHAT KEEPS THE DEFAULT THAT WAY.

    ADR-029 **withdraws** the promise in as many words: the staking floor is "announced but not
    implemented on-chain; no mechanism raises emission if APR falls. That is an over-claim and must
    be corrected." The parameter follows that decision (`min_security_apr = 0.0`,
    tokenomics_v5.py:52). The opposite assertion — that the floor is defended every year — cannot
    hold on this model: with the parameter at zero, the target derived from it is a reading that weak
    demand never produces, because the trajectory bottoms out far above zero. Such a test goes red
    for a reason unrelated to what it means to measure.

    ⚠️ THIS DOCSTRING DELIBERATELY QUOTES NO FIGURE. The publication gate forbids a numeric yield
    next to "APR", and it cannot tell a claim from its retraction — explaining the withdrawal in the
    withdrawn claim's own numbers trips it. ADR-029 carries a written exemption because a retraction
    MUST cite what it retracts; a test docstring does not have to, so it does not.

    What this test locks is the DECISION, not the withdrawn claim: the default is no floor, so the
    top-up branch stays inert unless somebody arms it deliberately. If a future default puts it back
    above zero, this goes red and points here.

    ⚠️ ADR-029 also records why re-arming it would need more than a parameter: the Monte-Carlo that
    reported "floor robust, 100 %" is a HORIZON ARTEFACT — ten years is shorter than the Reserve.
    """
    assert P.min_security_apr == 0.0, (
        "ADR-029: no on-chain APR floor. A non-zero default re-introduces a claim the ADR withdrew, "
        "and nothing in the chain implements the top-up it would promise.")
    rows = simulate_v5(_low_demand(), 10)
    assert all(r["val_apr_pct"] > 0.02 for r in rows), (
        "with no floor the APR is whatever fees and the Reserve produce; a 0.00 % reading would mean "
        "the branch pinned it to a floor of zero, which is not the same thing as having no floor")


def test_the_floor_top_up_stays_bounded_by_the_reserve_when_armed():
    """The branch is inert by default (above), but it still EXISTS as a modelling knob. Exercised
    here so it is not dead code nobody has run: armed at 5 %, it must pin the APR AND never fund the
    top-up beyond what the Reserve holds — `extra` is bounded by `reserve` (tokenomics_v5.py:107).
    Arming it in a simulation says nothing about the chain, which implements no such mechanism."""
    p = replace(_low_demand(), min_security_apr=0.05)
    rows = simulate_v5(p, 10)
    floor = p.min_security_apr * 100
    pinned = [r for r in rows if abs(r["val_apr_pct"] - floor) < 0.02]
    assert pinned, "armed at 5 %, the top-up branch never fired — it is then untested dead code"
    for r in rows:
        assert r["reserve_left"] >= 0, "the top-up drew more than the Reserve held"
        assert r["supply"] <= p.max_supply, "the top-up minted above the cap"


def test_miner_keeps_the_majority_of_the_fee():
    """Carried over from the deleted v1-v4 suite, ported to v5: the operator who does the work keeps
    the bulk of what the client pays. `miner_fee_share` is 0.85, so the miner's income must stay well
    above 80 % of fee revenue once the work subsidy is set aside."""
    rows = simulate_v5(P, 5)
    for r in rows:
        assert r["fee_rev"] > 0, "no fee revenue: this test would assert nothing"
        assert r["miner_income"] >= 0.8 * r["fee_rev"], (
            f"year {r['year']}: the miner keeps {r['miner_income'] / r['fee_rev']:.0%} of the fee, "
            f"below the {P.miner_fee_share:.0%} the model promises")


def test_hard_invariants_hold_across_scenarios():
    scenarios = {
        "default": P,
        "weak_demand": _low_demand(),
        "strong_growth": replace(P, jobs_day_y1=100_000, job_growth=2.2, avg_fee_dndr=0.02),
        "fast_release": replace(P, reserve_release_rate=0.30),
        "high_burn": replace(P, bme_burn_share=0.10),
    }
    for name, p in scenarios.items():
        rows = simulate_v5(p, 12)
        assert all(r["supply"] <= p.max_supply for r in rows), f"[{name}] supply above 10 M"
        assert all(r["reserve_left"] >= 0 for r in rows), f"[{name}] Reserve negative"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)} tests passed")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
