# ADR-037 — Freezing the drawing pool: one cannot enter a pool whose seed is already known

**Status:** **Accepted — implemented.**
**Corrects:** [ADR-032](ADR-032-comite-audit-ancre.md) and
[ADR-033](ADR-033-comite-readjudication-ancre.md) anchored **the committee drawn**; neither anchored
**the pool it was drawn from**.
**Complements:** [ADR-034](ADR-034-vitalite-du-mineur.md),
[ADR-036](ADR-036-vitalite-de-jure.md) (vitality is **never** an entry filter, so it is not vitality
that can close this hole).
**Implementation:** Implemented — `chain/x/jobs/keeper/pool_freeze.go` holds the predicate;
the anchor is set in `msg_server_open_job.go` and re-armed in `genesis.go`; all three draw sites
(`committee.go`, `audit_committee.go`, `redo_committee.go`) consult it. The new window invariant is in
`types/params.go::Validate`. **Consensus-breaking: it ships with the next genesis, like ADR-036.**

---

## 1. The defect, measured

The three draws each re-read the **live pool** on every call, with no snapshot at all.

The ordering is stake-weighted: the score is `h/stake` with `h = sha256(seed|id)`, compared by integer
cross-product. **That weighting resists splitting a stake** — measured: at constant total stake,
splitting into 2, 4 or 16 identities moves the probability of obtaining the first slot from **51.4 % down
to 42.1, 40.3 and 38.6 %**. Multiplying *costs*.

**It does not resist choosing the identifier.** Once the seed is public, one tries candidate identifiers
**offline** until `h` is tiny; the score `h/stake` then wins with a derisory stake. Measured against the
exact comparator, with an attacker at **1** stake against an honest miner at **1,000,000** — probability
of obtaining the first slot: **3.5 % at 2^16 attempts**, then (closed form, validated over three orders
of magnitude) **38 % at 2^20, 94 % at 2^24, close to 100 % at 2^40**. At 2^40 attempts an attacker with
1 stake still wins **99.9 %** of the time against an honest miner with a billion.

> **Hashing is free, stake is not.** Any economic weighting is moot if the adversary chooses its
> identifier *after* seeing the seed.

**What that permits, concretely**: miner creation is functional and permissionless, so the attacker
places itself in the first slot of an **already open** job and blocks its settlement for the price of
gas; or installs itself in an audit jury and weighs on a slash verdict.

**And the file carried a written proof of a property it did not have.** The sampling code asserted that
"the seed is LATER than the primary's commit, therefore nobody can place themselves in the committee".
Unpredictability stops a miner that is **already registered**; it does not stop a miner **created
afterwards**. That justification was withdrawn and replaced by the measurement.

## 2. The decision

**A draw's pool is frozen at one height, and eligibility is read as a comparison of two anchoring
dates.**

1. **A dedicated collection**: `JobPoolFreezeHeight : jobId → uint64`, set **once, at job opening**.
2. **A single predicate, shared by all three draws**: `m` is drawable for `job` **if and only if**
   `MinerRegisteredHeight[m] <= JobPoolFreezeHeight[job]`.
3. **`<=`, never `<`.** Equality is **load-bearing** (§3.b): with `<`, the entire imported population
   becomes ineligible.
4. **No default, in either direction.** A missing anchor is an **ERROR**, never "permissive" and never
   "restrictive". A draw that cannot establish its anchor **defers with an explicit reason**; it neither
   includes nor excludes silently.
5. **Genesis import**: `JobPoolFreezeHeight` is **re-armed to the init height**, by the same code and at
   the same block as `MinerRegisteredHeight` (§3.b).

## 3. The three traps, and why the obvious form fails

### a. The dispute height as an anchor — REFUSED

The first design reused the job's dispute height for the audit, adding no new field. **It produces a
total silent failure.**

- The genesis code explicitly permits a **negative** value there, justified by "this is intended and
  safe: the dispute height is a signed integer and **no business logic tests its value**". The design is
  precisely the business logic that would test it. **Reusing a field whose safety is conditioned on
  nobody reading it removes the condition without touching the conclusion.**
- A dispute height of zero means "never disputed", indistinguishable from a legitimate height.
- **And after an import it is false for everyone**: the genesis code sets `MinerRegisteredHeight` to the
  init height for **every** imported miner, and the dispute height to init height minus the elapsed
  window, hence **strictly lower**. `registered <= dispute` becomes `initH <= initH − elapsed`, which is
  **false for every miner on every imported disputed job**, giving empty committees and therefore, by
  ADR-034, **unbounded deferral**. Neither red nor an error: audits that never close.

### b. The rule that generalises, and applies beyond this record

> **Two anchors that are compared must be set — and re-armed — by the SAME code, at the SAME height.**
> Otherwise their relative order is not a fact of history: it is an artefact of the migration code, and
> it changes without a single line of the rule moving.

That is exactly what kills (a): both values are reinvented at import, in the opposite order to the one
the rule assumes. Hence §2.5: the freeze height is re-armed to the init height, like the registration
height, which makes `initH <= initH` **true** for the whole imported population — correct, since they
were all there before, and any identity created after the import carries a **strictly greater** height.

### c. Freezing "at the height the draw is armed" — REFUSED, and the subtlest trap

The natural form would be an anchor **per draw**, set when each draw is armed — a fresher pool and wider
juries. **It is wrong in the regime we are launching in.**

Without a decentralised seed, the committee base seed **falls back to the previous block's app hash**.
But a single contributor is the assumed launch topology, so **the legacy fallback is the normal regime**,
and the anti-grinding property is inactive. The seed of block `H` is therefore derivable as soon as
`H−1` is committed: an attacker grinds its identifier offline and submits miner creation **inside block
`H`**. An anchor set at `H` with `<=` **accepts it**.

A per-draw anchor is therefore safe only if one **proves, for each draw, that the seed is not derivable
at the freeze height** — a proof that depends on the **seed source**, which varies (decentralised, legacy
fallback, future evolutions). **An invariant conditioned on a variable mechanism is exactly the pattern
that has been expensive here.**

**Decision: one anchor per job, set at job opening, shared by all three draws.** It precedes any seed by
construction, whatever the source, with no freshness proof to redo. The anchor that **needs no
hypothesis** is preferred to the anchor that yields larger juries.

## 4. The cost, stated before anyone raises it

**A job's eligible pool can only shrink with its age**: a miner registered after the job opened will
never judge that job. Combined with eviction of the inactive
([ADR-034](ADR-034-vitalite-du-mineur.md), [ADR-036](ADR-036-vitalite-de-jure.md)), **a sufficiently old
job can become unjudgeable** — degrading into *deferral* (never into a sanction), but real.

The age bound is the window during which a jury can still be requested, that is, the dispute window.
Hence the **new invariant**, validated alongside the one from ADR-036:

> `miner_prune_blocks >= dispute_window` — otherwise the registry can evict the only jurors the freeze
> still permits to judge.

*(ADR-036 already imposed `miner_prune_blocks >= juror_freshness_blocks`; these are two distinct
relations, and neither implies the other.)*

## 5. What is explicitly REFUSED

- **An entry filter on vitality.** Juror eligibility deliberately welcomes a new miner — a new node has
  never judged, and requiring a recent verdict would make it ineligible for life. **The freeze must
  therefore act on a DIFFERENT dimension**: the registration date compared against a freeze height,
  **never** activity. The prohibition of ADR-034 §2.3, maintained.
- **Making miner creation permissioned.** The remedy would be worse: an entry barrier is precisely what a
  testnet must not have.
- **Raising the minimum stake.** It does not attack the cause: grinding wins at 1 stake against a billion.
- **Recomputing an already-anchored committee** from the frozen pool. Anchored committees
  ([ADR-032](ADR-032-comite-audit-ancre.md), [ADR-033](ADR-033-comite-readjudication-ancre.md)) keep
  their seats: draws in flight at the time of the fix are not rewritten.

## 6. Acceptance conditions — non-negotiable

1. **One red-before test per draw site** (three), proving that an identity created after the freeze is
   refused.
2. **A GENESIS ROUND-TRIP test** that fails if an imported miner ends up ineligible on an imported job.
   **That test, and it alone, would have caught §3.a** — the defect is in **no** draw site, it is in
   their **interaction with the import**. A per-site test would never have seen it.
3. **An absence test**: a missing anchor produces an explicit error, never a silently permissive draw.
4. **An equality test**: `registered == freeze` means **eligible**, locking `<=` against a future
   "simplification" to `<`.

## 7. Consequences

- **Positive**: identifier grinding stops being exploitable on all three draws; stake weighting regains
  the meaning it is supposed to have; the blocker on public opening closes.
- **Accepted cost**: §4 (the pool shrinks with job age); draws in flight at the time of the fix are not
  corrected.
- **Consensus-breaking**: does not land on the running chain — it **ships with the next genesis**, like
  [ADR-036](ADR-036-vitalite-de-jure.md).
- **Not measured here**: all of §1 is a simulation against a faithful copy of the comparator, not against
  the binary; §3.c is a reading of the seed-fallback code, not an attack that was played.
