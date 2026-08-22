#!/usr/bin/env python3
"""DENDRA tokenomics v5 — a NATIVE model, designed for a useful inference network rather than for a
generic Cosmos chain.

Guiding idea (what differs from a classic Cosmos PoS):
  * NO perpetual inflation, NO minting. Supply CAPPED at 10,000,000 DNDR.
  * Rewards come out of a PRE-ALLOCATED RESERVE (33 % of genesis) that drains along a DECREASING
    curve -> no token is ever created, pre-allocated supply is RELEASED.
    => supply always <= 10 M; BURNS push it BELOW the genesis supply (genuinely deflationary).
  * 3 reward flows, all verified and bounded (anti-farming):
      - WORK          (per job, demand-gated: subsidy <= 1.5x the non-recoverable demand)
      - AVAILABILITY  (random challenge every 4 h, verified; bounded by the pool; slash on failure)
      - SECURITY      (validators: a DECREASING Reserve budget -> shifts to FEES as usage grows)
  * The development share is 5 % at genesis plus a GOVERNABLE fraction of INFERENCE revenue, not a tax
    on every transaction.
  * Once the Reserve is exhausted the economy is 100 % fee-funded (security included). No emission left.

Everything here is illustrative and governable. The price effect assumes real demand, which is not
guaranteed.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class V5Params:
    ticker: str = "DNDR"
    max_supply: int = 10_000_000                 # HARD CAP (never exceeded)
    # Genesis pockets in bps. These are READ FROM the shipped genesis, they are not a modelling
    # choice made here: the accounts and their coins are `chain/config.optimistic.yml:36-54`
    # and `docker/entrypoint-chain.sh:66-70`. A rounder number is easier to quote and is not the
    # one the chain applies. The faucet float is a POCKET of its own, not a rounding remainder:
    # dropping it silently inflates whichever pocket absorbs its 100 bps.
    # The keys are the genesis ACCOUNT names, which is why they are not translated.
    alloc: dict = field(default_factory=lambda: {  # sums to 10000
        "communaute": 3400, "reserve": 3300, "tresorerie": 2700, "equipe": 500, "faucet": 100})
    # --- emission from the RESERVE (decreasing, bounded, NON-inflationary) ---
    reserve_release_rate: float = 0.22           # annual release = 22 % of the REMAINING Reserve (decay)
    emit_split: dict = field(default_factory=lambda: {  # split of the Reserve release
        "work": 0.50, "availability": 0.20, "security": 0.30})
    # --- WORK (per job) ---
    jobs_day_y1: int = 50_000
    job_growth: float = 1.8                        # ANNUAL growth while far from the ceiling (early geometric phase)
    jobs_day_max: int = 50_000_000                 # market ceiling: SATURATING demand (logistic). Near it, growth
    #   falls back toward 1, which removes compounding that is not physical over 30 years. It bounds the TOP only and
    #   does NOT touch the bottom (a decline g<1 is preserved), so the security verdict — driven by weak demand —
    #   is unchanged.
    avg_fee_dndr: float = 0.005                   # average price of a job (what the user pays)
    miner_fee_share: float = 0.85                 # 85 % to the miner, 15 % to the protocol
    work_gate_x: float = 1.5                      # work subsidy <= 1.5x the NON-RECOVERABLE demand
    # --- AVAILABILITY (4 h challenge): bounded by the pool; no farming beyond it ---
    avail_pass_rate: float = 0.95                 # average pass rate on the challenge (the rest is slashed, out of model)
    # --- PROTOCOL (15 % of the job) ---
    protocol_split: dict = field(default_factory=lambda: {  # of the protocol cut (15 %)
        "validators": 0.50, "dev": 0.20, "treasury": 0.30})
    # --- SECURITY / staking ---
    target_bonded: float = 0.40                   # bonded target (security = fees + Reserve, not inflation)
    min_security_apr: float = 0.0                 # ADR-029: NO on-chain APR floor (fixed 10 M cap, zero mint, no tail).
    #   Security is funded by the Reserve (a few thousand epochs; "about 20 years" only under an assumed block
    #   interval the consensus does not fix) and THEN by fees alone, WITHOUT self-correction. The
    #   default 0 mirrors the on-chain reality (no simulated floor top-up). A value > 0 is a hypothetical SCENARIO
    #   knob and is NOT implemented on-chain.
    # --- DEFLATION ---
    bme_burn_share: float = 0.05                  # 5 % of fees burned -> GENTLE deflation. DNDR is a PAYMENT token
    #   (high velocity), so it must stay LIQUID and spendable rather than become a hoarded asset. At 5 % the supply
    #   is about 8.1 M after 10 years, scarcity about 1.23x: it appreciates without running away. At 30 % the supply
    #   would collapse to 2.3 M, and hyper-deflation drives hoarding, which kills the inference market.


def simulate_v5(p: V5Params, years: int = 10) -> list[dict]:
    supply = float(p.max_supply)                         # can only GO DOWN (burns)
    reserve = p.max_supply * p.alloc["reserve"] / 10000  # 3.3 M: the single source of emission
    treasury = p.max_supply * p.alloc["tresorerie"] / 10000
    circ = supply - reserve - treasury                   # initial circulating (community + team + faucet float)
    bonded_ratio = 0.20
    burned_cum = 0.0
    dev_cum = 0.0
    jobs_day = float(p.jobs_day_y1)
    rows = []
    for y in range(years):
        # 1) Reserve RELEASE (decreasing) -> 3 flows
        release = reserve * p.reserve_release_rate
        em_work_pool = release * p.emit_split["work"]
        em_avail = release * p.emit_split["availability"]
        em_sec = release * p.emit_split["security"]

        # 2) USAGE FEES (real revenue, paid by users -> recirculated, creates nothing)
        # The price of a job is pegged in USD. If the token appreciates (scarcity up), fewer DNDR are
        # needed per job, so DNDR-denominated fees are damped by scarcity: no runaway feedback.
        scarcity_now = p.max_supply / supply if supply else 1.0
        eff_fee = p.avg_fee_dndr / max(1.0, scarcity_now)
        fee_rev = jobs_day * 365 * eff_fee
        miner_fee = fee_rev * p.miner_fee_share
        protocol = fee_rev - miner_fee
        val_fee = protocol * p.protocol_split["validators"]
        dev_fee = protocol * p.protocol_split["dev"]
        treas_fee = protocol * p.protocol_split["treasury"]
        # NON-RECOVERABLE demand = WHAT THE CHAIN MEASURES, which is the DROP IN SUPPLY.
        # `RunEpoch` (x/emission/keeper/epoch.go) computes `demand = lastSupply - curSupply` and feeds
        # that to `EpochRelease`. Only the fee BURN destroys coins; the treasury and dev shares move
        # them between accounts, so total supply does not move and they can never enter that delta.
        # Counting them here made this model report about 2.5x the work subsidy the chain will ever
        # release -- a published number more generous than the code, on the flow that pays miners.
        bme_burn = fee_rev * p.bme_burn_share
        nonrec_demand = bme_burn

        # 3) WORK: demand-gated subsidy (the unused part STAYS in the Reserve -> anti-farming + duration)
        work_sub = min(em_work_pool, p.work_gate_x * nonrec_demand)
        released = work_sub + em_avail + em_sec          # what actually leaves the Reserve this year
        reserve -= released
        circ += released                                  # the released Reserve becomes circulating

        # 4) SECURITY / APR (floor)
        bonded = supply * bonded_ratio
        sec_income = em_sec + val_fee
        apr = sec_income / bonded if bonded else 0.0
        if p.min_security_apr > 0 and bonded and apr < p.min_security_apr:
            extra = min(p.min_security_apr * bonded - sec_income, reserve)  # bounded by the Reserve
            reserve -= extra; circ += extra; sec_income += extra; apr = sec_income / bonded
        # stake grows when the APR is attractive (>6 %)
        bonded_ratio = max(0.15, min(0.70, bonded_ratio + (0.04 if apr > 0.06 else -0.03)))

        # 5) BURN + treasury
        bme_burn = min(bme_burn, supply * 0.5)
        supply -= bme_burn; circ -= bme_burn
        burned_cum += bme_burn
        treasury += treas_fee
        dev_cum += dev_fee

        # 6) miner revenue (fees + work subsidy + availability)
        miner_total = miner_fee + work_sub + em_avail
        scarcity = p.max_supply / supply if supply else 0.0
        defl = bme_burn / (supply + bme_burn) if supply else 0.0
        rows.append({
            "year": y + 1,
            "supply": round(supply),
            "reserve_left": round(reserve),
            "scarcity_x": round(scarcity, 3),
            "miner_income": round(miner_total),
            "work_subsidy": round(work_sub),
            "avail_reward": round(em_avail),
            "val_apr_pct": round(apr * 100, 2),
            "real_apr_pct": round((apr + defl) * 100, 2),
            "bonded_pct": round(bonded_ratio * 100, 1),
            "fee_rev": round(fee_rev),
            "burned_cumul": round(burned_cum),
            "dev_fee_cumul": round(dev_cum),
            "treasury": round(treasury),
            "fee_funded": fee_rev > released,   # does usage fund more than the Reserve emission?
        })
        # SATURATING demand (logistic toward the market ceiling): jobs_day << K gives roughly geometric growth
        # (job_growth); as jobs_day approaches K, growth tends to 1. A decline (job_growth<1) is PRESERVED, so the
        # lower tail that drives the security verdict is unchanged. This replaces pure geometric growth, which is
        # not physical over 30 years.
        K = float(p.jobs_day_max)
        jobs_day = max(0.0, jobs_day * (1 + (p.job_growth - 1) * (1 - jobs_day / K)))
        jobs_day = min(jobs_day, K)
    return rows


def make_v5() -> V5Params:
    return V5Params()


def _fmt(x):
    return f"{x/1e6:.2f}M" if abs(x) >= 1e6 else (f"{x/1e3:.0f}k" if abs(x) >= 1e3 else f"{x:.3f}")


def main():
    p = make_v5()
    rows = simulate_v5(p, 10)
    print("=== DENDRA TOKENOMICS v5 - FIXED 10 M supply, emission from the Reserve (zero inflation) ===\n")
    print(f"{'Yr':>2} {'Supply':>8} {'Reserve':>8} {'Scarce':>7} {'Miner/yr':>10} {'APR':>6} {'APRr':>6} {'Bonded':>6} {'Fees':>8} {'Burn':>8} {'FeeFund':>7}")
    print("-" * 92)
    for r in rows:
        print(f"{r['year']:>2} {_fmt(r['supply']):>8} {_fmt(r['reserve_left']):>8} {r['scarcity_x']:>6}x "
              f"{_fmt(r['miner_income']):>10} {r['val_apr_pct']:>5}% {r['real_apr_pct']:>5}% {r['bonded_pct']:>5}% "
              f"{_fmt(r['fee_rev']):>8} {_fmt(r['burned_cumul']):>8} {'yes' if r['fee_funded'] else 'no':>7}")
    last = rows[-1]
    dep = next((r["year"] for r in rows if r["reserve_left"] < p.max_supply * 0.0033 * 100 * 0.05), None)
    print(f"\n>>> 10 years: supply {_fmt(rows[0]['supply'])} -> {_fmt(last['supply'])} (scarcity x{last['scarcity_x']}), "
          f"Reserve left {_fmt(last['reserve_left'])}, cumulative dev share {_fmt(last['dev_fee_cumul'])} DNDR")
    print(f">>> Switch to 'fee-funded': year {next((r['year'] for r in rows if r['fee_funded']), '-')}")
    print(f">>> Supply ALWAYS <= 10 M: {'OK' if all(r['supply'] <= p.max_supply for r in rows) else 'FAILED'}; "
          f"deflationary: {'OK' if last['supply'] < rows[0]['supply'] else 'no'}")


if __name__ == "__main__":
    main()
