# ADR-006 — Data availability

> **Reconfigured by [ADR-011](ADR-011-two-confidentiality-modes.md).** This record assumes prompt
> and output are published (for example in a content-addressed store) so they can be retrieved, which is **incompatible**
> with the confidentiality of ADR-011 (encrypted commitments, **nothing in the clear**). For the
> private modes, availability covers **ciphertexts and commitments**, never content.

**Status:** Accepted, partially reconfigured by ADR-011 (box above). Not in force: reveal-to-CID binding,
pinning, and payment gated on a proof that the data is retrievable. What an audit reads is the reveal the
primary seals for the jury anchored on its job, on the relay
([ADR-057](ADR-057-judge-anchor-check-and-single-judge-model.md)). A reveal is not bound to the delivered
answer: ADR-057 records that the binding needs a hash of the answer in the commit, a new consensus epoch,
and is not designed. Classified on 2026-10-16: open, with that hash; whether it enters the mainnet genesis
or a later epoch is undecided.
**Implementation:** Not implemented. The chain code has no content identifier, pinning proof or
availability-gated settlement (`chain/x/jobs`). Availability of a provider, a different property, is the
challenge of [ADR-022](ADR-022-proof-of-useful-availability.md).

## Context

A flaw in the original specification: a miner can **post the hash** of an output without ever
**publishing the output**. It then gets paid while nobody can verify or challenge — nothing to
replay, nothing to compare.

## Decision

- **Reveal-to-CID binding**: the reveal ties the on-chain hash to the content identifier (CID) of the
  full output in a content-addressed store.
- **Pinning** of the output in that store for at least the challenge window.
- **Availability proof gating payment**: no proof that the data is retrievable, no escrow
  settlement.

## Consequences

- Prevents data withholding.
- The miner client must upload and pin the output, with a pinning SLA at least as long as the
  challenge window ([ADR-003](ADR-003-fraud-proof-bisection.md)).
- No bulk text on-chain: only hashes and CIDs live there, and the data stays in the content-addressed
  store.
