#!/usr/bin/env python3
"""Cost model: dollars per million output tokens on Dendra, against the list price of a hosted API.

This does not model the on-chain price in udndr: there is no market price, so the figure would mean
nothing. It models the real resource cost (electricity plus GPU amortization) of producing one output
token, multiplied by the k=3 redundancy, against the list price of a hosted API serving a small model.

The assumptions are in main() and can all be changed. No proprietary data is used; the reference prices
below are a snapshot of hosted list prices for small models (low, middle, high). Measure them again
before relying on them.
"""
from dataclasses import dataclass

SEC_PER_YEAR = 365 * 24 * 3600


@dataclass
class GPU:
    name: str
    tok_per_s: float   # generation throughput for Llama 3.1 8B q4 (output tokens per second)
    watts: float       # power draw under load
    price_usd: float   # purchase price (amortized over the useful life)


def single_miner_usd_per_mtok(g: GPU, elec_usd_kwh: float, amort_years: float, util: float) -> dict:
    """Cost for one miner to produce 1 M output tokens: electricity plus amortization."""
    elec = (g.watts / 1000 * elec_usd_kwh) / (g.tok_per_s * 3600)          # $/token
    life_tokens = amort_years * SEC_PER_YEAR * util * g.tok_per_s          # tokens over the useful life
    amort = g.price_usd / life_tokens                                      # $/token
    return {"elec": elec * 1e6, "amort": amort * 1e6, "total": (elec + amort) * 1e6}


def dendra_usd_per_mtok(single_total: float, k: int, miner_share: float, miner_margin: float) -> float:
    """Client price: k miners compute the same answer (redundancy), so the network cost is k x single;
    add the miner margin, then the protocol share (the miner receives miner_share of the fee, so the
    client pays the total divided by miner_share)."""
    net = single_total * k * (1 + miner_margin)
    return net / miner_share


# Hosted list prices for small models, $/M output tokens (a snapshot, low to high).
REFERENCE_PRICES = {
    "hosted API, low": 0.08,
    "hosted API, middle": 0.18,
    "hosted API, high": 0.60,
}


def main():
    elec, amort_years, util = 0.20, 3.0, 0.40
    k, miner_share, miner_margin = 3, 0.85, 0.20
    gpus = [GPU("24 GB high-end", 120, 400, 1800), GPU("8 GB mid-range", 45, 200, 500)]
    print(f"Assumptions: electricity ${elec}/kWh | amortization over {amort_years} years at {util:.0%} "
          f"utilization | k={k} (redundancy) | miner share {miner_share:.0%} | miner margin {miner_margin:.0%}\n")
    worst, best = 0.0, 1e9
    for g in gpus:
        c = single_miner_usd_per_mtok(g, elec, amort_years, util)
        d = dendra_usd_per_mtok(c["total"], k, miner_share, miner_margin)
        print(f"{g.name:14s}: 1 miner ${c['total']:.2f}/M (electricity ${c['elec']:.2f} + amortization ${c['amort']:.2f})"
              f"  ->  Dendra ${d:.2f}/M output")
        for name, p in REFERENCE_PRICES.items():
            mult = d / p
            worst, best = max(worst, mult), min(best, mult)
            print(f"                vs {name:18s} ${p:.2f}/M  ->  {mult:4.0f}x more expensive")
        print()
    print(f"=> Dendra is structurally {best:.0f}x to {worst:.0f}x more expensive per output token.")
    print("   Conclusion: the network cannot compete on price. At these rates it cannot undercut")
    print("   hosted APIs; what it can sell is a verifiable answer.")


if __name__ == "__main__":
    main()
