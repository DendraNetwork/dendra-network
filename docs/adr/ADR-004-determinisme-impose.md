# ADR-004 — Determinism enforced for the verifiable tier

**Status:** Accepted
**Implementation:** Partially implemented. `x/modelregistry` records the model identifier and a
declared hardware class (`hw_class`), and every commit carries `ModelId` and `WeightsHash`
(`chain/x/jobs/keeper/msg_server_commit.go`). Quantization and engine build are **not** part of
the on-chain registry; they are declared and enforced off-chain by the operator kit.

## Context

A fraud proof by re-execution ([ADR-003](ADR-003-fraud-proof-bisection.md)) only makes sense if
redoing the computation yields **the same result**. LLM inference on GPU is subject to
non-determinism: floating-point non-associativity, non-deterministic kernels, and variation across
quantization, engine and hardware.

## Decision

**Determinism is enforced** for the verifiable tier (Tier A,
[ADR-005](ADR-005-architecture-2-tiers.md)), through an **`x/modelregistry`** module. For each
admitted model the registry records:

- canonical hash of the **weights**;
- exact **quantization** (for example Q8_0, Q5_K_M);
- exact **engine and build version**;
- **greedy** decoding: temperature 0, fixed seed;
- declared **hardware class**.

## Consequences

- The exact strategy (exact determinism vs tolerance vs deterministic VM) is settled by empirical
  measurement ([ADR-OPEN](ADR-OPEN-strategie-determinisme.md)).
- The miner client must conform to this specification; any undervolt or power limit aggressive
  enough to introduce computation errors counts as non-determinism, hence as a slashable fault.
