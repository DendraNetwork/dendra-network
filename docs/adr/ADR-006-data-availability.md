# ADR-006 — Data availability

> **Reconfigured by [ADR-011](ADR-011-deux-modes-confidentialite.md).** This record assumes prompt
> and output are published (for example on IPFS) so they can be retrieved — which is **incompatible**
> with the confidentiality of ADR-011 (encrypted commitments, **nothing in the clear**). For the
> private modes, availability covers **ciphertexts and commitments**, never content.

**Status:** Accepted, partially reconfigured by ADR-011
**Implementation:** Not implemented. There is no CID binding, no pinning proof and no
availability-conditioned settlement anywhere in the chain code. Availability of a *provider* — a
different property — is covered by [ADR-022](ADR-022-proof-of-useful-availability.md).

## Context

A flaw in the original specification: a miner can **post the hash** of an output without ever
**publishing the output**. It then gets paid while nobody can verify or challenge — nothing to
replay, nothing to compare.

## Decision

- **Reveal-to-CID binding**: the reveal ties the on-chain hash to the IPFS CID of the full output.
- **IPFS pinning** of the output for at least the challenge window.
- **Availability proof gating payment**: no proof that the data is retrievable, no escrow
  settlement.

## Consequences

- Prevents data withholding.
- The miner client must upload and pin the output, with a pinning SLA at least as long as the
  challenge window ([ADR-003](ADR-003-fraud-proof-bisection.md)).
- No bulk text on-chain: only hashes and CIDs live there, the data stays on IPFS.
