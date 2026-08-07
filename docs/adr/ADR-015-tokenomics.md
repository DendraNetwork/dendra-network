# ADR-015 — Tokenomics (DNDR), v4 model

> **SUPERSEDED by [ADR-021](ADR-021-tokenomics-v5-native.md).** The **v5** model (fixed 10 M supply,
> **zero inflation and zero minting**, emission from a decreasing released Reserve) replaces the v4
> tokenomics described below (dynamic inflation plus deflationary cycles). Kept for the record —
> **do not implement**.

**Status:** Superseded by ADR-021
**Implementation:** Not implemented, and must not be. On-chain minting is structurally zero
(`x/mint` is not registered); the live economic layer follows
[ADR-023](ADR-023-emission-reserve-onchain.md) and
[ADR-024](ADR-024-couche-economique-reelle-onchain.md).

## Context

The token powers **staking** (validators and delegators), **miner bonds**, **fees** (transactions and
inference), **governance**, and **payment** for inference. The monetary model has to be fixed.

## Decisions

1. **Hybrid supply (decay plus tail).** Strong inflation at launch to **bootstrap security and attract
   miners and validators**, decaying towards a **residual inflation** that funds long-term security.
   Default: **12 % in year 1 falling to a 2 % tail** (×0.8 per year).
2. **Fees, no burn.** All fees (inference and transactions) go to **validators and delegators** and to
   the **treasury**; nothing is destroyed. More staking revenue, no deflationary pressure.
3. **Balanced genesis allocation** (10 M at genesis): community and airdrop 25 %, DAO treasury 20 %,
   network reward reserve 15 %, team 18 % (4-year vesting, 1-year cliff), investors 15 % (vesting),
   foundation 7 %.
4. **Target staked ratio around 60 %**; miner share around **85 %** of a job price (15 % protocol, of
   which half to validators and half to the treasury).

## Consequences (ten-year projection from the simulator)

- Supply: **10 M growing to about 16.83 M**; inflation 12 % falling to 2 %.
- **Staking APR**: around **18 % in year 1** (emission-funded), a trough near 7.5 % in years 6-7, then
  back to roughly **16 % by year 10**, carried by **fees** as usage grows. That **emission-to-fee
  transition** is what makes security durable.
- Treasury: grows continuously (no burn), funding ecosystem and development.

## Honest limitation of the no-burn choice

Without a burn, **value capture** by the token is weaker than in a deflationary model (EIP-1559
style). It rests on **staking demand** (security yield), **utility** (holding to stake, bond or pay),
**governance**, and possible **treasury buybacks** (a governance decision). The sell pressure from
early emission is the thing to watch.

## Notes

- **Every figure is a governance parameter**, not a constant. The usage scenario (jobs per day,
  growth) is an **assumption**: the projections depend on adoption.
- To be calibrated more finely by an economic spike (game theory plus Monte-Carlo simulation).

## Evolution

1. **Training reward** added: miners are paid for inference **and** for contributions to training,
   paid per validated compute (bond plus slash if fake), funded from the **reward Reserve** (15 %).
2. **Fee burn**: moves from "no burn" to a **recommended governable option** (`fee_burn_bps`, 30 %
   suggested). 0 % preserves the original model. The value-capture versus treasury-revenue trade-off
   is analysed above.

## Links

[ADR-021](ADR-021-tokenomics-v5-native.md) · [ADR-012](ADR-012-sanctions-fuites.md) ·
[ADR-009](ADR-009-anti-collusion-vrf.md) · [ADR-013](ADR-013-mineur-exploite-un-node.md)
