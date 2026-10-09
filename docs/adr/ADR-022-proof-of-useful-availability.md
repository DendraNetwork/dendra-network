# ADR-022 — Availability challenge ("proof of useful availability")

**Status:** Accepted (design ratified). Full activation is **conditional on the decentralised VRF
being live** (see Sequencing).
**Implementation:** Implemented but **dormant in the shipped genesis**. The challenge, the failure
window and the bounded slash exist in `chain/x/jobs/keeper/availability.go`, but
`avail_slash_bps` is set to `0` in `chain/config.optimistic.yml`, so **no availability slash can
fire** as shipped. In the interim, a `vrf_pubkey` is required to draw from the availability pool in
rewarded mode, which closes echo-farming.

Answers the gap in which the "availability" flow (20 % of Reserve emission, see
[ADR-021](ADR-021-tokenomics-v5-native.md)) was a **principle without an algorithm** ("a challenge
every four hours, verified, slashable") — hence neither implementable nor modellable.

## Context

v5 rewards **three flows** from the Reserve: work, **availability**, security. Availability must prove
that a **registered and bonded** miner **stays reachable and capable** (GPU warm, model loaded)
between real jobs — otherwise a miner collects the availability reward without ever serving.

Constraints: (1) do not create a **grindable** mechanism (a miner must not be able to distinguish a
challenge from a real job, or it will only wake up for challenges); (2) **verifiable on-chain without
privileged re-execution**; (3) **bounded slash** (a network false negative must not ruin an honest
operator).

## Decision

**The challenge IS a real sampled job, indistinguishable, VRF-drawn, verified through the same
semantic path.**

1. **Draw.** Each epoch (about four hours), for each bonded miner, an **on-chain VRF** (protocol key
   plus block randomness, *not* chosen by the miner) decides whether it is **challenged** and with
   which **seed prompt** (drawn from a public corpus of reference embeddings, or a real job replayed).
   The miner **does not know** it is a challenge: same sealed format as a normal job.
2. **Response.** The miner infers and anchors its embedding exactly as for an ordinary job.
3. **Verification.** **Reuses the semantic verification path** (integer cosine,
   [ADR-020](ADR-020-tier-verification-s2.md)): the response must be **close to the expected
   reference** (governed threshold `AvailThresholdBps`). No new verdict code.
4. **Reward and slash.**
   - Answers in time and semantically valid: a share of the epoch's **availability pool**, bounded by
     the pool (no farming beyond it).
   - **No answer** before `AvailDeadlineBlocks`, or an aberrant answer: a **bounded slash**
     `min(stake × AvailSlashBps/10000, AvailSlashMax)`. The hard bound means an isolated network
     incident costs little, while repeat failure costs a lot.
   - **False-positive quorum**: a single missed challenge does not slash; slashing requires more than
     `K` failures inside a sliding window.
5. **Anti-grinding.** Because the challenge is **indistinguishable** from a real job and **VRF-drawn**,
   the miner can neither detect it nor influence the draw, so it must stay **genuinely** available.

## Parameters (governable)

`AvailEpochBlocks` (about four hours), `AvailThresholdBps` (close to `SemanticThresholdBps`),
`AvailDeadlineBlocks`, `AvailSlashBps`, `AvailSlashMax`, `AvailFailWindow`, `AvailFailK`.

## Consequences

- **Implementable**: reuses the beacon and VRF, the commit path and semantic verification. What is new
  is an **epoch keeper** that draws challenges and settles the pool.
- **Modellable**: the macro simulation must integrate a **challenge failure rate** and the
  corresponding **slash**. A constant pass rate held outside the model **overestimates** the net cost
  and **underestimates** grinding.
- **Depends on** a real VRF: without it, the challenge draw is as grindable as everything else.

## Accepted limitations

While the VRF is a stub whose seed can be influenced, indistinguishability and anti-grinding do not
hold: this record is **only truly safe once the real VRF is live**. That ordering is binding.

## Sequencing

The decentralised VRF is **coded and tested but dormant**: `committee_seed_source` is absent from the
shipped genesis files, so the default is the legacy source.
> ⚠️ **No longer true, and left here because an ADR records the state it decided in.** Measured
> 2026-08-21 on the public chain: `committee_seed_source = 1`, and `docker/entrypoint-chain.sh`
> defaults `DENDRA_COMMITTEE_SEED_SOURCE` to `1`. The source is armed; what is NOT met is the
> contributor floor — one anchored contributor against `committee_min_vrf_contributors = 2`, so
> every draw falls back to the legacy seed. Read it rather than this paragraph:
> `/dendra/jobs/v1/committee_seed_health`. The code exists — decentralised seed
derivation, the beacon submission message, vote extensions, and the ECVRF primitive — but has never
been activated or proven live across multiple validators.

Coding the full record now would therefore be **premature**, exactly as the body above states. Ratified
order:

1. **Activate and prove the decentralised VRF live**: set `committee_seed_source=1` in the rewarded
   genesis and demonstrate vote-extension aggregation across **two to three real validators**. This is
   also a precondition of an **incentivised** testnet: a committee drawn from a legacy seed is weaker
   than one drawn from a decentralised VRF.
2. **Regenerate the protobuf definitions** for the six parameters: `AvailThresholdBps`,
   `AvailDeadlineBlocks`, `AvailSlashBps`, `AvailSlashMax`, `AvailFailWindow`, `AvailFailK`.
3. **Code the epoch keeper**: per bonded miner, draw a challenge by VRF (decentralised seed plus block
   randomness, seed prompt from a reference corpus), commit, verify semantically against
   `AvailThresholdBps`; valid means a share of the availability pool, failed or aberrant means a
   **bounded slash** plus the **false-positive quorum** (at least `AvailFailK` failures inside
   `AvailFailWindow`). This **replaces** the `vrf_pubkey` interim requirement.
4. **Align the model**: replace the out-of-model constant pass rate with the real **challenge failure
   rate and slash**, grouped with step 3 (the real slash parameters only exist after step 2). Until
   then the model output must carry an honest caveat that the availability cost is optimistic.

**Gate before an incentivised testnet**: VRF proven live (1) plus the full mechanism (2-3) plus an
aligned model (4). The interim is sufficient for a closed soft launch or a **non-rewarded** public
testnet.
