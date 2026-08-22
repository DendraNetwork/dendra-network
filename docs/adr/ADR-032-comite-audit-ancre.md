# ADR-032 — Audit committee ANCHORED on-chain (the audit vote becomes authenticated)

**Status:** **Accepted — implemented and deployed.** `go test ./x/jobs/...` green; the
consensus-breaking upgrade was applied on a halted chain and the height resumed without an app-hash
disagreement. **Amendment 1 (relative quorum) is included below and is coded.**
**Corrects a CRITICAL flaw in [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md)**: the
anti-false-slash veto had become the vector of the false slash.
**Implementation:** Implemented — `chain/x/jobs/keeper/audit_committee.go`,
`antievasion.go`. The draw is additionally restricted to the frozen pool of
[ADR-037](ADR-037-gel-du-vivier-de-tirage.md).

## Context — the flaw, verified line by line

A hard slash of an audited primary is decided on a **tally of verdicts**
`<jobId>__verdict__<minerId>` posted off-chain by judges. Nothing authenticated the **voting set**:

- The tally walked the commits under the prefix, excluded the primary, and required only that the
  voter be a **registered miner** — **no check of committee membership at all**.
- The slash decision was taken on a **COUNT** (`invalidVotes >= audit_min_quorum`).
- `CreateCommit` required only that the signer be the operator of *that* miner; on a
  `<jobId>__verdict__<x>` key the beacon check is skipped.
- `CreateMiner`, `DisputeVerdict` and `AdjudicateDispute` were **permissionless**, and the bond was
  **refunded in full** on exit.

Therefore **any third party** could open a dispute on a chosen victim's job, post four `"0"` verdicts
from four identities at `min_stake`, trigger the adjudication itself, and have **80 % of an honest
miner's stake slashed** for roughly the cost of gas. **Our own test suite proved it**: a test performed
exactly that attack (four judges at `stake=1`) and **required** the slash. It was **green on every
run** — it certified the flaw.

**BOTH paths were vulnerable, not only the veto.** In the earlier variant
(`audit_min_quorum == 0`), the decision was `cheated = invalidStake*2 > totalStake`, where
`totalStake` **aggregates only the voters** — so if the only voters are the four Sybils, they hold
100 % of the voting stake and clear the participation floor as well. The fix therefore had to land on
the **tally**, not on the decision rule.

**Nature of the attack: GRIEF, not profit.** The disputer's reward equals `DisputeBond` (zero in the
deployed configuration) and the slashed stake goes to the treasury. The attacker does not enrich
itself — it destroys a competitor's stake and falsifies the protocol's central property. The severity
is unchanged.

**The principle that was violated:** *an action that costs nothing and risks nothing is not a security
primitive.* The code comment justifying the old behaviour ("only REGISTERED miners are accepted; real
stake is the anti-Sybil") states a **qualitative** assumption — that registering costs something — which
is **quantitatively false** at `min_stake`.

## Decision

**The audit committee is DRAWN and ANCHORED ON-CHAIN at sampling time; the tally counts ONLY the
verdicts of its anchored members.**

1. **Draw at sampling.** When the optimistic audit draws a job, it also draws **K judge members** from
   the registered miners, **excluding the primary**, **stake-weighted**, **without replacement**, from a
   seed **later than the commit**, under a separate domain string.
2. **Anchoring.** The drawn identifiers are **persisted** and become the only list entitled to vote on
   that job.
3. **Restricted tally.** The tally ignores any verdict whose miner identifier is not in the anchored
   list. Everything else (exclusion of the primary, counting, stake majority) is **unchanged**.
4. **Fail-closed.** A job with no anchored list (older jobs, paths outside optimistic mode) permits
   **no hard slash**; it falls back to the light clawback. A cheater slipping through is bounded and
   still negative expected value ([ADR-025](ADR-025-verification-optimiste-echantillonnage.md)); an
   honest miner false-slashed is **catastrophic** — the asymmetry in favour of the accused is
   maintained.

**Why stake weighting is the heart of the defence:** the probability of obtaining a seat becomes
proportional to **capital**, not to the **number of identities**. Multiplying Sybils at `min_stake` no
longer multiplies seats — an attack by identity becomes an attack by capital, correctly priced.

## Implementation specification

**a. Storage, without a protobuf regeneration.** A new keeper collection:
`AuditCommittee collections.Map[string, string]` — key `jobId`, value the identifiers joined by commas.
*(A cleaner alternative at the next regeneration window would be a `repeated string audit_committee`
field on `Job`. Do not trigger a regeneration for this alone.)*

**b. Draw** — a pure helper, testable outside the keeper, modelled on the existing weighted selection:

```
drawAuditCommittee(ctx, seed, jobId, primaryId, K) []string
  eligible = every registered Miner except primaryId (and except the disputer if it is a miner)
  weight   = Miner.Stake
  seed     = sha256(<domain> + seed + "|" + jobId)
  draw     = K distinct, stake-weighted, WITHOUT REPLACEMENT (deterministic walk, stable key order)
  if len(eligible) <= K -> return every eligible miner
```

Determinism is required: iteration in a stable key order, integer arithmetic only, no dependence on map
ordering.

**c. Hook point** — inside the optimistic audit, at the moment the job becomes `+disputed`, just before
persisting the job: draw the members, store them under the job identifier, and emit them in the
`audit_requested` event (transparency, and so an off-chain judge knows it is summoned).

**d. Tally** — read the anchored list; if it is absent or empty, return "no decision" (fail-closed, no
slash). Otherwise build the allowed set and, inside the walk, after excluding the primary, ignore any
miner that is not in it. The slash decision rule itself is **not** modified: once the tally is
authenticated, the count becomes a legitimate honest-biased veto again, and the stake majority becomes
sound again.

**e. Liveness — the tension to own.** Only miners running a judge worker answer. Drawing exactly
`AuditCommitteeSize` at random risks never reaching the quorum, leaving audits unresolved.
**Decision: over-sample** — draw a larger `K`, leaving the quorum unchanged. The anti-Sybil bound holds
(one must be drawn, and the draw is stake-weighted) while participation becomes achievable again.
*Long-term target: a `can_judge` flag on `Miner` so that only declared judges are drawn, at which point
`K` can fall back towards the committee size.*

## Parameters

| Parameter | Role | Value |
|---|---|---|
| `auditCommitteeDrawSize` (Go constant) | number of judge members drawn and anchored per audited job | **15** |

**Accepted deviation.** The parameter block is a protobuf message, so making the draw size governable
would have required exactly the regeneration this record forbids for the storage. A Go constant was
retained instead, with **promotion to a parameter at the next regeneration window**. The security
property does not depend on it; only liveness did — and amendment 1 below removes that coupling for
good by making the quorum relative.

The safety net implemented in place of a validation constraint: if the minimum quorum exceeds the draw
size, the draw size is raised. A guard that is mathematically inert is worse than no guard.

## Tests required

1. **Rewrite the old timeout test**: in its original form it **required** four non-summoned judges to
   slash — it locked the bug in. The new requirement is that **non-members CANNOT slash**.
2. **New attack test**: four identities at `min_stake`, **not drawn**, post `"0"` — the tally ignores
   them, **no slash**, the primary's stake intact.
3. **Legitimate slash, no regression**: the **anchored** members post the quorum of `"0"` — hard slash
   at `SlashLeakBps`, unchanged.
4. **Vindication, no regression**: anchored members, majority `"1"` — vindication and definitive
   payment.
5. **Fail-closed**: a job with no anchored list — clawback, **never** a hard slash.
6. **Draw determinism**: the same (seed, jobId, set) yields the same committee; iteration order is
   irrelevant.

## Consequences

**Plus** — closes the attack by identity: voting requires being **drawn**, and the draw is **priced in
capital**. **Plus** — restores to the veto the property it claimed to have. **Plus** — fixes **both**
paths (the veto and the stake majority) at a single point. **Plus** — makes the statement "an honest
miner cannot be slashed by a minority" **defensible**, and therefore publishable.

**Minus** — liveness: if too few drawn judges answer, the audit resolves without a slash (clawback) —
accepted, and biased towards the honest. **Minus** — an attacker with very large capital can still buy
seats: that is now a real cost, not a flaw. **Minus** — one more collection (purgeable when the job
resolves).

## Alternatives rejected

- **(A) Restrict the tally to the assignment committee** — **rejected, moot.** The assignment committee
  derives the stake-weighted **origin** committee, which adjudication already deliberately excludes from
  the fresh tally; and it is re-derived from **live** state (moving stake, withdrawn miners), so it is
  not deterministic between assignment and tally. Applied as-is, it turns the verdict-slash test red and
  freezes slashing.
- **(B) Require `invalidVotes >= quorum` AND the stake majority** — insufficient alone (an
  unauthenticated voting set holds 100 % of the voting stake) and **regressive**: it reintroduces the
  stake veto that the count closed (a large-stake cheater would escape four small honest judges).
  Admissible **after** this record, as a belt, never in its place.
- **(C) Raise `min_stake` or set `dispute_bond > 0`** — **mitigation, not a fix**: it prices the attack
  without closing it, and it penalises small honest miners, which contradicts the consumer-GPU position.
  Worth applying immediately by governance in the meantime, then re-evaluating.

## Out of scope (identified follow-up)

**Punishing the false accuser.** Nothing currently penalises a judge whose `"0"` verdict is contradicted
by the resolution — every "verdict leads to slash" path targets the primary. Even when summoned, a judge
can lie for free. Slashing the contradicted minority judge would make voting genuinely costly, to be
weighed against the risk of discouraging honest audits. A later record, after judge behaviour has been
measured in the field.

## Amendment 1 — over-sampling had de-calibrated the veto quorum

**An error in this record, owned.** Section (e) specifies a larger `K` with the "quorum unchanged". But
the minimum quorum had been calibrated for a committee of **five**: four of five is **near unanimity**,
which IS the property the veto of ADR-028 claims to carry. At fifteen seats, four of fifteen is roughly
a quarter. And the slash decision was taken on an **ABSOLUTE COUNT**: four invalid votes slashed **even
if eleven jurors voted valid** (on that path the valid votes only fed dead variables, and the
participation floor was redundant and non-binding).

**Near unanimity had therefore become a blocking minority of about a quarter.** That is **not** a
regression against the state before this record (before: four free identities; after: four seats paid
for in capital — a major net gain), but the announced property was still not held, and any wording
built on the old "four of five" ratio had become numerically false.

**Arbitrated fix — RELATIVE quorum plus a double lock.** The anchored committee is already loaded in the
tally, so its size is available at no cost. A hard slash requires:

```
invalidVotes >= max( ⌈2/3 × len(anchored committee)⌉ , audit_min_quorum )   AND   stake majority
```

- The **relative quorum** restores near unanimity whatever the drawn size, and **decouples `K` from the
  calibration** — `K` becomes a pure liveness knob, which is what makes the Go constant above
  permanently safe.
- The **AND stake majority** is the former option (B), originally rejected because an **unauthenticated**
  voting set mechanically holds 100 % of the voting stake. Now that the set is authenticated, it becomes
  a legitimate belt: two independent locks (drawn seats, and stake majority).
- Every comment and configuration line stating the old absolute ratio had to be corrected in the same
  pass.

**Additional tests required:** ① four "invalid" plus eleven "valid" out of fifteen anchored produces
**NO slash**; ② a two-thirds majority of "invalid" plus the stake majority produces a slash; ③ the
relative quorum is recomputed correctly when the anchored committee is smaller than `K` (a young
network).

## Accepted blind spots

- The attack was **not replayed live** (that should only be done on a disposable genesis).
- The over-sampling size is an **unmeasured engineering choice**, to be recalibrated once the real judge
  response rate is observed. The instrumentation needed to measure it, and the trap in measuring it only
  on the failure path, are covered by [ADR-033](ADR-033-comite-readjudication-ancre.md).
