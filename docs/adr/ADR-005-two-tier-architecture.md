# ADR-005 — Two-tier architecture

**Status:** Accepted — reconfigured by [ADR-011](ADR-011-two-confidentiality-modes.md), which
re-casts the two tiers as two **confidentiality modes**.
**Implementation:** Decided, not implemented as two distinct tiers. The chain ships a single
verifiable path; the streaming best-effort tier has no on-chain support.

## Context

An irreducible tension between three requirements: **verifiability** (determinism plus
commit-reveal), **quality and UX** (large models, token-by-token streaming) and **latency**. Real-time
streaming is **incompatible** with a synchronous commit-reveal scheme.

## Decision

Two distinct service tiers:

- **Tier A — verifiable / deterministic.** Greedy decoding per
  [ADR-004](ADR-004-enforced-determinism.md), commit-reveal, slashing by fraud proof
  ([ADR-003](ADR-003-fraud-proof-bisection.md)). For jobs whose correctness must be provable.
- **Tier B — best-effort / streaming.** Interactive chat UX, reputation rather than byte-exact slashing,
  **asynchronous settlement**. For interactive jobs where latency dominates.

## Consequences

- Resolves the streaming/commit-reveal contradiction by confining it to Tier B.
- Two distinct economic and security models coexist on the same chain.
- Tier B relies on reputation and deferred settlement; Tier A on escrow plus fraud proof.
