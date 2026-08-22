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
| [ADR-023](ADR-023-emission-reserve-onchain.md) | On-chain Reserve emission module (`x/emission`) | Accepted | Implemented — ⚠️ the module account was EMPTY on the chain of 2026-08-01; the boot now **refuses to start** unless the genesis credits it (`3300000000000 udndr`, verified at every first boot), dated correction at the top of the ADR |
| [ADR-024](ADR-024-couche-economique-reelle-onchain.md) | Real economic layer on-chain: deployment and verified invariants | Accepted | Implemented, verified live |
| [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) | Optimistic verification by VRF sampling (k=1, sampled audit, hard slash) | Accepted — extends ADR-020 | Implemented and armed; under `committee_seed_source = 1` a draw happens **only** on a decentralised seed, and due jobs are **deferred** at any height that has none (`chain/x/jobs/keeper/audit_sampling.go`, `runOptimisticAudit` / `auditDeferStride`) |
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
| [ADR-040](ADR-040-politique-de-downtime-du-validateur.md) | Consensus downtime: widen the signing window (100 → 10000 blocks, 0.5 → 0.85) and cut the burn 20-fold, while leaving the jail and double-signing untouched | **Decided by the owner** | Enacted as **governance proposal 1**, voting until 2026-08-10T18:52:03Z; the three genesis paths already write these values; **not consensus-breaking** |
| [ADR-041](ADR-041-repartition-du-pouvoir-de-consensus.md) | No validator may hold two thirds or more: a supermajority holder forms the polka before the block arrives, so every other validator precommits `nil` — present, never absent, never jailed, contributing nothing | **Decided by the owner** | Enacted on-chain 2026-08-11 by two delegations (heights 171109 / 171112), 99.999 % → 62.50 % with two partners at 18.75 %; measured 21.4 % → 98.8 % and 10.3 % → 96.4 % `Commit`; **not consensus-breaking**. ⚠️ **An index states a DECISION, never a state.** Every chain this decision was enacted on has since been destroyed, and the second validator was lost for good when its host expired — read the current split with `dendrad query staking validators -o json` and the seed with `query jobs committee-seed-health -o json`. **Addendum**: seed suppression is gated at ⌈**1/3**⌉, not 2/3 (`MinVrfContributorPowerBps=6667`), so even the target split leaves the largest holder a unilateral veto on every committee draw |
| [ADR-042](ADR-042-monopole-d-assignation-et-verrou-circulaire.md) | Thirteen registered miners, one ever served: a frozen pool strands 35 jobs, a 100× stake takes ~94 % of the rest, and the retention that pays them cannot be released without judges — a loop in which no single component is faulty | **Observed; two of its six relaunch requirements now implemented** | Primary drawn 51/51 for one miner, and 9/9 where it had competitors; the jury is drawn from the same frozen pool, so **42 of 51 jobs could never reach a quorum whatever judges joined**; 16 retentions totalling 272 406 udndr held with no governance path to settle them. **Fixed for the relaunch**: the work draw now tests vitality at the freeze height, and the draw weight is capped at twice `min_stake` — 4 cases, each red under its own mutation, no overlap — ⚠️ **the chain it measures has been destroyed**: its figures are history, not a reading of any live network, and the mechanisms are unchanged in the code |
| [ADR-043](ADR-043-forme-des-cles-de-commit.md) | A commit key says what it is, and one function decides it: `CreateCommit` checked neither that the job existed nor any field length, so a registered miner wrote permanent, free, unbounded entries under job ids no job had opened (`ValidateBasic`: 0 occurrences in the module) | **Accepted, implemented, NOT deployed** (consensus-breaking: `CONSENSUS_EPOCH` 5 → 6, reaches a network only through a fresh genesis) | The obvious fix cuts the audit layer: a **fifth key shape** — the reveal worker's epoch marker `e<N>__reveals__<minerId>`, armed by default — carries **no job id**, and the chain has no name for that namespace (0 occurrences outside tests), so a naive `Job.Has` would refuse every marker at every epoch on every judge node. Hence three answers rather than two, an exemption decided by the KEY and never by the sender-supplied `Kind`, a residue test that closes padding (`<realJob>__x1__<me>`), and size bounds **derived** from `embedMaxDim`/`embedMaxMag` rather than chosen. Derivation 16 cases / mutation 5/5, handler 7 cases / mutation 5/6 — the sixth is **not provable** with this harness and the test file says so |
| [ADR-044](ADR-044-le-silence-du-mineur-ne-se-punit-pas-il-se-rend-sans-valeur.md) | Staying mute pays, and the payoff is a parameter: EV(silence) ≈ `(1 − a) × minerNet`, and the live chain serves `audit_sample_bps = 5000` while `config.optimistic.yml:145` announces `10000` — a desynchronisation, not an arbitration (re-measured against the public network; the writer is now named — `docker/entrypoint-chain.sh:168` rewrites the params at every container start from its own default, so the file is never read for this line. Of 5 divergences on the jobs params, 4 are deliberate). Remove the payoff rather than invent a punishment | **Proposed — owner arbitration required** (one governance parameter + consensus-breaking Go that does not exist yet) | ⛔ **The obvious fix locks the network**: at `a = 10000` every job enters the audit path, and a jury below `audit_min_quorum` defers with the fee still held (`audit_sampling.go:194-203`) — with one registered miner that floor is unreachable forever, so `HeldFee` never clears, `hasOpenObligation` stays true and **every miner's bond is locked for good**. Hence a mandatory order: ship the bounded unwind first, raise the rate second. Three independent designs were each refuted by four adversarial lenses, all for one shared reason — they gated release on the ABSENCE of a bad signal when the chain carries no POSITIVE one (no `__reveal__` branch), so every such guard falls back on a jury that never concludes and turns against the honest miner. Also measured: `silence_slash_bps = 2000` is **served** while its only reader has no caller — the chain advertises a penalty it cannot apply |
| [ADR-045](ADR-045-neuf-defauts-mesures-en-attente-d-arbitrage.md) | Nine defects reproduced by running the shipped code, none of them a one-line fix: an escrow stranded by `SettlePay`, a "verified" capacity block won by NAMING a staked miner, an admission that reads quorum zero as no constraint while the draw reads four, a work committee re-derived from the LIVE stake, a retention age lost whenever the restart height is below it, a reveal signed by its writer and keyed by its reader, a member that may leave the registry after answering, and an anti-collusion exclusion that removes the ASSIGNED instead of the AUTHORS, and a miner daemon that de-duplicates on the relay deposit instead of the on-chain commit | **CLOSED — all nine are FIXED** | One record rather than nine because they share a structure: a guard, a claim or a key built for one question is being asked another — assigned vs authored, drawn vs still registered, named vs owned, absent vs zero, paid vs settled. Each entry carries its own measurement and its candidate fixes; the recommendations are marked as opinions. Three defects from the same sweeps were FIXED rather than recorded (compound slash taking ~96 %% of a bond, a slash above 100 %% underflowing a uint64, a dispute openable with no deadline) and moved `CONSENSUS_EPOCH` 6 → 9. Every pinning test asserts TODAY's behaviour on purpose: its red is the signal that a fix landed. **2026-08-22 — all nine are FIXED**, their pins flipped and red under mutation. (9), (11), (13), (15) and (16) were taken first; (10), (12), (14) and (17) followed once the owner arbitrated each. They ride consensus epoch 12, live since 2026-08-22 on a fresh genesis — so they are SHIPPED, not yet EXERCISED: no block has driven one of them so far |

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
