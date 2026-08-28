# ADR-036 — Juror vitality: a seat that cannot vote must neither be drawn nor immobilise

**Status:** **Accepted** — code and tests green (the open-obligation clause now tests a predicate, the
window ordering is validated, and the eviction path is covered). **Consensus-breaking: it does not land
on the running chain, it ships with the next genesis.**
**Complements:** [ADR-034](ADR-034-vitalite-du-mineur.md) (miner vitality),
[ADR-032](ADR-032-comite-audit-ancre.md) (anchored committee).
**Implementation:** Implemented — `chain/x/jobs/keeper/miner_vitality.go`
(`eligibleAsJuror`, `hasOpenObligation`), and the window-ordering invariant in
`chain/x/jobs/types/params.go::Validate`.

---

## 1. The problem, measured rather than deduced

[ADR-034](ADR-034-vitalite-du-mineur.md) gave the protocol the notion of a "demonstrably alive" miner
through the height of its last commit, refreshed by **any assigned commit**. But **an inference commit
and an audit verdict refresh the SAME date**. The consequence, observed in production:

> **A node that MINES and never JUDGES stays eligible as a juror indefinitely.** It occupies seats,
> **raises the two-thirds bar** it never helps to reach, and is **never evicted**, because it produces.

Measured state: 8 registered miners, **5 voters**, **3 silent**, giving 7 drawable seats and a bar of
`max(4, ⌈2/3 × 7⌉) = 5`, against **4** available voters. **4 < 5: no audit could close.** The general
solvency condition — **`J ≥ 2M`** (J voters, M silent seats) — was written **nowhere** in the protocol.

**And a circular lock compounded it**: the open-obligation clause refuses the exit of a miner sitting in
an **unresolved** audit; eviction goes through the same guard; audit deferral is **unbounded**
(ADR-034, an owned decision). Composed: **a dead identity sitting in a deferred audit can NEVER be
evicted, neither voluntarily nor automatically.** The seat does not leave because the audit is not
resolved, and the audit is not resolved because the seat does not vote.

**This is not a laboratory case**: two rented GPUs were returned mid-audit, with their keys. On a public
testnet, a consumer card unplugged during an audit produces exactly that — and the cost is borne by the
**client** (fee withheld) and by the **network** (a raised bar for every subsequent audit).

## 2. The decision

**Two states the code conflated are now distinguished — this is the generic remedy for the seventh form
(see [ADR-034](ADR-034-vitalite-du-mineur.md) §3), not a threshold tightening.**

1. **The open-obligation clause applies only if the juror is STILL summonable**, that is, if the juror
   eligibility predicate evaluates to true. Holding someone in an old committee while refusing to draw
   them into a new one is **incoherent**: the reason to hold a juror is that it **votes**; if it can
   demonstrably no longer vote, holding it achieves nothing and **costs everyone**.
2. **The rule applies to BOTH paths** — voluntary exit **and** eviction. *Decisive reason: the keys of
   dead identities are lost with the machines, so voluntary exit is closed to them forever. **Only
   eviction can remove an identity whose key has disappeared**, which is precisely the case that
   motivated this record.*
3. **New invariant**: `miner_prune_blocks ≥ juror_freshness_blocks`, validated in `Validate()` (a dormant
   pattern: only what is explicitly governed is compared). *The reverse ordering is incoherent in
   itself — the registry would evict someone still considered summonable.*

**The rule is written on the PREDICATE, never on a supposed ordering of the windows.** A first formulation
relied on "every eviction candidate is stale by construction, because the compiled default windows are
ordered that way". **True for the compiled defaults, false in general**: both windows are **governable
parameters** (ADR-034 §5). A governance decision inverting them would have collapsed the reasoning
without a word. **Test the state, do not assume the configuration** — and the invariant (3) is only a
**second belt**: the clause does not rest on it, otherwise both guards would fall together.

## 3. What is explicitly REFUSED, and why

- **Releasing the seat "once the audit is past its deadline".** An audit past its deadline without a
  quorum is exactly the case of a **primary whose audit could not conclude**. A cheater able to drag out
  its own audit would wait for the deadline and **leave with its stake**: the slash would become evadable
  by waiting.
- **Lowering the minimum quorum** so the bar clears. That would be *obtaining green by lowering the
  requirement* — forbidden, and it would hollow out the audit on the very network being offered for
  testing.
- **Excluding vanished members from the denominator of an ALREADY anchored committee.** Because removal
  is self-service, this would offer a lever to **lower the bar mid-audit** — a grinding vector worse than
  the disease. **Already-anchored committees keep their dead seats**: audits in flight at the time of the
  fix are lost, and that is accepted.
- **An ENTRY filter.** The ineligibility predicate must distinguish **"summoned and never voted"** from
  **"never summoned"**: a new node has never judged, and requiring a recent verdict would make it
  ineligible for life (a deadlock). That is the prohibition of ADR-034 §2.3, maintained.

**What the open-obligation clause still guarantees**: a **live** summoned juror cannot desert. The other
clauses — a withheld fee, being the primary of a disputed job — are **intact**: **no audited party can
escape.** Only the **juror** role is released, and only once the protocol has itself established that the
role is no longer tenable.

## 4. Honest scope — what this record does NOT do

**This is hygiene, not an unblocking.** Removing a miner from the registry **rewrites no already-anchored
committee**: no in-flight audit's bar goes down. What it buys: voluntary exit becomes possible again, the
bond returns to its owner, the registry stops accumulating dead entries, and **the dead seat is no longer
drawn into NEW committees**.

> **The real unblocking is the number of live miners.** The target is **seven**, not five. At five the
> margin is nil; the seventh identity is free, and the eighth **degrades** the situation by raising the
> bar.

## 5. Consequences

- **Positive**: the "mines without judging" deadlock can no longer form; an identity whose key is lost
  stops immobilising a seat for life; the ordering of the two windows is bounded instead of hoped for.
- **Accepted cost**: audits in flight at the time of the fix stay frozen (anchored committees); the
  behaviour is proven only in unit tests — **no real eviction has been observed on a live chain**, and
  none will be until dead identities cross the eviction window.
- **To be exposed**: `J ≥ 2M` must become a **published measurement**, not a discovery. A network that can
  no longer arithmetically close an audit must **say so** before it is noticed in the logs.
- **Test discipline reinforced by this work**: *a test must depend on what it asserts.* A retention test
  whose juror has **no date at all** goes through the **benevolent fallback** — it tests the fallback, not
  the retention, and it will silently change meaning the day someone dates the fixture.
