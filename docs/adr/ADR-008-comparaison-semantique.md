# ADR-008 — Semantic comparison is embedding cosine

**Status:** Accepted — **superseded as a verdict mechanism** by
[ADR-026](ADR-026-llm-juge-onchain.md). Cosine similarity was measured to accept both a keyword salad
and a fluent false statement; it is not a judge. It survives only as a cheap filter.
**Implementation:** Implemented as a filter (`chain/x/jobs/keeper/msg_server_verify_semantic.go`).

## Context

Some cases — notably the S2 strategy ([ADR-OPEN](ADR-OPEN-strategie-determinisme.md)) — require
judging whether two outputs that are not byte-identical "say the same thing". Levenshtein is
disqualified ([ADR-002](ADR-002-abandon-levenshtein.md)).

## Decision

Semantic comparison is done by **canonicalisation plus embedding cosine**, using an embedding model
**registered** in `x/modelregistry`. Strict constraints:

- **never Levenshtein**;
- **only as a heuristic filter** — never the sole decider of a slash.

## Rationale

Embedding cosine captures **meaning**, unlike Levenshtein. It remains probabilistic: it filters and
routes, while the final arbitration of a slash rests on bisection against a reference implementation
and hardware ([ADR-003](ADR-003-fraud-proof-bisection.md)).

## Consequences

- The embedding model must itself be deterministic and versioned.
- A semantic disagreement triggers an arbitration procedure, not a direct slash.
