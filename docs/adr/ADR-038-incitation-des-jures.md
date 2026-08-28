# ADR-038 — The juror incentive: why none is added, and what must exist before one can be

**Status:** **Accepted — the decision is a non-action, implemented as an executable guard.**
**Concerns:** [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) (the sampled audit),
[ADR-026](ADR-026-llm-juge-onchain.md) (the judge), [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md)
(`silence_slash_bps`), [ADR-032](ADR-032-comite-audit-ancre.md) (the anchored committee).
**Builds on:** [ADR-034](ADR-034-vitalite-du-mineur.md) and [ADR-036](ADR-036-vitalite-de-jure.md),
whose invariant — *an infrastructure outage is never billed to the honest party* — is the reason two
of the four options below are rejected.
**Implementation:** Implemented as a **guarded absence**. No consensus code is changed; nothing is
added to `x/jobs`. The four measurements and the two invariants live in
`chain/x/jobs/keeper/adr038_juror_incentive_test.go`. **Not consensus-breaking: the state
machine, the parameters and the app-hash are untouched.**

---

## 1. The gap, measured

Both halves of the gap are real. Neither is a suspicion.

**(a) A juror is paid by no path.** Every coin and stake movement in `x/jobs` is directed at one of
four parties: the **primary** (`job.MinerId`), the **client**, the **disputer**, or the **Treasury**.
None is reachable from the identity of a juror. The whistleblower reward in
`msg_server_adjudicate.go:303-317` pays `job.Disputer`, not the committee that ruled. The work
subsidy is gated on `miner.Demand * work_gate_bps / 10000` (`msg_server_claim_subsidy.go:39`), and
`Demand` is credited only at settlement, only to the primary (`msg_server_settle_job.go`, the `miner.Demand +=` at settlement,
`msg_server_settle_semantic.go`, the same `miner.Demand +=`). A juror's `Demand` never moves, so a juror's subsidy cap never
rises.

**(b) A silent juror is sanctioned by no path.** `silence_slash_bps` is consumed at
`antievasion.go:343-355`, inside `clawbackPayment`. That function has **no production caller**: the
"no quorum then clawback" path it belonged to was replaced by deferral, and its own header says so
(`antievasion.go:274`). The parameter is **inert**, not dormant — the distinction `params_inert_test.go`
draws: dormant is a value, inert is an absence of code. The shipped genesis nonetheless describes it
as "what keeps staying mute -EV" (`chain/config.optimistic.yml:103`). A governance vote raising
it would pass and change nothing.

So: **no carrot, no stick.** And judging is opt-in off-chain (`DENDRA_MINER_JUDGE=1`), costs a GPU
pass per audited job, and returns nothing. Mining, on the same box, is paid.

## 2. The fact that decides everything, and it is not about money

**A verdict is an unpriced, unproven byte.**

`CreateCommit` writes `<jobId>__verdict__<minerId>` with an arbitrary `ResultCommit` string. It checks
the signer is the miner's operator, that the key is unused, and the model registry when enforced. It
checks **nothing** about an inference having been run. The tally then distinguishes exactly one token:

```go
if strings.TrimSpace(c.ResultCommit) == "0" {   // antievasion.go:133
    invalidStake += m.Stake
    invalidVotes++
}
```

`"0"` is the accusation. **Every other string is counted as a valid vote** — including one a juror can
emit without loading a model.

The verdict alphabet is therefore **one-sided**. A juror has three available behaviours and only two
of them are expressible:

| Intent | What the chain sees | Cost to the juror | Effect on the audit |
|---|---|---|---|
| "I verified; the primary cheated" | `"0"` | a full inference pass | counts toward the slash |
| "I verified; the primary is honest" | any non-`"0"` string | a full inference pass | counts toward the vindication |
| **"I am here and undecided"** | **not expressible** | — | — |
| "I did no work" | silence, or any non-`"0"` string | zero | nothing, or **counts toward the vindication** |

The last two rows are the whole problem. **"Present but undecided" and "did no work" collapse onto the
same free byte, and that byte is read as a vindication.**

### The measurement

`adr038_juror_incentive_test.go` puts a number on it, on the real draw size (15 anchored seats,
relative bar `ceil(2/3 x 15) = 10` under default parameters):

- **T1** — ten seats post `"x"`, a byte attesting to no inference at all. The audit closes:
  `+resolved+vindicated+quorum`, the primary keeps its stake, the held payment is released.
- **T2** — the identical fixture with those ten seats **silent**. The audit **defers**: no `+resolved`,
  no vindication, no clawback, the fee stays held.
- **T3/slash** — the same ten seats posting `"0"` instead: `+resolved+clawed+quorum`. Same seats, same
  effort, opposite outcome — which is what proves T1 measures the token and not the fixture.

> The difference between "nothing happens" and "the payment is released without verification" is **ten
> bytes**, and no juror had to verify anything to produce them.

**That gradient is the object every candidate incentive would act on.** The cheapest way to satisfy
*any* pressure to participate is the free byte, and the free byte is a vindication. The defect is not
"money buys votes". It is that **the observable is one-sided**, so pressure of any denomination —
coins, stake, or access to work — pushes in exactly one direction: pay the primary without checking.

### It is already the reference behaviour under uncertainty

The off-chain judge does not merely risk this; it does it by design. `judge_worker.py:116` ships with
`DENDRA_JUDGE_ABSTAIN_VOTE=1`: on positively detected ambiguity, vote `"1"` (benefit of the doubt)
**instead of abstaining**. That default is defensible on its own terms — on an intrinsically ambiguous
prompt there is no ground truth, so vindicating is the honest option. But it means the honest client
already resolves uncertainty toward vindication. **An incentive to participate would add a dishonest
path to the same destination, and nothing on-chain could tell the two apart.**

## 3. The options, and what each one costs

### (a) Add nothing, and say so publicly

- **Cost:** the launch risk is real and must be stated, not hidden. With judging opt-in and
  unremunerated, audits close only if operators run `--judge` out of self-interest in the network. If
  they do not, audits defer indefinitely: the fee stays **held** (never lost, never released), and
  `held_total` / `held_oldest_age` are what make that falsifiable. Verification degrades toward
  nothing while the chain keeps producing blocks — the failure is silent unless those two gauges are
  watched.
- **Attack surface opened:** none new. The existing surface is unchanged.
- **Consensus-breaking:** no.

### (b) Eligibility: judging conditions access to paid work

Rejected, for two independent reasons — and the first one matters because the option looks free.

**(i) The predicate that already exists does not bite.** `eligibleAsJuror` (`miner_vitality.go:325`)
is satisfied by a recent commit, and `commitProvesAssignedWork` (`miner_vitality.go:138`) accepts an
**assigned-committee commit** as proof. So a miner that works and never judges keeps its vitality
fresh forever. Applying `eligibleAsJuror` to the work draw — the cheap version of this option — is a
**no-op on exactly the population it targets**: a green that costs nothing. To bite, it needs a new
predicate ("answered its last summons"), new state, and a new filter inside
`assignedCommitteeOrdered` — which changes **who gets paid**, hence is consensus-breaking, and would
have to be conceived, not retrofitted.

**(ii) Implemented correctly, it inherits the defect of §2.** The cheapest way to keep one's
eligibility is the free byte. The mechanism would manufacture vindications, in the same direction and
for the same reason as a cash reward. It is not the safe non-monetary option; it is the same defect
denominated in access instead of coins.

**And it reopens a closed invariant.** A miner that misses a summons because of a power cut, a
migration or a maintenance window would lose its access to paid work. That is *billing an
infrastructure outage to the honest party* — the fault `audit_jury_floor.go`, ADR-034 and ADR-036
each removed, reappearing one layer down, on the juror side. `jurorFreshnessBlocks` is deliberately
three days wide for precisely this reason.

- **Cost:** new consensus state, a new filter on the payment draw, a widened outage surface.
- **Attack surface opened:** manufactured vindications; and a griefing angle — an attacker able to
  keep a competitor off committees degrades its access to paid work.
- **Consensus-breaking:** yes.

### (c) Wake up `silence_slash`

Rejected twice over.

It **presupposes a payment that never arrives**: the penalty lives inside `clawbackPayment`, whose
whole premise is a payment to claw back, and a juror is never paid. Re-wiring it onto jurors would be
writing a new mechanism, not waking an old one.

And such a mechanism would be **strictly worse than (b)**: a stake penalty for silence makes the free
byte not merely profitable but *mandatory*. It also re-punishes the outage case with real money, and
it does so on a signal — silence — that ADR-028's own deferral was introduced to stop treating as
evidence.

- **Cost:** a new penalty path, plus the re-litigation of the deferral that replaced the old one.
- **Attack surface opened:** forced participation, hence forced vindications; a network partition
  becomes a mass slashing event.
- **Consensus-breaking:** yes.

### (d) Pay the juror

All three forms fail, each for its own reason, and all three for the reason in §2.

**Per vote.** Buys votes, not judgement. The rational juror votes fast, votes with the crowd, and
takes as many seats as its stake allows. It destroys the independence of juror errors, which is the
only property the audit exists to produce.

**Per participation, not per vote.** Presented as the safer variant. It is not, and the reason is
specific to this codebase: **participation and voting-valid are the same observable.** There is no
"present and undecided" token (§2), so paying for presence is paying for the free byte, which is a
vindication. It is per-vote payment wearing a different name.

**Deferred, conditioned on having been in the anchored majority.** This one correlates *by
construction*, and openly: it makes the expected majority a Schelling point and turns the jury into a
guessing game about the other jurors. It is the worst of the three, because a juror who has genuinely
verified and finds itself in the minority is **punished for being right** — the precise inversion of
what a jury is for.

- **Cost:** a new emission or Treasury sink, sized without any data on juror participation.
- **Attack surface opened:** seat farming (stake-weighted draw plus a per-seat reward is a direct
  yield on stake, decoupled from any work); complaisance stamping; in the deferred-majority form, a
  self-reinforcing cartel.
- **Consensus-breaking:** yes.

## 4. The decision

**Add no incentive. Publish the absence. Guard it with tests.**

A juror incentive designed before the verdict alphabet can express "present and undecided" is an
incentive whose cheapest satisfaction is an unverified vindication. **A wobbly economic mechanism laid
down at genesis cannot be taken back; a documented absence can.**

What is implemented is therefore the absence itself, made executable:

1. **The gap is measured, not asserted** (T1/T2, and the T3 slash/vindication pair as its witness).
2. **The absence is pinned in both directions** (T3): across a vindication *and* a hard slash, every
   anchored juror ends with the stake it began with, the operator balance it began with, and
   `Pools.MinerPaid` does not move. Wiring a juror payment — or a juror penalty — turns this red.
3. **The inertia of `silence_slash_bps` is pinned** (T4): `clawbackPayment` must exist exactly once
   and be called from nowhere. Both directions go red — a new caller, or a vanished definition.

Nothing about the running chain changes. That is the point: the decision is reversible, and the tests
are what make reversing it a deliberate act rather than an accident.

## 5. What must exist before any incentive can be considered

The gate is a **prerequisite**, not a wish. In order, and none of them optional:

1. **Make abstention a first-class, distinguishable token.** "Present and undecided" must be
   expressible and must cost a juror no more than a vindication. Until then, every incentive has a
   cheapest satisfaction that is a lie. This is a change to the verdict alphabet, hence to
   `auditVerdictTally`, hence **consensus-breaking**: it belongs to a genesis, and it must land
   *before* any incentive, never alongside one.
2. **Then make effort observable, or accept that it never will be.** With abstention expressible, an
   incentive can at least be attached to something other than "emitted a byte". Without it, no
   incentive can be. ADR-035 (deterministic verification by recomputation) is the only route on the
   table that would make a juror's work checkable; it is deferred behind a cross-GPU measurement, and
   this record does not unblock it.
3. **Only then, size a payment on data.** `redo_participation` (`seats` / `responders`, emitted at
   `audit_sampling.go:377-383` and its adjudication counterpart) is the measurement point that already
   exists. Sizing a juror reward before that series has a shape would be guessing with the genesis.

Until step 1 ships, the honest public statement is the one this record makes: **on Dendra, judging is
voluntary and unremunerated, and the protocol says so rather than implying otherwise through an inert
parameter.**

## 6. What this record does not claim

- It does **not** claim jurors have no incentive at all. One exists and is weak: posting a verdict on
  an anchored seat dates a miner's vitality (`commitProvesAssignedWork`, case (a)), which keeps it in
  the jury pool and away from `pruneInactiveMiners`. For a small miner rarely drawn as primary, that
  is its **only** vitality path. For a large one it is redundant with mining, so the incentive does
  not discriminate where it would need to.
- It does **not** claim the launch is safe without jurors. It claims the opposite, in §3(a): if
  nobody judges, audits defer forever and the network verifies nothing. `held_total` and
  `held_oldest_age` are the gauges that make that visible, and they must be watched from the first
  epoch.
- It does **not** remove `silence_slash_bps`. Removing a numbered proto field changes the parameter
  encoding, hence the app-hash: that is a **consensus** change, and it belongs to the pass that
  rebuilds the genesis — the one `params_inert_test.go` records in `champsRetires`, where a withdrawn
  field number is pinned so it can never be recycled and the name can never come back. ADR-038 does
  not make that call. What it does is keep the inertia **explicit and executable** in the meantime
  (T4), so that the field is either retired deliberately or left inert deliberately, never simply
  forgotten while the genesis keeps advertising it as an active deterrent.
