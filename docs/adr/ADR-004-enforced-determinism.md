# ADR-004 — Determinism enforced for the verifiable tier

**Status:** Accepted, in force in part. In force: the binding of a commit's `model_id` and `weights_hash` to
the registry, armed by the parameter `enforce_model_registry`; its value at the mainnet genesis is one of
the genesis parameters [ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md) leaves open. Not
built: quantization, engine build and greedy decoding as registry fields, which belong to the deterministic
tier of [ADR-035](ADR-035-deterministic-verification.md), later. Classified on 2026-10-16.
**Implementation:** Partial. When `enforce_model_registry` is true,
`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit` refuses a commit whose model is not registered
and active, or whose `weights_hash` differs from the `weights_sha256` the registry holds for that model;
with the parameter false it checks neither. Read it with `dendrad query jobs params -o json`: an absent
field is false, the proto3 zero. `x/modelregistry` records the model identifier and an indicative
`hw_class`; quantization and engine build are not registry fields.

## Context

A fraud proof by re-execution ([ADR-003](ADR-003-fraud-proof-bisection.md)) only makes sense if
redoing the computation yields **the same result**. LLM inference on GPU is subject to
non-determinism: floating-point non-associativity, non-deterministic kernels, and variation across
quantization, engine and hardware.

## Decision

**Determinism is enforced** for the verifiable tier (Tier A,
[ADR-005](ADR-005-two-tier-architecture.md)), through an **`x/modelregistry`** module. For each
admitted model the registry records:

- canonical hash of the **weights**;
- exact **quantization** (for example Q8_0, Q5_K_M);
- exact **engine and build version**;
- **greedy** decoding: temperature 0, fixed seed;
- declared **hardware class**.

## Consequences

- The exact strategy (exact determinism vs tolerance vs deterministic VM) is settled by empirical
  measurement ([ADR-OPEN](ADR-OPEN-determinism-strategy.md)).
- The miner client must conform to this specification; any undervolt or power limit aggressive
  enough to introduce computation errors counts as non-determinism, hence as a slashable fault.
