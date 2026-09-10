# ADR-021 — Tokenomics v5: native model (fixed supply, Reserve emission)

**Status:** Accepted. Supersedes [ADR-015](ADR-015-tokenomics.md) as the adopted model. Executable
specification: `tokenomics/tokenomics_v5.py`.
**Implementation:** Implemented for the monetary core. Minting is structurally zero (`x/mint` is not
registered) and the decreasing Reserve release runs on-chain
([ADR-023](ADR-023-emission-reserve-onchain.md),
[ADR-024](ADR-024-couche-economique-reelle-onchain.md)). **Not implemented:** the availability payout
(the pool accrues but is not distributed, see
[ADR-022](ADR-022-proof-of-useful-availability.md)) and the **APR floor**, which
[ADR-029](ADR-029-endgame-securite-emission-long-terme.md) withdraws as a claim.

> **Correction (2026-07-31) — the "22 % per year" release rate below is a projection, not a
> measurement.** It converts epochs into years at an *assumed* block interval of 1 s, which
> `chain/x/chaintime/chaintime.go` documents as a **target** the consensus does not guarantee. The
> live interval measures **~5.03 s**. The release rate is a governable on-chain parameter and the
> launch genesis ships `reserve_release_bps = 2` per 86,400-block epoch — read the live value with
> `dendrad query emission params -o json`. Durations are stated in **epochs** outside this historical
> text; the *shape* of the curve this record adopts (geometric, decreasing, no minting) is unaffected.

## Context

Tokenomics v1 through v4 rested on the default Cosmos paradigm: **perpetual inflation** (minting) to
pay for security. The economy was re-thought **for a useful inference network** rather than a generic
PoS chain — rewarding **real work** and **availability**, without endless dilution.

## Decision

1. **Supply capped at 10,000,000 DNDR. No inflation, no minting.** Rewards come out of a
   **pre-allocated Reserve (33 %)** released along a **decreasing** curve (22 % per year of the
   remainder). No token is ever created, only pre-allocated tokens released, so supply always stays at
   or below 10 M.
2. **Three verified, bounded flows**: **Work** (per job, demand-gated at 1.5×), **Availability**
   (random challenge, slashable), **Security** (validators: a decreasing Reserve budget plus a share
   of fees).
3. **Gentle deflation** (5 % fee burn): DNDR stays **liquid**, a payment token rather than a
   hoarding asset. Supply around 8.1 M at ten years, scarcity around ×1.23.
4. **Security shifts towards fees** (the emission-to-usage transition).
5. **Development**: 5 % at genesis plus **20 % of the protocol cut** (inference revenue, governable) —
   **not** a tax on every transaction.
6. **Accepted boundary**: consensus remains CometBFT/PoS (node plumbing). "Outside Cosmos" refers to
   the **economic layer**, which is rebuilt from scratch.

## Consequences

- **Chain**: `mint` inflation set to **0**. The **Reserve emission module is coded and deployed**
  ([ADR-023](ADR-023-emission-reserve-onchain.md) then
  [ADR-024](ADR-024-couche-economique-reelle-onchain.md); `x/emission`, decreasing curve plus three
  flows). Remaining: governable parameters and real distribution of the pools.
- **Verified**: a Monte-Carlo run against v5 (4000 runs) reports v5 as **robust** — supply at or below
  10 M in 100 % of runs, Reserve non-negative in 100 %, APR floor in 100 %, deflation in 100 %,
  fee-funded in 89.5 %. The APR-floor line is the one
  [ADR-029](ADR-029-endgame-securite-emission-long-terme.md) later withdraws as an unimplemented
  claim, and the "robust" label on it is a **horizon artefact** (ten years is too short to drain the
  Reserve).
- v1 through v4 are kept as history; v5 is canonical.
