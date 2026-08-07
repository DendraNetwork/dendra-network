# ADR-014 — L1 framework: Cosmos SDK, sovereign independent chain

**Status:** Accepted. Makes [ADR-001](ADR-001-consensus-tendermint.md) concrete.
**Implementation:** Implemented — `chain/` is a Cosmos SDK / CometBFT chain with custom modules
`x/jobs`, `x/modelregistry`, `x/emission`, `x/chaintime`.

## Context

Choice of framework for building the L1. Requirements: **sovereignty** (our own token, validators and
rules), **custom modules** (`x/modelregistry`, `x/jobs`, `x/miners`), **BFT PoS** with instant
finality, and **miners distinct from validators**. Computation (inference, MPC) is **off-chain**; the
chain only does coordination, commitments and staking, which keeps the on-chain surface small.

## Decision

**Cosmos SDK plus CometBFT.** The chain is **sovereign and independent**:

- **Token, validators, genesis, governance and rules are entirely ours.**
- **IBC is opt-in**: interconnection with other chains is **optional**; disabled, the chain is fully
  isolated.
- **No Interchain Security**: we do not rent security from the Cosmos Hub, we run **our own validator
  set** ([ADR-001](ADR-001-consensus-tendermint.md)).
- **No dependency on the Cosmos Hub or ATOM.** "Cosmos" here means a **framework** and an ecosystem of
  **independent** chains, not a network that governs us.

## Why, against the alternatives

- **Substrate / Polkadot**: Rust plus forkless upgrades (a real advantage), but interoperability and
  ecosystem are Polkadot-centred, with less low-level control.
- **Rollup (OP Stack, Orbit, Celestia)**: inherits security but **depends** on an L1 or a DA layer,
  which dilutes sovereignty.
- **Bittensor subnet**: a shortcut, but it means adopting its mechanics (TAO, Yuma) and reduced
  sovereignty.
- **Contracts on an existing chain**: no chain of our own.
- Cosmos is the reference app-chain framework (dYdX, Celestia, Osmosis), well suited to a sovereign L1
  with custom modules.

## Consequences

- We **bootstrap our own security** (validator recruitment, staking); no borrowed security.
- **Accepted limitation**: core upgrades go through **hard fork plus governance vote**, where
  Substrate offers forkless upgrades. Acceptable.
- Target modules in **Go**: `x/modelregistry`, `x/jobs`, `x/miners` (staking, slashing, reputation).
- IBC can be enabled later to bridge assets without changing anything about sovereignty.

## Implementation plan

1. **Prototype the module logic in Python** (a testable state machine, no Go toolchain required) as an
   exact blueprint of the state transitions.
2. **Port to Go** (chain and module scaffolding) once the Go toolchain is available. The real chain
   lives in `chain/`.

## Links

[ADR-001](ADR-001-consensus-tendermint.md) · [ADR-013](ADR-013-mineur-exploite-un-node.md)
