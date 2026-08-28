# ADR-028 — Anti-evasion: PROVISIONAL optimistic payment (clawback, appeal, quorum)

**Status:** Accepted. **Corrects a flaw in
[ADR-025](ADR-025-verification-optimiste-echantillonnage.md)** — the "innocent by default" liveness
rule.
**Implementation:** Implemented, with two documented deviations from the text below (see
*Deviations*). **Superseded on one point:** the no-quorum clawback path was later **replaced by
deferral** ([ADR-034](ADR-034-vitalite-du-mineur.md) §2.1), which makes the dedicated
`silence_slash_bps` penalty **unreachable** — see *Status of the silence penalty*.

## Context — the hole (evasion by non-revelation)

The optimistic mode pays the k=1 primary **first**; the sampled audit is the **only** clawback lever.
But the reveal step is **cooperative**: only the primary can re-seal the prompt and answer to the fresh
committee, because the relay blob is encrypted to *its* session key. The consequence, verified in the
code:

- a cheater that **reveals a false answer** gets slashed by adjudication;
- a cheater that **stays silent** produces no re-commit and no verdict, so adjudication fails with an
  incomplete fresh committee, and the resolve timeout marks the job resolved **without a slash**
  ("innocent by default").

Therefore **the cheater's dominant strategy is SILENCE** — it keeps the payment — not revelation. The
Nash inequality of ADR-025 (`s·P > (1−s)·g`) **no longer holds** for a silent primary; the slash only
deterred a *cooperative* cheater. **The optimistic slash was evadable.** The liveness rule that opened
the hole had been added for a good reason: to avoid jobs stuck forever in `+disputed`.

## Decision — PROVISIONAL payment (optimistic-rollup semantics)

The optimistic primary is the **suspect**, not the accuser: **it never keeps an unverified payment**,
and the burden of proving (revealing) falls on it.

1. **Unaudited jobs are paid definitively and instantly** (the optimistic property is preserved: cost
   near 1×, zero added latency).
2. **A job drawn for audit (`+disputed`) is paid PROVISIONALLY** until resolution. At
   `audit_resolve_timeout`:
   - **quorum** of verdicts reached, majority **valid**: payment becomes **definitive** (vindicated);
   - quorum reached, majority **cheating**: **hard slash** at `SlashLeakBps`;
   - **no quorum, or a SILENT primary**: not "innocence" but **(a) CLAWBACK of the payment** (the fee
     is re-escrowed and refunded to the client) **plus (b) a `silence_slash_bps` slash** calibrated so
     that cheat-then-stay-silent is negative expected value (the slash must exceed the compute saved by
     lazy cheating).
3. **APPEAL**: an `appeal_window` during which the primary can **reveal late**; if a fresh committee
   then vindicates it, **restitution** follows (payment and stake). **This is what protects an honest
   miner that went offline** (crash, network): it reveals on its return and recovers; a cheater does
   not.
4. **QUORUM (against false slashes)**: **never slash on fewer than `audit_min_quorum` fresh verdicts**
   (default the committee size, stake majority). Below quorum, treat the case as "audit not honoured"
   — clawback, **not** a hard slash. This absorbs cross-GPU and judge non-determinism: one divergent
   judge is not cheating.
5. **`Params.Validate()` in mode 1**: require `audit_sample_bps > 0` and
   `audit_resolve_timeout > dispute_window > 0`, otherwise the timeout vindicates before adjudication
   is admissible and the slash is inert.

## Parameters (governable, dormant by default)

| Parameter | Role | Default |
|---|---|---|
| `silence_slash_bps` | slash for non-revelation at the deadline (at least the compute saved) | 0 (off) |
| `appeal_window` | blocks for late revelation, with restitution if vindicated | 0 (off) |
| `audit_min_quorum` | minimum fresh verdicts before any slash | committee size |

plus the cross-constraint in `Validate()` for mode 1.

## Consequences

**Plus** — closes evasion by silence and **restores the Nash property for ALL primaries** (both
revealing-false and staying-silent are negative expected value). **Plus** — stays optimistic:
unaudited jobs keep instant payment and zero latency; only audited jobs have deferred finality.
**Plus** — the appeal protects an honest offline miner, so mere unavailability cannot produce a false
slash.

**Minus** — finality latency on audited jobs (acceptable). **Minus** — complexity (provisional and
appeal states, clawback). **Minus** — **requires the fee to remain re-escrowable**: settlement must not
release the whole escrow when paying an auditable job, so a hold or claim must persist until finality.

## Alternatives rejected

- **A bare slash on silence**: punishes an honest offline miner by its **entire stake** (false
  positives) — rejected in favour of **clawback plus a calibrated slash plus an appeal**.
- **Pre-revelation or escrow of the sealed answer** (to a threshold, or to the chain at open time):
  removes the cooperative dependency but is complex (who holds the decryption key? a threshold? a
  pre-commit that can leak) — **deferred to research**.
- **Status quo (innocent by default)**: open evasion — unacceptable.

## Code impact

- Settlement: **provisional** payment plus a **fee hold** on auditable jobs (do not release everything).
- The audit resolve timeout: replace "vindicate without slash" by the **quorum / clawback / slash**
  logic above.
- `AdjudicateDispute`: require `audit_min_quorum`.
- `Params` and `Validate`: `silence_slash_bps`, `appeal_window`, `audit_min_quorum` plus the
  cross-constraint.
- **Tests**: (1) cheater that reveals is slashed; (2) **silent** cheater is clawed back and slashed;
  (3) honest offline miner appeals and obtains **restitution**; (4) quorum not reached means **no** hard
  slash.

## Detecting "silence" — the mechanism that filled a gap in this record

This record said "silent primary means clawback plus slash" but did **not** say *how the chain
distinguishes* a silent primary from an absent committee. The implemented answer uses a **non-forgeable
signal**, which is strictly better than a revelation marker anchored by the suspect (that would be
forgeable): **the judge worker posts a `"0"` (invalid) verdict** when it cannot obtain a usable
revelation after a grace period shorter than `dispute_window`. A silent primary, or one that reveals
nothing usable, is therefore **converted into a cheating quorum by the committee itself**, producing a
hard slash. The "no quorum" case is then reserved for a **genuine committee failure** (rare). It also
answers the objection that the relay must not be trusted.

## Implementation state and deviations

**Delivered without a protobuf regeneration:** the audit resolve timeout has three outcomes (cheating
quorum, hard slash at `SlashLeakBps` plus clawback; valid quorum, vindication; insufficient
participation or no quorum, light restitutable clawback); a **participation floor** of
`⌈CommitteeSize/2⌉+1` gates **every** hard slash, on both the timeout and the adjudication paths, which
prevents two judges or Sybils forming a "majority of those present" and slashing an honest miner while
honest judges vote late; the cross-constraint in `Params.Validate()` for mode 1; and the judge worker
posting `"0"` when no usable revelation arrives. Six tests cover this, including the two-judge grief case below
the floor.

**Deviations still open**, each requiring a protobuf regeneration:

| Point in this record | What is implemented | What full conformance needs |
|---|---|---|
| `audit_min_quorum` (participation) | a constant floor of `⌈CommitteeSize/2⌉+1` for hard slashes, plus a minimum of 2 to vindicate | a **governable** parameter expressed as a fraction of the committee size |
| dedicated `silence_slash_bps` | reuses `SlashLeakBps` for cheating, and a fee clawback for no quorum | a dedicated parameter |
| **fee hold** at settlement | pays in full, claws back from the **stake** | retain the fee until finality (a true "provisional" payment) |
| permissionless `appeal_window` | restitution through a governance dispute resolution, with a restitutable slash record | an appeal window with automatic restitution |

The relative quorum that later replaced the absolute floor is specified in
[ADR-032](ADR-032-comite-audit-ancre.md), amendment 1.

## Status of the silence penalty (`silence_slash_bps`) — inert

The dedicated silence penalty lives in `clawbackPayment`, and that function **has no production
caller**: the "no quorum leads to clawback" path was **replaced by deferral**
([ADR-034](ADR-034-vitalite-du-mineur.md) §2.1), because charging an honest primary for a committee's
failure is the very invariant this project refuses to break. A **genuinely** silent primary is still
punished, through the slash branch — a missing revelation makes the judges post `"0"`, which forms a
cheating quorum.

Consequently `silence_slash_bps` **cannot fire**. It is an **inert parameter**, and it must not be
described as an active guard. The shipped genesis (`chain/config.optimistic.yml`) still sets it to
a non-zero value, which changes nothing and should be removed with the parameter. **A formal removal is
outstanding**; the function is kept meanwhile as an executable record of the former semantics and must
not be re-wired without re-reading the reasoning above.

## Links

[ADR-025](ADR-025-verification-optimiste-echantillonnage.md) ·
[ADR-026](ADR-026-llm-juge-onchain.md) · [ADR-027](ADR-027-decisions-execution.md) ·
[ADR-032](ADR-032-comite-audit-ancre.md) · [ADR-034](ADR-034-vitalite-du-mineur.md)
