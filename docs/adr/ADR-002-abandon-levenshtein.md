# ADR-002 — Dropping Levenshtein distance

**Status:** Accepted
**Implementation:** Implemented — no edit-distance comparison exists anywhere in the chain code.

## Context

The original specification checked output conformance using **Levenshtein distance** (number of
character edits).

## Decision

**Levenshtein is dropped entirely** as a verification criterion.

## Rationale

- Levenshtein measures **character edits, not meaning**. Two semantically identical summaries can be
  far apart under it, which would slash honest miners.
- It is a **useless middle ground**: too lax for exact matching, pointless against byte-for-byte
  identity.
- Its **on-chain cost** is O(n×m), which is a **denial-of-service vector**.

## Consequences

- Replaced by deterministic re-execution plus fraud proof
  ([ADR-003](ADR-003-fraud-proof-bisection.md)).
- Where a semantic comparison is needed, it is embedding cosine, **never** Levenshtein
  ([ADR-008](ADR-008-comparaison-semantique.md)).
