# ADR-001 — Consensus is Tendermint PoS BFT; miners are not validators

**Status:** Accepted
**Implementation:** Implemented — the chain runs on Cosmos SDK / CometBFT (`chain/app/`).

## Context

The original specification described "PoUW" as if it were a consensus mechanism replacing a wasteful
proof-of-work. On Tendermint, consensus is **already** BFT proof-of-stake: there is no proof-of-work
to replace.

## Decision

Chain consensus is **Tendermint PoS BFT**. **Miners** (GPU providers running inference) are a role
**distinct** from **validators** (who order blocks). Consensus security is **never** coupled to GPU
ownership.

What is being built is a **verifiable compute marketplace** at the **application layer**, on top of
Tendermint; the consensus mechanism is taken as it is.

## Consequences

- BFT security stays independent of the network's aggregate GPU power.
- The reference domain becomes **optimistic verification with fraud proofs**, as used for rollups and
  for machine-learning inference, not proof-of-work.
- A compromised miner does not threaten block ordering; at worst it cheats on a job, which the fraud
  proof handles ([ADR-003](ADR-003-fraud-proof-bisection.md)).
