# ADR-023 — On-chain Reserve emission module (`x/emission`)

**Status:** Accepted — implemented.
**Implementation:** Implemented and live. `x/emission` releases the Reserve on a decreasing curve and
credits the pools on-chain; zero minting is **structural** (`x/mint` is not registered). Deployment
and the invariants proven on-chain are recorded in
[ADR-024](ADR-024-couche-economique-reelle-onchain.md).

> **Correction (2026-07-31) — the "22 % per year" curve below is a projection, not a measurement.**
> It converts epochs into years at an *assumed* block interval of 1 s, documented as a **target** the
> consensus does not guarantee (`chain/x/chaintime/chaintime.go`); the live interval measures
> **~5.03 s**. The launch genesis ships `reserve_release_bps = 2` per 86,400-block epoch. What this
> record specifies — the module, its decreasing release from a pre-allocated Reserve, and structural
> zero minting — is unaffected; only the yearly framing is.

> **Correction (2026-08-01) — this module is INERT on the launched network, and the reason is in the
> genesis, not in the code.** Measured first-hand on the live chain: `app_state.emission.state` is
> `null`, so `InitGenesis` takes the bootstrap branch and writes a **Reserve counter of 3.3 M DNDR**
> — while the `emission` module account holds **0 udndr**. The 3.3 M were allocated to an **ordinary
> genesis account**, and minting is structurally impossible, so the counter is backed by no coin the
> release path can reach. Each epoch therefore computes a non-zero security slice, attempts the
> transfer, fails, and **skips without loss** — no halt (`x/emission/keeper/emission_runepoch_test.go`
> pins that best-effort behaviour), no release, and an `Info` log line as the only trace.
>
> **Correction (2026-08-16) — CLEARED, and not by a transaction.** The chain above was destroyed and
> relaunched on a new genesis that credits the 3.3 M **directly to the `emission` module account**;
> measured on the live chain, that account holds `3300000000000 udndr` against a total supply of
> `10000000000000`. A transfer could never have fixed it — a third of the supply does not move into a
> module account by transaction — so the defect was fixed where it is decided, and the node now
> **refuses to boot** on a Reserve counter no module balance backs. The paragraph above describes the
> chain of 2026-08-01 and is kept as the record of what was wrong.
>
> The gap is between this record and the **launch kit**: `epoch.go` states that an unfunded module
> account is "a genesis edge case that the launch kit rules out", and the shipped kit does not rule it
> out. **What this record specifies is unchanged and remains correct as a design.** What is not yet
> true is its deployment: any statement that the Reserve "is released by `x/emission`" describes the
> mechanism, not the running network, until the module account is funded.
>
> Funding it is a **one-way door** — coins entering a module account leave only through code paths,
> and the sole outbound flow here is security → `fee_collector` — so it is recorded here as an open
> decision, not applied. The `emission` account is *not* in `blockAccAddrs`
> (`chain/app/app_config.go`), so an ordinary transfer would suffice; that fact is read from the
> source, not yet exercised on the chain.

Answers the gap in which the v5 tokenomics (fixed supply, decreasing emission from the Reserve) lived
**only in Python**, while the chain merely settled escrow fees. This record specifies the module that
makes **v5 genuinely on-chain**.

## Context

v5 ([ADR-021](ADR-021-tokenomics-v5-native.md)): **fixed 10 M supply, ZERO minting**. Rewards come out
of a **pre-allocated Reserve** (3.3 M DNDR at genesis) released on a **decreasing curve (22 % per year
of the remainder)**, split into **three flows**: work (demand-gated at 1.5×), availability, security.
Before this module, none of that existed on-chain, so the v5 narrative was not held.

## Decision

A module **`x/emission`** (depending on `bank`) which, **per EPOCH** (in `BeginBlock`, every
`epoch_blocks`):

1. reads the **remaining Reserve** (module state) and the epoch's **non-recoverable demand** (fees
   burned, treasury, development — measured on-chain);
2. computes the release through the **deterministic core** `emission.EpochRelease` (integer udndr and
   basis points, an exact mirror of the reference simulation): `release = 22 % × reserve`; split
   `50/20/30`; **work capped** at `1.5 × non-recoverable demand`, with the unused part **staying in
   the Reserve** — the anti-self-dealing property of
   [ADR-017](ADR-017-anti-sybil-demande.md);
3. **credits the three pools** (work, availability, security) through `bank` from the Reserve account
   and **decrements the Reserve** — **never minting**: only pre-allocated funds move.

### State

`reserve: uint64` (remaining udndr) · `pools{work, avail, security}: uint64` · governable `Params`.

### Parameters (genesis, basis points)

`reserve_release_bps=2200` · `work_split_bps=5000` · `avail_split_bps=2000` (security takes the
remainder) · `work_gate_bps=15000` · `epoch_blocks` (about one year of blocks at roughly one second).

### Determinism

**Everything in integers** (no floating point), so it reproduces across validators. The exact
remainder goes to security, which avoids rounding loss.

## Invariants (tested)

- **Anti-mint**: `Σ released ≤ initial Reserve`, which guarantees **supply at or below 10 M**.
- Reserve **decreasing** (asymptotic, never negative).
- **Conservation**: `work + avail + security = released`.
- Oracle: a unit test of the core plus an integration test replaying the reference simulation year by
  year.

## Consequences

- v5 becomes **genuinely on-chain**; the "decreasing emission" pillar is no longer Python-only.
- Distribution of the pools to miners and validators hooks into the existing paths (work through
  settlement and payout; availability through
  [ADR-022](ADR-022-proof-of-useful-availability.md); security through staking).
- The reference simulation remains the **specification** and the **test oracle**.

## Out of scope (later iterations)

- **APR floor** (drawing slightly more from the Reserve if validator APR falls below 5 %) and the
  **fee-funded transition**: modelled in the reference simulation, to be ported after the core. The
  APR floor was subsequently **withdrawn as a claim** by
  [ADR-029](ADR-029-endgame-securite-emission-long-terme.md).
- Precise on-chain measurement of non-recoverable demand (collecting non-recoverable fees per epoch).
- **Team vesting** and a **free-tier sub-pool**, to be integrated at the same place.
