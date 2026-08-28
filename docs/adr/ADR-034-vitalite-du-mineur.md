# ADR-034 — Miner vitality: "demonstrably alive" rather than "registered"

**Status:** **Accepted — fully implemented.** The findings F1, F2 and F3 below are corrected and
covered by discriminating tests, several of which were red against the previous code. The last
remaining motive — an audit outlasting the prune window — is exercised by a dedicated test with the
red-before state executed in **both** directions (guard removed: the primary is evicted; replay
disabled: the honest miner is never paid).
**Complements:** [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) (optimistic audit),
[ADR-032](ADR-032-comite-audit-ancre.md) (anchored committee),
[ADR-033](ADR-033-comite-readjudication-ancre.md).
**Code:** `chain/x/jobs/keeper/miner_vitality.go`, `audit_jury_floor.go`,
`msg_server_commit.go`, `types/keys.go`.

---

## 1. The problem, as measured

Until this record, the **only known quality of a miner was "registered in the registry"** — and that is
bought **once**, for `min_stake`. Two consequences observed in production:

- **(a)** three jobs served by an **honest** primary (real inference, correct answer, anchored commit)
  ended resolved and clawed back: their payment was **taken back** because the audit could not complete.
  There were two eligible jurors, both **silent**, against a floor of four. **The quorum was unreachable
  by construction**: no conduct by the miner could have avoided the clawback. That is "billing an
  infrastructure failure to the honest party", the invariant this project refuses to break.
- **(b)** nothing evicted those identities: the silence penalty lives in the clawback path, which
  **presupposes a prior payment** — but an identity that produces nothing is never paid, and therefore
  **never punished**. The protocol knew how to punish a paid miner, not how to evict an inert one.

**This is not merely a cold-start problem, it is an attack vector**: registering N silent identities
costs `N × min_stake` **once**, occupies committee seats without ever voting, and produces **serial
no-quorum, hence serial clawbacks on honest miners**. At a modest N, the network stops paying anyone who
works, and **the only parties punished are those who produce**.

## 2. Decision

**The common primitive is the height of the last anchored commit** (`MinerLastCommitHeight`). Anchoring
a commit is an act **signed by the operator and carrying a result**: the notion moves from "registered"
to "demonstrably alive", exactly as [ADR-032](ADR-032-comite-audit-ancre.md) moved from "registered" to
"drawn".

1. **An insufficient jury means DEFER, never sanction.** When the eligible pool is below the floor, the
   audit is deferred by a wide step, the **retention is kept**, and the primary is **neither slashed nor
   clawed back**. The deferral is **unbounded and owned**: releasing for lack of an audit would make
   evasion free. A security-level log and a deferral event make the state loud.
2. **Juror eligibility means a recent commit**, with a deliberately **very wide** window (about three
   days). The window does not sort the good from the less good: it excludes those who **have
   disappeared**.
3. **Benevolent default**: a missing entry means eligible. Two innocent cases produce it — a **new**
   miner (this must never become an entry filter) and a **genesis import** (the collection is not
   exported; without the default, a migration would empty every jury).
4. **Eviction of the inert** (once per epoch, about seven days): **objective** (an on-chain criterion
   anyone can verify), **non-discretionary** (no parameter designates a target — a chosen removal would
   be a censorship vector), and **non-confiscatory** (the bond returns to its creator; inactivity is an
   absence, not a fault). A failed refund **cancels** the removal.

**Intended economic effect**: the cost of an attack by silent identities stops being a **one-off
payment** — the attacker must re-register, hence re-bond, indefinitely.

## 3. The class this record establishes

> **A security artefact must be able to produce the result that contradicts it.**

A reproducible query, to be run against **every** guard, test, measure or gate:

1. What **observable value** would make this artefact **red**?
2. Is that value **reachable by construction**?
3. Which **form** of the targeted defect would **escape** the pattern used?
4. Does the artefact bite on the **population it targets**, or only on a neighbouring one?
5. If the answer to (2) is "no" or to (4) "neighbouring", **the artefact verifies nothing** — and it is
   **worse than absent**, because it reassures.

**Nine known forms**: a test that cannot fail · a guard that cannot bite · a measure that cannot
contradict · an alarm that always cries · **a pattern that misses the variant** · **an invariant
unverifiable at N=1** · **a guard that strikes the NEIGHBOURING population**, because one signal encodes
two distinct states (§4, F1) · **anti-replay specific to a PATH** — when N paths can sanction the same
fact, a per-path marker protects nothing: the anti-replay predicate must be **COMMON to the class** ·
**a sum whose BOTH sides come from the same enumeration** — any conservation check whose two sides derive
from the same walk is true by construction; the right-hand side must come from an **INDEPENDENT**
counter (the partition check compared a sum of classes against a total counted by the same loop; it was
corrected by an on-chain counter of audits opened, with a falsifiable case in the self-test).

**Added corollary (the vindication/slash asymmetry).** A threshold expressed as an **absolute value** on
a committee of variable size is the same fault in economic form: since
[ADR-032](ADR-032-comite-audit-ancre.md) a slash requires two-thirds of the anchored seats, but
vindication had stayed at the absolute floor. On a stake-weighted draw of fifteen seats, roughly a
quarter of the stake expects about four seats — so a quarter of the stake **bought the vindication of any
cheating**, and the same quarter kept **silent** forced the no-quorum, hence the clawback of honest
miners. **Rule: what distinguishes two outcomes of the same committee is the DIRECTION of the majority,
never the BAR to be heard.** And when no bar is reached, there is no verdict: there is an audit that did
not happen — hence **deferral with retention kept and no sanction**, as under the jury floor (§2.1).

**Operational corollary — no gate turns green on a pool of one.** An invariant about the behaviour of a
**collective** (quorum, plurality of judges, independence of errors) is **unverifiable** as long as the
collective does not exist; a green obtained at N=1 measures the machine, not the network.

## 4. What review found — the guard did not yet bite its target

**The two findings are fixed together or not at all.**

**F1 — CRITICAL. The benevolent default misses exactly the population it targets.**
`MinerLastCommitHeight` was written **only** on the commit path — **never at miner creation**. So a
registered identity that **never** commits never has an entry, and therefore: stays **eligible as a juror
forever** **and** is never evicted. That is *literally* the population described in §1 — "registered and
silent". The code comment claiming "stays eligible until the window applies" was **false**: with no
entry, the window never applies.

- **Cause**: **one signal (`!ok`) encoded two distinct states** — "new or imported" (innocent) and
  "registered long ago, never produced anything" (the target). They are **distinguishable**.
- **Fix**: date the **registration** (`MinerRegisteredHeight`, written at miner creation). `!ok` then
  becomes readable: `h − registered ≤ window` means new, benefit of the doubt; greater means registered
  long ago and silent, hence **the target**. Both guarantees of §2.3 are preserved (a new miner stays
  eligible; **neither** date present means genesis, hence benevolent).

**F2 — CRITICAL (its twin). The vitality proof was SELF-ISSUED.**
The commit handler verified that the miner exists and that the signer is **its** operator, and refused
overwriting — but **never verified that the job exists**, nor that the miner was assigned to it. An
operator could anchor a commit against a non-existent job identifier and obtain fresh vitality **for the
price of gas**.

- **Direct consequence**: fixing F1 **alone** closes nothing — it converts a **free** attack into an
  attack costing **one transaction every three days**. Vitality would then measure willingness to pay
  gas, not production.
- **Fix**: do **not** harden the commit itself (backwards compatibility), but **gate the writing of the
  date** on the existence of the job. **A trap not to miss**: the verdict key is
  `<jobId>__verdict__<minerId>`, so a naive existence check on the prefix would **break verdicts** —
  precisely the act §2 wants to count as production. The base identifier must have the `__verdict`
  suffix stripped before the job existence check.

**F3 — SERIOUS. Composing the two guards destroys an honest miner's payment.**
The retention-release path removed the held-fee entry **before** loading the miner, and returned bare if
the miner had disappeared: the retention was **erased from the store** and the money **stayed at the
module**. The path is reachable: audit deferral is **unbounded** (§2.1), so a job can stay in audit for
longer than the eviction window while the eviction pass removes the miner (which has nothing left to
commit). The withheld payment of an **honest** miner is therefore destroyed, silently, **and the
accounting key disappears** — triggered exactly in the situation both guards target: **a small pool**.
It violates "never bill an outage to the honest party" **and** "escrow stays balanced".

- **Fix**: (i) eviction refuses to remove a miner holding an **open retention** or an **anchored seat**;
  (ii) the retention entry is removed only **after** a successful transfer.

## 5. Consequences

- **Positive**: the attack by silent identities stops being a one-off payment; an impossible audit no
  longer costs the honest party anything; eviction is objective, hence non-censoring; judging counts as
  producing.
- **Accepted cost**: an attacker can still **delay** an audit (the deferral is unbounded) — it can no
  longer make an honest miner **lose**. That is the right side of the trade.
- **Open — both windows are CHOICES, not measurements** (about three days for juror freshness, about
  seven for eviction). Too short and an outage gets billed to the honest party. The measurement that
  would fix them: the **distribution of inter-commit intervals of honest miners on an open testnet**;
  the juror window must exceed the 99th percentile. Until that distribution is measured, **keep them
  wide** (the safe direction) and **promote both to governable parameters** — a value known to need
  correction on data must not require a binary upgrade.
- **Dependency**: the guards here can only be **evaluated** with a partition of counters distinguishing
  audits closed **by quorum**, **by timeout**, **deferred**, and **by adjudication** — without it, "the
  network verifies" cannot be told apart from "the network never verified". See §3, the form "a measure
  that cannot contradict".
