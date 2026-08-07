# Architecture Decision Records — Dendra

The register of architecture decisions for Dendra, an L1 for verifiable AI inference. Each record is
**firm** unless new evidence arrives.

## How to read this index

Every record carries two fields in its header, and they mean different things:

- **Status** — the state of the *decision*: `Accepted`, `Proposed`, `Deferred`, `Rejected`, `Superseded
  by ADR-NNN`, or `Settled → ADR-NNN` for the two open questions that were later resolved elsewhere.
- **Implementation** — the state of the *code*, which is not the same thing. A decision can be accepted
  and unbuilt. Four labels form the vocabulary; a row narrows one of them (`Partial — …`, or a named
  scope) when only part of a record is built, and says so rather than rounding up:
  - **Implemented** — the mechanism exists and, where it is a guard, is armed in the shipped genesis.
  - **Implemented, dormant** — the code exists but a parameter in the shipped genesis keeps it from
    firing. It must not be described as an active guard.
  - **Decided, not implemented** — the decision stands; no code implements it.
  - **Not implemented** — including decisions deliberately not built.

  A fifth state exists and is worse than dormant: **inert** — the parameter is posted and no code
  reads it, so raising it by governance would pass and change nothing. Where a record is in that
  state its row says `inert`, never `dormant`.

**A guard that is not applied is never announced as applied.** Where a record's body describes a
mechanism more ambitious than what runs, the header says so and the body carries the correction.

File names are kept stable because they are referenced from outside this directory; the content is in
English.

## Index

| # | Title | Status | Implementation |
|---|---|---|---|
| [ADR-001](ADR-001-consensus-tendermint.md) | Consensus is Tendermint PoS BFT; miners are not validators | Accepted | Implemented |
| [ADR-002](ADR-002-abandon-levenshtein.md) | Dropping Levenshtein distance | Accepted | Implemented |
| [ADR-003](ADR-003-fraud-proof-bisection.md) | Optimistic verification plus fraud proof by bisection | Accepted | Partial — bisection exists, is not the settlement path |
| [ADR-004](ADR-004-determinisme-impose.md) | Determinism enforced for the verifiable tier (`x/modelregistry`) | Accepted | Partial — registry binding exists, dormant in the shipped genesis |
| [ADR-005](ADR-005-architecture-2-tiers.md) | Two-tier architecture (verifiable / best-effort) | Accepted, reconfigured by ADR-011 | Decided, not implemented as two tiers |
| [ADR-006](ADR-006-data-availability.md) | Data availability (reveal-to-CID binding, pinning) | Accepted, partially reconfigured by ADR-011 | Not implemented |
| [ADR-007](ADR-007-anti-dilemme-verificateur.md) | Countering the verifier's dilemma (jackpot, random re-verification) | Accepted | Partial — random audit yes, jackpot not implemented |
| [ADR-008](ADR-008-comparaison-semantique.md) | Semantic comparison is embedding cosine (filter only) | Accepted, superseded as a verdict by ADR-026 | Implemented as a filter |
| [ADR-009](ADR-009-anti-collusion-vrf.md) | Anti-collusion: VRF assignment and slashing parameters | Accepted | Partial — VRF draw yes, the stake-versus-gain bound is not enforced |
| [ADR-010](ADR-010-gateway-permissionless.md) | Permissionless gateway, no key custody | Accepted | Implemented |
| [ADR-OPEN](ADR-OPEN-strategie-determinisme.md) | Determinism strategy S1/S2/S3/S4 | Settled → tiered S2 | Operationalised by ADR-020 then ADR-025 |
| [ADR-OPEN-PRIVACY](ADR-OPEN-PRIVACY-confidentialite.md) | Confidentiality and anonymity (justification) | Settled → ADR-011 | See ADR-011 |
| [ADR-011](ADR-011-deux-modes-confidentialite.md) | Two confidentiality modes (Standard / Confidential MPC) | Accepted | Partial — Mode A implemented, Mode B not implemented |
| [ADR-012](ADR-012-sanctions-fuites.md) | Sanctions for leaks and confidentiality violations | Accepted | Decided, not implemented |
| [ADR-013](ADR-013-mineur-exploite-un-node.md) | Every miner runs a full/relay node, not a validator | Accepted | Operational requirement, not enforced on-chain |
| [ADR-014](ADR-014-framework-l1-cosmos-souverain.md) | L1 framework: Cosmos SDK, sovereign independent chain | Accepted | Implemented |
| [ADR-015](ADR-015-tokenomics.md) | Tokenomics (DNDR) — v4 model | **Superseded by ADR-021** | Not implemented, and must not be |
| [ADR-016](ADR-016-nom-dendra.md) | Project name: Dendra (DNDR) | Accepted | Implemented |
| [ADR-017](ADR-017-anti-sybil-demande.md) | Anti-Sybil: subsidy gated on non-recoverable demand | Accepted | Implemented |
| [ADR-018](ADR-018-reglement-onchain-v4.md) | On-chain settlement v4 (cut split, vesting, BME buyback) | **Superseded by ADR-021** | Prototype only |
| [ADR-019](ADR-019-pont-inference-chaine.md) | Inference-to-chain bridge | Accepted | Implemented in the prototype; superseded in production by the chain modules |
| [ADR-020](ADR-020-tier-verification-s2.md) | On-chain verification tier (tiered S2: hash / semantic, slash and jackpot) | Accepted, partly superseded by ADR-025 and ADR-026 | Superseded in place; the jackpot is not implemented |
| [ADR-021](ADR-021-tokenomics-v5-native.md) | Tokenomics v5 (fixed 10 M supply, zero inflation, Reserve emission) | **Accepted — supersedes ADR-015 and ADR-018** | Implemented for the monetary core |
| [ADR-022](ADR-022-proof-of-useful-availability.md) | Availability challenge (proof of useful availability, slashable) | Accepted (design ratified) | **Implemented, dormant in the shipped genesis** |
| [ADR-023](ADR-023-emission-reserve-onchain.md) | On-chain Reserve emission module (`x/emission`) | Accepted | Implemented |
| [ADR-024](ADR-024-couche-economique-reelle-onchain.md) | Real economic layer on-chain: deployment and verified invariants | Accepted | Implemented, verified live |
| [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) | Optimistic verification by VRF sampling (k=1, sampled audit, hard slash) | Accepted — extends ADR-020 | Implemented and armed; under `committee_seed_source = 1` a draw happens **only** on a decentralised seed, and due jobs are **deferred** at any height that has none (`chain/x/jobs/keeper/audit_sampling.go:63`) |
| [ADR-026](ADR-026-llm-juge-onchain.md) | Language-model judge on-chain for the audited sample | Adopted | Implemented on-chain; the judge itself runs off-chain and is **not** constrained by consensus |
| [ADR-027](ADR-027-decisions-execution.md) | Execution decisions (disputed index, batch items, judge model, TEE tier) | Accepted; D4 reversed by ADR-031 | D1-D3 implemented; D4 inert; D5 reserved and dormant |
| [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) | Anti-evasion: provisional optimistic payment (clawback, appeal, quorum) | Accepted — corrects ADR-025 | Implemented with documented deviations; the silence penalty is **inert** |
| [ADR-029](ADR-029-endgame-securite-emission-long-terme.md) | Security endgame and long-term emission (fixed cap kept, cliff documented) | Accepted / ratified | Implemented as a non-action; the APR floor claim is withdrawn |
| [ADR-030](ADR-030-verification-image.md) | Image verification ("verified effort") — gated behind the text equivalent | **Proposed — deferred** | Not implemented |
| [ADR-031](ADR-031-allow-list-modeles-juges.md) | On-chain enforcement of judge-model heterogeneity | **Rejected as specified; allow-list form deferred** | Nothing to implement; the reserved field is **inert** |
| [ADR-032](ADR-032-comite-audit-ancre.md) | Audit committee anchored on-chain (the tally counts only drawn jurors) | **Accepted** — corrects a critical flaw in ADR-028; amendment 1 included | Implemented and deployed |
| [ADR-033](ADR-033-comite-readjudication-ancre.md) | Anchored re-adjudication committee and bond semantics (only an adjudication forfeits) | **Accepted** — corrects a recurrence of ADR-032 | Implemented |
| [ADR-034](ADR-034-vitalite-du-mineur.md) | Miner vitality: "demonstrably alive" rather than "registered" | **Accepted** | Fully implemented |
| [ADR-035](ADR-035-verification-deterministe.md) | Deterministic verification by recomputation (exact tier, no judge) | **Proposed — deferred** behind a cross-GPU measurement | Not implemented, deliberately |
| [ADR-036](ADR-036-vitalite-de-jure.md) | Juror vitality: a seat that cannot vote is neither drawn nor immobilising | **Accepted** | Implemented; **consensus-breaking — it applies from the genesis it is built into, never to a chain already running** |
| [ADR-037](ADR-037-gel-du-vivier-de-tirage.md) | Freezing the drawing pool (closes identifier grinding) | **Accepted** | Implemented; **consensus-breaking — it applies from the genesis it is built into, never to a chain already running** |
| [ADR-038](ADR-038-incitation-des-jures.md) | Juror incentive: none is added, because the verdict alphabet has no "present and undecided" | **Accepted — the decision is a non-action** | Implemented as a **guarded absence** (no juror payment, no juror penalty, `silence_slash_bps` inert); **not consensus-breaking** |
| [ADR-039](ADR-039-reconnaissance-contribution-testnet.md) | Testnet contribution is recorded and snapshotted durably, with the intention of recognising it at mainnet — an intention, never an entitlement | **Decided by the owner** | Texts corrected; snapshot triplet, payee journal and index rework **decided, not implemented**; **not consensus-breaking** |

## Reading paths

- **Verification and slashing** (the core): [ADR-003](ADR-003-fraud-proof-bisection.md) →
  [ADR-020](ADR-020-tier-verification-s2.md) → [ADR-025](ADR-025-verification-optimiste-echantillonnage.md)
  → [ADR-026](ADR-026-llm-juge-onchain.md) → [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) →
  [ADR-032](ADR-032-comite-audit-ancre.md) → [ADR-033](ADR-033-comite-readjudication-ancre.md) →
  [ADR-034](ADR-034-vitalite-du-mineur.md) → [ADR-036](ADR-036-vitalite-de-jure.md) →
  [ADR-037](ADR-037-gel-du-vivier-de-tirage.md) → [ADR-038](ADR-038-incitation-des-jures.md).
- **Economics**: [ADR-021](ADR-021-tokenomics-v5-native.md) →
  [ADR-023](ADR-023-emission-reserve-onchain.md) → [ADR-024](ADR-024-couche-economique-reelle-onchain.md)
  → [ADR-029](ADR-029-endgame-securite-emission-long-terme.md), with
  [ADR-017](ADR-017-anti-sybil-demande.md) for the anti-Sybil gate.
- **Confidentiality**: [ADR-OPEN-PRIVACY](ADR-OPEN-PRIVACY-confidentialite.md) →
  [ADR-011](ADR-011-deux-modes-confidentialite.md) → [ADR-012](ADR-012-sanctions-fuites.md).

## Conventions

- **Same-gesture rule**: creating a record, or changing its status, updates this index in the same pass.
- A superseded record carries a banner at the top pointing to the record that replaces it, and stays in
  place — the reasoning that was rejected is part of the reasoning that was kept.
- Records are numbered continuously. ADR-031 was reserved before it was written; it is now written, and
  it reverses the decision that reserved it.
