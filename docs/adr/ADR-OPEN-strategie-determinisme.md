# ADR-OPEN — Determinism strategy (S1 / S2 / S3 / S4)

**Status:** **SETTLED by the Phase 0 measurement → S2 (tiered).** The measurement is summarised below.
**Implementation:** The verdict is operationalised by [ADR-020](ADR-020-tier-verification-s2.md), and
subsequently by [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) and
[ADR-026](ADR-026-llm-juge-onchain.md), which replace the semantic decider. The S1 hash path is
**not** implemented as a separate tier; the deterministic-recompute idea is revisited by
[ADR-035](ADR-035-verification-deterministe.md).

## Context

The feasibility and cost of a fraud proof ([ADR-003](ADR-003-fraud-proof-bisection.md),
[ADR-004](ADR-004-determinisme-impose.md)) depend entirely on the **real level of determinism** of LLM
inference in greedy mode across different hardware. That quantity cannot be settled **on paper**: it must
be **measured**.

## The question to settle

*Do two honest miners running the "same" model in greedy mode produce outputs concordant enough to
verify by re-execution?*

## Options (the measurement picks one)

| Measured byte-for-byte identity | Strategy | Cost | Fraud proof |
|---|---|---|---|
| **99 % or more within a class** | **S1 — exact determinism scoped to a class** | low | re-execute, compare hashes |
| **90-99 %**, late divergences preserving meaning | **S2 — tolerance plus fraud-proof tie-break** | medium | embedding filter ([ADR-008](ADR-008-comparaison-semantique.md)) plus arbitration on a reference implementation and hardware |
| **below 90 %**, diffuse non-determinism | **S3 — deterministic VM (opML)** | high | deterministic execution plus byte-exact bisection |
| high-value jobs | **S4 — zkML** (opt-in) | very high | cryptographic proof, no re-execution — *roadmap* |

## Decision rule

The **99 % / 90 %** thresholds are **pre-registered BEFORE** the experiment is run (in the harness
configuration). That is what makes the measurement decisive rather than interpretable after the fact.

---

## Measurement (RTX 3070 Laptop, Llama 3.1 8B Q4_K_M, strict greedy)

Clean run, with weights pinned through the manifest digest, greedy at temperature 0, top-k 1, seed 0:

| | Byte-for-byte identity rate |
|---|---|
| Run 1 (240 pairs, 512 tokens) | **0.975** |
| Clean run 2 (60 pairs, 256 tokens, pinned weights) | **0.95** |

**By category (clean run)**: `code` **1.0** · `factual_short` **1.0** · `long_generation` **1.0** ·
`edge_adversarial` 0.93 · `reasoning` 0.83. **Late divergence** (first diverging character around
position 126).

Both runs fall inside the **90-99 % band**, with a **late, meaning-preserving** divergence concentrated
on **free-form** categories (reasoning, adversarial), while **structured output is byte-perfect**.

## Decision: **S2, tiered**

The non-determinism is **not diffuse** (so not S3): it is **targeted by category**. That is exploited:

1. **S1 (hash) for byte-stable outputs** — code, factual, structured (measured at 1.0): verification by
   **hash comparison**, nearly free.
2. **S2 (tolerance) for free-form** — reasoning and open generation: a **semantic** embedding filter
   ([ADR-008](ADR-008-comparaison-semantique.md)) plus a **bisection fraud-proof tie-break**
   ([ADR-003](ADR-003-fraud-proof-bisection.md)), on the two-tier architecture
   ([ADR-005](ADR-005-architecture-2-tiers.md)).
3. **S3 / S4 (opt-in) as escalation** — deterministic VM or zkML reserved for **high-value jobs** or
   unresolved disputes, **not** by default.

> The harness carries a conservative short-circuit to S3 that fires as soon as intra-machine identity is
> below 100 %. Its premise — that intra-machine execution is deterministic — is **refuted by the
> measurement**, so the decision follows the **pre-registered bands** (0.95 falls in 90-99 %, hence
> **S2**), not the short-circuit. A traced and owned choice.

## Consequences

- **Phase 1 unblocked**: the fraud proof is built on S2 (semantic filter plus bisection), with scoped S1
  for the deterministic categories.
- **The last blocker before the Cosmos port is lifted**: determinism is settled (this record) **and** the
  keeper API is proven end to end ([ADR-019](ADR-019-pont-inference-chaine.md)). The port from the Python
  state machine to Go `x/` modules can begin.
- To watch in Phase 1: a **cross-hardware** test (other GPUs) to confirm the band holds beyond a single
  card; it could **lower** the rate, which would narrow scoped S1 and widen S2. That measurement is the
  one [ADR-035](ADR-035-verification-deterministe.md) still depends on.

## Links

[ADR-003](ADR-003-fraud-proof-bisection.md) · [ADR-004](ADR-004-determinisme-impose.md) ·
[ADR-005](ADR-005-architecture-2-tiers.md) · [ADR-008](ADR-008-comparaison-semantique.md) ·
[ADR-019](ADR-019-pont-inference-chaine.md) · [ADR-020](ADR-020-tier-verification-s2.md) ·
[ADR-035](ADR-035-verification-deterministe.md)
