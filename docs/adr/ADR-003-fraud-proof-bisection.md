# ADR-003 — Optimistic verification plus fraud proof by bisection

**Status:** Accepted
**Implementation:** Partially implemented. The bisection primitive exists
(`chain/x/jobs/keeper/bisection.go`); it is **not** the settlement path in production. The live
verification path is the sampled optimistic audit of
[ADR-025](ADR-025-optimistic-verification-by-sampling.md), judged by the committee of
[ADR-026](ADR-026-onchain-llm-judge.md). Bisection is retained for a future deterministic
greedy-decode tier ([ADR-035](ADR-035-deterministic-verification.md)).

## Context

The original specification had **no fraud-proof mechanism**: slashing was pronounced with no
objective way to establish cheating. Voting by peer agreement rewards **conformity**, not
correctness — a beauty contest that degenerates into a race to the smallest model.

## Decision

**Optimistic** verification: an output is presumed correct unless challenged. Fraud is established
by **deterministic re-execution** plus **interactive bisection** (the interactive fraud-proof game of
optimistic rollups, applied to a model's execution).

## Rationale

Interactive bisection is the only way to establish fraud objectively at **low on-chain cost**:
instead of replaying the whole computation, challenger and defender converge by dichotomy on the
**first diverging step**, and only that micro-step is arbitrated by the chain.

## Consequences

- Replaces Levenshtein ([ADR-002](ADR-002-abandon-levenshtein.md)) as the source of truth.
- Requires computational determinism ([ADR-004](ADR-004-enforced-determinism.md)) and data
  availability ([ADR-006](ADR-006-data-availability.md)).
- Feasibility depends on the measured level of determinism
  ([ADR-OPEN](ADR-OPEN-determinism-strategy.md)).
- Must be paired with a countermeasure to the verifier's dilemma
  ([ADR-007](ADR-007-verifier-dilemma.md)).
