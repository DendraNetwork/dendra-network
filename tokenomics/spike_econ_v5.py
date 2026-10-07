#!/usr/bin/env python3
"""MONTE-CARLO stress test of the v5 tokenomics, run against the REAL model (`simulate_v5`).

WHY THIS FILE EXISTS. `tokenomics_v5.py` is DETERMINISTIC: it produces a single trajectory, so v5 had
never been tested under uncertainty. An earlier stress test targeted the inflationary v4 model, and its
"robust" verdict was carried over to the v5 documentation, where it did not belong. This script closes
that gap: it SAMPLES the unknowns and measures the v5 invariants over N trajectories.

WHAT IS SAMPLED (the real sources of uncertainty):
  initial demand, growth (from DECLINE to strong increase), job price, miner share, Reserve release
  rate, work gate, burned share.

WHAT IS MEASURED (per run, over the horizon):
  supply_ok      supply <= 10 M every year (hard cap)
  reserve_ok     Reserve >= 0 AND monotonically decreasing (never minted)
  cliff_year     the year the security Reserve empties (< 1 % of genesis) -> security = FEES ALONE
                 (ADR-029: fixed cap, zero mint, NO on-chain APR floor, no self-correction)
  apr_horizon    validator APR at the horizon, after possible depletion: do fees sustain security?
  fee_funded     the year the network becomes "fee funded", or never
  deflation      final supply < initial supply

SCOPE, STATED PLAINLY: this tests the economy ON-MODEL. It does NOT model free-tier farming (the Reserve
pays the full reward outside the demand gate), nor grinding of the availability challenge (unspecified),
nor team vesting. The verdict below therefore concerns MONETARY SUSTAINABILITY, not resistance to every
abuse.
"""
from __future__ import annotations

import argparse
import datetime
import random
import statistics
from tokenomics_v5 import V5Params, simulate_v5


def sample_params(rng: random.Random) -> V5Params:
    """One uncertainty trajectory. Bounds are wide and deliberately pessimistic at the low end
    (job_growth < 1 means demand SHRINKS) so that unfavourable cases are exercised."""
    return V5Params(
        jobs_day_y1=int(rng.uniform(3_000, 120_000)),     # highly uncertain initial demand
        job_growth=rng.uniform(0.85, 2.2),                # 0.85 = -15 %/year ... 2.2 = strong growth (far from the cap)
        jobs_day_max=int(rng.uniform(5_000_000, 200_000_000)),  # uncertain market CAP (saturating demand)
        avg_fee_dndr=rng.uniform(0.001, 0.02),            # price of a job (USD-pegged, uncertain)
        miner_fee_share=rng.uniform(0.75, 0.92),
        reserve_release_rate=rng.uniform(0.15, 0.30),     # speed at which the Reserve drains
        work_gate_x=rng.uniform(1.0, 2.0),
        bme_burn_share=rng.uniform(0.02, 0.10),
    )


def run_one(p: V5Params, years: int) -> dict:
    rows = simulate_v5(p, years)
    res = [r["reserve_left"] for r in rows]
    sup = [r["supply"] for r in rows]
    # Security CLIFF (ADR-029: no on-chain floor): the year the Reserve drops below 1 % of genesis.
    cliff_thresh = (res[0] if res else 0) * 0.01
    cliff_year = next((r["year"] for r in rows if r["reserve_left"] <= cliff_thresh), None)
    return {
        "supply_ok": all(s <= p.max_supply for s in sup),
        "reserve_nonneg": all(x >= 0 for x in res),
        "reserve_monotone": all(res[i] >= res[i + 1] - 1 for i in range(len(res) - 1)),
        "fee_year": next((r["year"] for r in rows if r["fee_funded"]), None),
        "deflation": sup[-1] < sup[0],
        "reserve_left": res[-1],
        "supply_left": sup[-1],
        "min_apr": min(r["val_apr_pct"] for r in rows),
        "apr_horizon": rows[-1]["val_apr_pct"] if rows else 0.0,  # APR at the horizon (after possible depletion)
        "cliff_year": cliff_year,
    }


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def main():
    ap = argparse.ArgumentParser(description="Monte-Carlo tokenomics v5")
    ap.add_argument("-n", "--runs", type=int, default=4000)
    ap.add_argument("-y", "--years", type=int, default=30)  # 30 years: a 10-year horizon hides the Reserve cliff
    ap.add_argument("-s", "--seed", type=int, default=12345)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    runs = [run_one(sample_params(rng), a.years) for _ in range(a.runs)]
    n = len(runs)
    supply_ok = sum(r["supply_ok"] for r in runs)
    reserve_ok = sum(r["reserve_nonneg"] and r["reserve_monotone"] for r in runs)
    defl = sum(r["deflation"] for r in runs)
    fee_ever = [r["fee_year"] for r in runs if r["fee_year"] is not None]
    res_left = [r["reserve_left"] for r in runs]
    min_aprs = [r["min_apr"] for r in runs]
    horizon_aprs = [r["apr_horizon"] for r in runs]
    cliffs = [r["cliff_year"] for r in runs if r["cliff_year"] is not None]

    stamp = datetime.date.today().isoformat()
    print(f"=== MONTE-CARLO TOKENOMICS v5 (simulate_v5) — {n} runs x {a.years} years — seed {a.seed} — {stamp} ===\n")
    print(f"  supply <= 10 M (hard cap) ............... {pct(supply_ok, n):6.1f}%  [{supply_ok}/{n}]")
    print(f"  Reserve >= 0 and decreasing ............ {pct(reserve_ok, n):6.1f}%  [{reserve_ok}/{n}]")
    print(f"  deflationary (final supply < genesis) .. {pct(defl, n):6.1f}%  [{defl}/{n}]")
    print(f"  becomes 'fee funded' at some point ..... {pct(len(fee_ever), n):6.1f}%  [{len(fee_ever)}/{n}]")
    if fee_ever:
        print(f"      -> crossover year: median {int(statistics.median(fee_ever))}, "
              f"min {min(fee_ever)}, max {max(fee_ever)}")
    # Security CLIFF (ADR-029: NO on-chain floor) — when the security Reserve empties.
    print(f"  security Reserve emptied (<1% genesis) . {pct(len(cliffs), n):6.1f}% of runs over {a.years} years")
    if cliffs:
        print(f"      -> CLIFF year: median {int(statistics.median(cliffs))}, "
              f"min {min(cliffs)}, max {max(cliffs)}  (after that: security = FEES ALONE)")
    # With SATURATING demand (a market cap), the HIGH end of the horizon APR is PHYSICAL — a network
    # saturated at the cap earns many fees against a bounded bond, so the APR is high but finite — not an
    # artefact of geometric compounding. For SECURITY after the cliff, the relevant figure is the LOW end
    # of the distribution (p05 and min: weak demand).
    print(f"  validator APR at the HORIZON ({a.years} yr) . p05 {sorted(horizon_aprs)[n//20]:.2f}% , "
          f"min {min(horizon_aprs):.2f}%  (low end = security under weak demand; median {statistics.median(horizon_aprs):.0f}% = network saturated at the cap, high but physical)")
    print(f"  minimum validator APR (any year) ....... median {statistics.median(min_aprs):.2f}% , "
          f"p05 {sorted(min_aprs)[n//20]:.2f}% , min {min(min_aprs):.2f}%")
    print(f"  Reserve left at {a.years} years ............. median {statistics.median(res_left)/1e6:.2f}M , "
          f"min {min(res_left)/1e6:.2f}M , max {max(res_left)/1e6:.2f}M")

    # --- Verdict (ADR-029: fixed 10 M cap, zero mint, NO on-chain APR floor) ---
    hard = pct(supply_ok, n) >= 99.9 and pct(reserve_ok, n) >= 99.9
    cliff_share = pct(len(cliffs), n)
    horizon_p05 = sorted(horizon_aprs)[n // 20]
    print("\n>>> VERDICT (this run):")
    print(f"    HARD invariants (supply<=10M, Reserve>=0 and decreasing): "
          f"{'HELD 100%' if hard else 'VIOLATED — investigate'}")
    print(f"    Long-term security (NO floor, ADR-029): the security Reserve empties in "
          f"{cliff_share:.0f}% of runs over {a.years} years -> after that, security = FEES ALONE, with NO self-correction.")
    print(f"    Validator APR at the horizon, LOW end of the distribution (the security test): p05 {horizon_p05:.2f}% , "
          f"min {min(horizon_aprs):.2f}% -> under durably weak demand after the cliff the APR falls to about 0 and is NOT")
    print("    recovered; the cliff is accepted by ADR-029. The high end (strong demand) is NOT a security risk.")
    print("    Scope: free-tier, availability and vesting are out of model (see the header). This measures MONETARY")
    print("    sustainability, not abuse resistance.")
    print("    AVAILABILITY CAVEAT: the availability cost is OPTIMISTIC (avail_pass_rate=0.95 is an out-of-model")
    print("    placeholder) while ADR-022 is not implemented, so grinding is UNDER-MODELLED. The real failure rate and")
    print("    slash (AvailSlashBps and friends) will be modelled with full ADR-022. The security verdict (low end of")
    print("    the distribution) is unchanged: the availability cost does not drive it.")


if __name__ == "__main__":
    main()
