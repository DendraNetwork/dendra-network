# ADR-005 — Two-tier architecture

> **Superseded in its form by [ADR-011](ADR-011-two-confidentiality-modes.md) (classified 2026-10-16).** The
> two tiers became two confidentiality modes. Tier B as written here, streaming with reputation in place of
> slashing and asynchronous settlement, is carried forward by no later record. This record stays for its
> reasoning.

**Status:** Accepted, superseded in its form by [ADR-011](ADR-011-two-confidentiality-modes.md) (banner
above). In force: one verifiable job path, Mode A of ADR-011 under the sampled audit of
[ADR-025](ADR-025-optimistic-verification-by-sampling.md). Mode B follows the status of ADR-011.
**Implementation:** Not implemented as two tiers: the chain has one job path (`chain/x/jobs`).

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
