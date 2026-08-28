# ADR-033 — Anchored RE-ADJUDICATION committee, and the semantics of the dispute bond

**Status:** **Accepted — implemented.** `go build ./... && go test ./x/... ./app/...` green; the 18
existing anti-evasion tests are unchanged and passing.
**Corrects a RECURRENCE of [ADR-032](ADR-032-comite-audit-ancre.md)** in the adjudication path — and
closes a permissionless remnant along the way.
**Implementation:** Implemented — `chain/x/jobs/keeper/redo_committee.go`,
`msg_server_adjudicate.go`, `msg_server_dispute.go`, `audit_sampling.go`,
`msg_server_settle_job.go`.

## Context — the same flaw, one path over, and worse

[ADR-032](ADR-032-comite-audit-ancre.md) authenticated the voting set of the **verdict tally**. It
closed only **one** of two doors: the "fresh" **re-adjudication committee** was **not drawn at all**.

- Before the fix, being **a registered miner outside the origin committee** was enough to post a
  re-commit and enter the tally. The gate was "at least `CommitteeSize` fresh commits" — three
  identities — and the majority was computed over the stake of the **self-selected** voters only, so
  three identities at `min_stake` held **100 % of the electorate**.
- **This path is worse than ADR-032's.** There, the Sybil slash only permitted **grief** (the attacker
  destroyed without gaining). Here, a **genuinely slashed** cheater can stand up three Sybils that copy
  its original commit, obtain the "strict majority of fresh stake" (100 % of it its own), and trigger
  **RESTITUTION from the treasury** plus a bond refund plus the disputer reward — that is, to itself.
  **The attacker collects.** Cost: gas (the dispute bond was zero, and the miner bond is refunded on
  exit).
- The file's header comment announced "dormant by default, `dispute_window=0`" while the testnet was
  running with a non-zero dispute window: **the comment described a dormancy the code did not
  implement.**
- An adjacent trap, found before delivery: the cosine fallback tested `agree*2 <= totalRedoStake`,
  which is **TRUE when both are zero** — an **empty** electorate therefore pronounced "the primary
  cheated". Closing the front door without that guard would have converted a Sybil flaw into a silence
  flaw.

## Decision

**The re-adjudication committee is DRAWN and ANCHORED when the dispute OPENS; only its members count;
and the disputer's bond is forfeited only on an effective adjudication.**

1. **Draw at opening** — from a seed **later than the original commits** (so the miner cannot grind it)
   and **earlier than the re-commits** (so the challenger cannot pick its judges after the fact).
2. **A DISTINCT draw domain** — otherwise knowing one committee reveals the other. A shared helper takes
   the domain as an argument; no copy of the drawing logic.
3. **Exclusions** — the primary, the disputer, the origin committee, **and the AUTHORS of the
   contested commits** (amended with ADR-045 entry 16). The first three were the whole list, and the
   fourth is not a refinement of them: `CreateCommit` requires neither that a miner be assigned to the
   job nor that it was drawn onto the committee, so the set that PRODUCED the disputed result is
   `{mid : Commit[jobId__mid] exists}` and it is not the drawn set. Measured: a miner assigned to
   nothing anchored a commit and then sat on the jury re-judging it. The two sets are UNIONED rather
   than swapped — a drawn member that stayed silent must still not judge the job it was summoned to.
   And an excluded seat now leaves the quorum DENOMINATOR as well: a seat whose holder may not vote is
   not a seat, or every exclusion would raise the bar for everyone else.
4. **Anchoring** under a `<jobId>__redo` key in the existing committee collection, so **no protobuf
   regeneration**. Job opening forbids `"__"` inside a job identifier, so `<jobId>` and `<jobId>__redo`
   cannot collide.
5. **Total fail-closed** — with no anchor: neither slash nor restitution.
6. **Quorum RELATIVE to the anchored seats**: `len(fresh) >= ⌈2/3 × seats⌉`, with a floor of
   `CommitteeSize`.
7. **Bond semantics**: **only an effective adjudication forfeits; a timeout returns.**

## Amendments required before ratification (all coded)

**A. The denominator.** The gate was three fresh commits against **fifteen drawn seats**: a fifth of the
seats pronounced a hard slash or a treasury restitution. **This is word for word the error made in
ADR-032** ("the threshold was right, its denominator changed underneath it"), reopened on the
adjudication path. Fixed with the exact formula of ADR-032 amendment 1:

```go
redoSeats  := len(allowedRedo)
redoQuorum := (2*redoSeats + 2) / 3   // ceil(2/3 x anchored seats), integer arithmetic
```

**B. The timeout never read the redo anchor.** It consulted only the audit-committee key: a human
dispute on a job that had never been sampled, with a redo committee **anchored but SILENT**, fell into
the "unfounded" branch — so the **bond of an HONEST disputer was forfeited** because the drawn judges
did not answer, and the refund path was unreachable. **That is the invariant turned against the
disputer.** Fixed by distinguishing **three silences** through the two anchors:

| Case | Condition | Effect |
|---|---|---|
| (i) summoned but silent | redo anchored, no audit | retention released, **bond RETURNED** |
| (ii) unfounded dispute | neither audit nor redo, human dispute | **bond forfeited** |
| (iii) normal audit | audit committee anchored | tally |

**C. A permissionless remnant, the purest form of the class.** The legacy settlement handler settled a
job on the **sole basis of the address format**: no commit read, no committee. Anyone could mark a job
settled, after which every real path refused it as "already settled", so the **escrow was frozen at the
module FOREVER**, the miner was never paid, and the pool counters were credited **as if it had been**.
Cost: gas, replayable on every job from the moment it opened. **Verified dead** (production settles only
through the semantic path; the legacy handler survives in demonstrations), so it was **gated to the
AUTHORITY**, with the gate placed **after** the anti-replay check so that the existing anti-replay test
still exercises what it claims to. **Full removal from the protobuf definitions is deferred to the next
regeneration** (see Open items).

## Ratified principle — the semantics of the bond

> **Only an effective adjudication forfeits a bond. A timeout returns it.**

A dispute bond exists to deter a **frivolous challenge**, not to bill an **outage of judges**.
Forfeiting on silence would punish a disputer whose claim was never evaluated — the invariant "an
infrastructure failure is never billed to the honest party", applied to the disputer.

**Accepted residue, and where anti-spam actually comes from.** Returning the bond on timeout makes
dispute spam free *in forfeiture terms*; its payoff is not theft but **delay** (the primary's retention
is blocked for the window). What limits spam is therefore **not** forfeiture but **capital
immobilisation**: N simultaneous disputes lock N bonds. **That is a direct argument for a non-zero
dispute bond** — and the precondition for allowing one (amendment B) is now met.

## Parameters

| Parameter | Role | Value |
|---|---|---|
| `dispute_bond` | cost of immobilising capital to challenge | set equal to `min_stake` — enough to deter grief, too little to deter a legitimate challenge. Set in the launch genesis, with a boot-time alert if absent (it had been set **nowhere**, so it defaulted to zero and the anti-grief forfeiture forfeited nothing). |

## Tests

A dedicated test file covers the attack in **both directions** (false slash and self-vindication),
fail-closed with no anchor, exclusion of the parties, and **the legitimate slash still firing** —
without that last one, the first three would be satisfied by code that no longer does anything. The
amendments add a test that the silent redo committee **refunds** the bond rather than forfeiting it
(written to fail against the previous code) and a test that the redo quorum is relative to the anchored
seats (fifteen seats, three re-commits, refused). **Seven existing tests stood up the fresh committee by
hand and declared it legitimate — they certified the flaw**; they are now anchored. No regression: the
18 anti-evasion tests are unchanged and green.

## Ratified process rule — audit the CLASS, not the instance

ADR-032 was found, fixed, tested, reviewed line by line and **deployed** — and the same flaw was living
**three files away**. Two end-to-end reviews missed it, on both sides, because the *instance* (the
verdict tally) was audited instead of the *class*.

**Rule: every security fix is accompanied by a named sweep of its twins, written into the record as a
REPRODUCIBLE QUERY, not as prose.**

The query for this class, to be replayed at every fix of this kind:

```
For each handler that moves capital (slash, payment, restitution, clawback, pool credit):
  1. On what basis does it decide?             (a commit? a vote? nothing?)
  2. Where is it written WHO was entitled to vote?  (anchored on-chain? recomputed? absent?)
  3. Is the threshold's denominator the anchored SEATS, or only the RESPONDERS?
  4. What does it do when the set is EMPTY?    (comparisons like `a*2 <= total` are true at zero)
  5. Who can call it?                          (operator / committee / authority / anyone?)
```

Applied: question 5 produced the settlement remnant, question 3 produced amendment A, question 2
produced this record itself, and question 4 produced the cosine fallback.

### Question 6 — can the artefact contradict me?

> **Which result, if I obtained it, would force me to the opposite conclusion? If none is reachable BY
> CONSTRUCTION, the artefact verifies nothing.**

This applies to **every verification artefact** — a test, a metric, a guard, a review — not only to
measurements. The project met this fault in **three forms**, and the root is the same: *an artefact
whose result is fixed by construction*.

| Form | Instance | Why it reassured falsely |
|---|---|---|
| A **test** that cannot fail | the old timeout test **required** four non-summoned judges to slash | green on every run — it **certified** the flaw |
| A **guard** that cannot bite | a minimum quorum larger than the draw size, hence mathematically unreachable | an announced protection that could never trigger |
| A **measure** that cannot contradict | a participation metric emitted only on the **timeout** path, so it samples only failures | it would have "proved" that the draw size must collapse, on censored data |

Operational corollary: a measure must emit **on every outcome** (success **and** timeout, separated by a
resolution label), a test must be **written to fail against the previous code**, and a guard must be
**exercised by a case that triggers it**.

## Consequences

**Plus** — closes the recurrence and the *remunerated* form of the attack (self-vindication from the
treasury). **Plus** — the bond becomes settable without punishing the honest. **Plus** — the class is now
tooled by a query rather than by vigilance.

**Minus** — **feasibility of the permissionless route**: at fifteen anchored seats, the adjudication
quorum is ten re-commits. If only three or four judges answer in practice, adjudication never completes
and everything goes through the timeout (which returns the bond and resolves, so **liveness is
preserved**), but the permissionless route becomes **decorative**. See Open items.

## Open items

0. **The instrumentation as first placed is CENSORED — complete it before drawing any conclusion from
   it.** The participation event is emitted only at the **timeout**, that is, only for disputes whose
   adjudication **failed for want of a quorum**; the success path emits nothing. The observed
   distribution is therefore truncated to its left tail: **any measurement will show responders below
   quorum by construction of the sample**, and will wrongly conclude that the draw size must collapse.
   The fix is to emit the same event on the success path, where both the seat count and the responder
   count are already computed, after the quorum is established. Without both points, the instrument
   produces a false conclusion with the confidence of a number.
1. **Calibrating the draw size — do not guess, INSTRUMENT.** The security property is the **RATIO**
   (two-thirds), not the absolute size, so the draw size can be tuned on liveness data **without touching
   security**. The two committees do not even have the same purpose (the audit committee over-samples
   because judges volunteer; the redo committee targets a specific dispute) and **should not share a draw
   size**. Emit, per dispute, how many summoned members answered, then set the size on the measurement.
   Until then, status quo: the timeout guarantees liveness.
2. **Removing the legacy settlement handler from the protobuf definitions** — approved, **at the next
   regeneration**. A half-removed RPC (protobuf out of step with generated code) would be worse than the
   gated remnant.
3. **The assignment committee anchors nothing** — it **recomputes** the committee over the *current* set
   of miners at every call, so the six capital-moving handlers that use it rest on a voting set never
   written on-chain, and therefore unverifiable after the fact. The same underlying fragility, not
   trivially exploitable; next regeneration window.
4. **A redo draw on a very small network** returns nothing without a log or event — minor, to be folded
   into the next batch.

## Accepted blind spots

- Neither attack was **replayed live** (to be done only on a disposable genesis).
- The response rate of summoned committees is **not measured** — it is the only unknown separating a real
  permissionless route from a decorative one.
