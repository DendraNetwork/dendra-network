# ADR-045 — Nine measured defects awaiting arbitration, and why none of them is a one-line fix

**Status:** **CLOSED — all nine are FIXED.** (9), (11), (13), (15) and (16) were taken first, then (10), (12), (14) and (17) once the owner arbitrated each. Kept as the record of what was measured, decided and why; the original text follows. Formerly: (9), (11), (13), (15) and (16) are FIXED and their pinning tests flipped; (10), (12), (14) and (17) carry the owner's decision and await implementation.** The three taken are exactly those whose fix needed no new state, no genesis field and no migration, carried by a fresh genesis that was owed anyway — the kit builds at consensus epoch 11 while the running network declares 5, so `join.sh` refuses every joiner. The epoch is now 12.

**Original status:** **Proposed — owner arbitration required, nine times over.** Nothing here is a diagnosis
waiting to be confirmed: every entry was reproduced by running the shipped code, and each carries the
measurement that produced it. What none of them carries is a decision, because each fix either changes
the **shape of consensus state**, changes **who may act**, or trades one failure for another.

> **Why one record for nine defects.** They came out of two adversarial sweeps in a single session, and
> they share a structure worth naming once rather than nine times: *a guard, a claim or a key was built
> for one question and is being asked another*. Splitting them into nine records would hide that, and
> would ask the owner to read nine preambles that say the same thing. The measurements are per-entry;
> the pattern is common.

## The pattern, stated once

Every defect below is a **mismatch between two sets, two moments or two meanings** that the code treats
as one:

| Confusion | Where it bites |
|---|---|
| assigned ≠ authored | ⑯ anti-collusion exclusion |
| drawn ≠ still registered | ⑫ live weight · ⑮ exit after answering |
| named ≠ owned | ⑩ `/capacity` verified block · ⑭ reveal key |
| a share of X ≠ a scale on X | (closed, epoch 9) |
| absent ≠ zero | ⑪ quorum zero · ⑬ retention age |
| paid ≠ settled | ⑨ `SettlePay` escrow |
| deposited ≠ anchored | ⑰ miner daemon de-duplication |

None of these is exotic. Each was written by someone who knew the rule and applied it to the neighbour
question, which is exactly why re-reading does not find them and running them does.

## The nine, each with what was measured

### ⑨ `SettlePay` strands the escrow `OpenJob` took

**✅ FIXED 2026-08-21** — `SettlePay` refuses a job that still holds the escrow `OpenJob` took, and the refusal names the protocol path so an operator is not stranded in place of the escrow. Refusing was chosen because it MOVES NO FUNDS; it does not touch the optimistic flow, which settles through `SettleSemantic`.

`OpenJob` moves `msg.Fee` into the module and books an expiry appointment. `SettlePay` pays the winners
from the **client's own wallet**, never touches that escrow, then appends `+settled`, which is terminal:
`expireUnservedJob` refunds only a state that is exactly `"open"`, and `Payout` / `SettleSemantic` /
`SettleJob` all refuse through `jobIsPaid`.

**Measured** (`settlepay_strands_escrow_test.go`): the client pays twice, one payment reaches nobody,
and after every disbursement entry point plus the EndBlocker at the booked appointment, the escrow has
not moved. Control: the same job settled through `SettleSemantic` empties the module.

**Choices.** Release the escrow to the client in `SettlePay` · **refuse** `SettlePay` on a job that
carries one · retire the message. *Recommended: refuse — it moves no funds, and a job with an escrow is
not this handler's business.*

### ⑩ The `verified` block of `/capacity` is won by NAMING a staked miner

**✅ FIXED — signed reports prove the OPERATOR, unsigned ones are still accepted.** The deposit verifies a signature whose address must be the operator the registry records for the miner id reported; the store key follows the PROOF, so one identity holds one entry instead of unbounded ones; and the `node_id in onchain` shortcut is gone — a chosen name proved nothing. ⚠️ No replay protection on this endpoint, and it is written rather than implied: re-posting a captured report overwrites the same key with the same content. Services only, no epoch. Originally arbitrated as: The label is right and the proof was missing, so the proof is added: reports signed with the miner's operator key feed the `verified` block, unsigned ones stay visible outside it. Refusing unsigned outright was rejected for the reason the relay already paid — one does not add a requirement to an operator that was running yesterday, and a per-route policy exists precisely so that reading and writing are not held to the same bar. ⚠️ The store key must become the SIGNED identity: `machine::node_id` is attacker-chosen at both ends, which is the escalation this entry names.

`aggregate()` restricts its totals to nodes whose identity appears in the on-chain registry, and the
served `_provenance` says why that block is the one to trust: *"Inflating it requires staking first"*.
The match is `set(rep["miner_ids"]) & onchain`, and `miner_ids` is whatever the reporting node wrote.
`do_POST` carries no signature and no authentication; the registry side is a public GET.

**Measured on the live endpoint**: the intersection of announced `node_id`s and registered miner ids is
**empty**, yet one node — an operator-chosen name, not a miner id — carries `registered_onchain: true`
and constitutes the entire verified block. Pinned in-process by
`test_capacity_declared_identity.py`, including the escalation: the store key is `machine::node_id`,
both attacker-chosen, so one party holds unlimited entries all naming the same staked id, and the block
sums them.

**Choices.** Sign the capacity report with the miner's operator key (the relay already signs deposits) ·
stop labelling the block "staked". *Recommended: sign — the label is right, the proof is missing.*

### ⑪ At `audit_min_quorum = 0`, admission demands nothing and the draw demands four

**✅ FIXED 2026-08-21** — the floor is now `effectiveSlashFloor(p) + 1`, and the refusal names the EFFECTIVE floor instead of the raw parameter, which announced 0 while demanding 5. Reachable at quorum zero only: the live chain governs the quorum to 4 and already demanded five, so production behaviour does not change.

`enoughEligibleMinersToVerify` refuses a job whose frozen pool could never supply a jury — pools freeze
at birth and never widen (ADR-037), so an under-supplied job is unverifiable forever. It builds its
floor from the raw parameter and returns `nil` at zero. `effectiveSlashFloor` returns 4 there, and it is
the function the draw consults.

**Measured** (`quorum_zero_pool_floor_test.go`): in optimistic mode with four eligible miners the job is
**accepted**, escrow taken, pool frozen. The same pool with the quorum *governed* to 4 — the value the
zero already selects — is **refused**.

**Choices.** Ask `effectiveSlashFloor` at admission — correct, and it extends the bootstrap ratchet (no
job until five miners exist) to quorum zero; measured cost, and **the recorded number was wrong**: **23**
fixtures — not four — opened jobs into pools that could never supply a jury, because the guard is called
unconditionally at `OpenJob`, not only in mode 1. They were repaired by seeding **zero-stake** miners,
which satisfy both predicates the floor counts while carrying no weight in the draw — itself a
measurement about the guard: it counts identities, not capacity · bound the deferral instead, releasing the held fee
after N attempts — which decides *who receives* the fee of an unjudgeable job, a tokenomics decision.

### ⑫ The work committee is re-derived from the LIVE stake

**✅ FIXED — anchored in a DEDICATED collection carried by genesis, at the first moment the committee is DERIVABLE.** Not at `OpenJob`: with a reveal delay the beacon seed is not there yet, so both hooks exist (open, and the EndBlocker that reveals) and both are driven end to end. ⚠️ That mattered — placed just after the seed, the anchoring anchored NOTHING, because the freeze height it derives from is written further down, and the read falls back to the derivation so the failure was silent. Originally arbitrated as: It removes the class rather than the symptom, and aligns two mechanisms that diverge today for no reason. ⚠️ It takes a DEDICATED collection with its own genesis field, not a new key namespace inside `AuditCommittee`: three consumers of that store read every entry as a JURY SEAT (`hasOpenObligation`, `held_summary`, the genesis export), and lodging a work committee there would commit the exact confusion this record catalogues — two meanings the code treats as one.

ADR-037 froze *who* may be drawn and *when* vitality is evaluated. The third input — the ordering weight
— is read live from the miner record.

**Measured** (`committee_weight_not_frozen_test.go`): slashing a **member** for an unrelated job moves an
open job's committee from `[cm2 cm1 cm5]` to `[cm2 cm5 cm6]`. The member that answered is no longer
assigned, a stranger is, and `SettlePay` refuses — escrow with no settlement path. Control: slashing a
**non-member** changes nothing, so what moves the draw is the weight of a drawn miner.

**Choices.** Snapshot each drawn miner's weight at the freeze (new per-job state, genesis field,
migration) · anchor the work committee at `OpenJob` as the audit committee already is · draw on a value
that cannot move, losing the economic weighting the assignment cap exists to bound.

### ⑬ A retention age is lost whenever the restart height is below it

**✅ FIXED — accepted and DECLARED, not migrated.** Storing the age instead of an anchor would take a collection, a genesis field and a migration for a figure that is exact as soon as the height passes the ages carried over. The loss is now stated where the figure is READ (`client.py::held_summary`) and where it is JUDGED (`c3_gate.py`, check `held_lock_age`) — the second matters more: that check raises its alarm on `age > threshold`, so an understated age fails to alarm, and the miss is on the QUIET side. ⛔ The gate cannot detect the clamp on its own: it holds no chain height, and giving it one would add a dependency this arbitration did not buy. It says so instead of guessing.

Ages are exported as elapsed blocks and re-anchored `since = h − elapsed`, clamped at 0.

**Measured**, one held fee exported with age 60, re-imported at three heights and read 29 blocks later —
the faithful answer being 89 every time: **h=1 → 30**, **h=30 → 59**, **h=200 → 89**. A chain restarted
from a fresh genesis therefore understates every retention age for its first blocks, and
`held_oldest_age` is what `audit_sampling.go` designates as the figure that makes *"held is not lost"*
falsifiable. The bench's `>= 29` bound hid it; the three regimes are now pinned as equalities.

**Choices.** Store the age rather than an anchor (collection, genesis field, migration) — a `uint64`
anchor **cannot** express "60 blocks old" at height 1 · accept the loss and say so on the surface that
displays it.

### ⑭ A reveal is signed by its writer and keyed by its reader

**✅ FIXED — the key now ends with its AUTHOR, for every kind without exception.** A reveal is keyed `<jid>__<recipient>__<primary>`, so the relay's positional attribution — the segment after the last `__` — designates the party that WRITES. Uniform rather than a per-kind special case: a rule with no exception cannot forget one. The reader names the author it expects, which is a property rather than a constraint — a reveal posted by anyone else no longer occupies the slot the primary must fill. Services only, no epoch. Originally arbitrated as: Asking the chain who the job's primary is was rejected: it would give the relay a dependency on the chain that the design deliberately avoids — the relay is presumed hostile and independent, and an RPC outage would become a deposit outage.

`_miner_of_key` takes the segment after the last `__`. Right for `res/<jid>__<mid>` and for
`pub`/`attest`; wrong for a reveal, keyed `<jid>__<recipient>` — one sealed copy per committee member —
where the writer is the primary.

**Measured** against the shipped server in `enforce`: the primary signing its own reveal gets **401**;
the recipient signing the same deposit gets **200**. Control in the same lot: the same identity and
signing code on `res` gets 200, so the cause is the key shape.

**Not a live outage**: `DENDRA_RELAY_SIGN` defaults to `off` and nothing under `deploy/` or `docker/`
sets it. It is a trap on the upgrade path the per-route policy exists to enable — and the policy's own
comment says a sampled job with no reveal never resolves its held fee.

**Choices.** Change the key shape and every reader that fetches `<jid>__<my_id>` · learn from the chain
which miner is the job's primary. *Accepting "any registered miner" would let a stranger occupy the key
first, and write-once would then lock the honest deposit out — a refusal traded for a squat.*

### ⑮ A member that answered may leave the registry

**✅ FIXED 2026-08-21** — a fourth clause holds a miner that has an anchored `Commit` on a job that is neither paid, refunded nor resolved. The bound is the JOB's own terminal state, never the miner's goodwill. ⚠️ And the case that covered the EVICTION half proved nothing: it removed the record directly, bypassing the very predicate both exit paths consult. It now drives that predicate.

⚠️ **What the fix leaves open, and it is not mechanical.** `held_summary` exists so that exit-after-vote
is decided ON NUMBERS -- "measure before loosening" -- and it now understates what the exit guard
locks: its third pocket counts anchored JURY seats, and a member held for having answered is not a
juror. Folding them into `jurors_locked_count` would change what an existing field means; counting
them separately needs a NEW proto field, on a query the mirror publishes. Either way it is a decision,
which is why it is recorded here rather than taken.

`hasOpenObligation` walks `HeldFee`, audit seats and disputed jobs. It reads **neither `Commit` nor
`assignedCommittee`**, so a work-committee member that has already answered is held by no clause.

**Measured** (`exit_after_commit_strands_test.go`): committee `[sc sb sa]`, all three committed; `sb`
unregisters **after** answering → accepted; committee `[sc sa]`; its commit is still stored; `SettlePay`
refuses for an incomplete committee; the job stays `"open"` with its fee. **No adversary is needed** —
eviction goes through the same guard. Control: the same job without the departure settles.

**Not a corollary of ⑫**: the draw's source is `k.Miner.Walk`, the live registry. Freezing the weight
pins the sort key; a deleted row is not walked at all.

**Choices.** Hold back a miner that has committed on an unsettled job — but bounded **by the job**
(settled, resolved, escrow returned), never by the miner's goodwill, or an identity whose key is lost
could never leave, which is the trap `miner_vitality.go` already documents.

### ⑯ The anti-collusion exclusion removes the ASSIGNED, not the AUTHORS

**✅ FIXED 2026-08-21** — authors are excluded at BOTH sites, unioned with the draw rather than swapped for it. The quorum interaction this record flagged was closed with it: `redoSeats` no longer counts a seat whose holder may not vote, so an exclusion shrinks both sides of the ratio instead of silently raising the bar.

`anchorRedoCommittee` excludes `orig = assignedCommittee(jobId, CommitteeSize)`, and `AdjudicateDispute`
applies the same set when counting votes. But `CreateCommit` — in the module's own words — *"requires
neither that the miner be assigned to the job nor that it was DRAWN onto the committee"*. The set that
produced the disputed commits is `{mid : Commit[jobId__mid] exists}`, which the adjudication re-reads,
and it is not `assignedCommittee`.

**Measured** (`redo_excludes_the_assigned_not_the_authors_test.go`): ten miners, drawn committee
`[co3 co2 co9]`, and `co0` — assigned to nothing — anchors a commit because nothing forbids it. The
anchored redo jury is `co7,co5,co6,co8,co4,co1,co0`. An author of the contested result sits on the jury
that re-judges it, and the vote filter does not exclude it either. Control: the three **drawn** members
are properly excluded, so the guard is not inert — it aims at the wrong set.

**Choices.** Read `Commit` keys instead of the draw — cheap to write, but it changes who may judge, so
it is governance-visible and consensus-breaking, and it interacts with the quorum: `redoSeats` counts
**anchored** seats while the vote filter uses a set computed later, so widening the exclusion shrinks the
numerator without touching the denominator.

### ⑰ The miner daemon de-duplicates on the RELAY DEPOSIT, not on the on-chain commit

**✅ FIXED — the commitment is journalled on disk BEFORE the deposit, and the loop resumes on that record.** A job whose response is deposited but whose commitment is still pending is no longer skipped: the next pass retries the ANCHORING ONLY, never the inference. The journal is written atomically, forgets an entry only once the chain carries it, and an unreadable file degrades to the previous behaviour rather than inventing a commitment. Services only, no epoch. ⛔ The bench covers the journal and its contract, not the loop that calls it — driving it would need a relay, a chain and an inference engine, and that limit is written in the bench rather than hidden by a name promising more. Originally arbitrated as: persist the sealed response AND its commitment locally BEFORE anchoring, then de-duplicate on that record.** De-duplicating on the on-chain commit is excluded by this entry's own constraint: a later pass re-runs inference, an LLM does not answer twice the same, and the deposit is sealed to the client's key, so recovery must CARRY THE ORIGINAL COMMITMENT FORWARD rather than recompute one. Keeping it in memory carries it forward but does not survive a restart, which is the case a daemon exists to survive. Making the irrepeatable step (inference to commitment) durable first leaves only repeatable steps — anchoring, then depositing — to be retried.

The work loop skips a job whose key already appears in the relay's `res` listing. But the response is
deposited BEFORE the commit is anchored, and the anchoring is attempted three times. If those three
fail — a misaligned identity, gas, a dropped RPC — the daemon prints "response posted but COMMIT NOT
anchored -> retry on the next pass", and the next pass skips the job, because the deposit is now in the
listing. The promise made 55 lines below the entry predicate cannot be kept by it. And `res` is
write-once, so even without the filter the re-post would be refused.

The GPU has run, the sealed answer is at the relay, and NO on-chain commit exists.

⚠️ TWO LEGS OF THE OBVIOUS CONSEQUENCE ARE FALSE, and they were corrected by an adversarial reading
rather than left standing. The client's escrow is NOT lost: `OpenJob` books an unconditional refund at
h+100 000 blocks, and with no commit from an ASSIGNED miner the expiry pass returns it — the money is
MISATTRIBUTED, not destroyed. And under a misaligned identity the daemon prints a six-line diagnostic at
every start, so that case is not silent; only transient failures are.

✅ **The MESSAGE half is closed; the design half is what remains.** The line no longer promises a pass
that cannot happen: it states that the job is abandoned, why the loop must skip it, and what the
operator can still do. The reason the skip is CORRECT is worth keeping in view for the choices below —
a later pass re-runs inference, an LLM does not return the same answer twice, and the response is
sealed to the client's key, so this process cannot read back the one it stored in order to commit to
THAT. Any recovery has to carry the original commitment forward, not recompute one.

**Choices.** De-duplicate on the on-chain commit rather than on the relay deposit · anchor the commit
BEFORE depositing the response · keep the computed commitment in memory and retry only the ANCHORING,
never the inference. *No recommendation here: the first changes what "already done" means for a daemon
that must stay idempotent across restarts, the second changes what a client can fetch and when, and the
third survives a failed transaction but not a restart. That is a design call.*

## Re-measured before this arbitration, and what the window changes

**Every pin was re-run, not re-read.** An arbitration made on a stale measurement is worse than none, so
the fourteen Go cases behind (9), (11), (12), (15), (16) were executed by name, the retention regimes of
(13) with them, and the twenty-four Python cases behind (10) and (14). **All green** -- which means they
still assert TODAY's behaviour, so all nine defects are still present, unchanged, on this tree.

**The costly part is already owed.** The kit builds at the epoch named on line 1 of
`docker/CONSENSUS_EPOCH`; the running network declares 5, and `join.sh` does not warn about the gap,
it `die`s -- a joiner cloning the mirror is refused outright. Redeploying therefore requires a fresh
genesis whatever else is decided. (This paragraph first named the number, and the number moved the
same day, in this same record, because of the fixes below. It now names the FILE that decides rather
than the digit that file happens to carry.) That is precisely the window in which the five state-machine entries cost nothing extra to
carry, and it is open now rather than at some later date of our choosing.

**So the nine are not nine decisions.** They separate by what each fix has to touch, and only one of
those groups is coupled to the reset:

| | entries | what the fix needs | coupled to the reset? |
|---|---|---|---|
| A | (11) (15) (16) | logic only -- no new state, no genesis field, no migration | yes, and they ride free |
| B | (12) (13) | a state-shape decision first (new per-job field + migration), or (13)'s stated alternative of accepting the loss and saying so on the surface that displays it | yes, but each needs an answer before it can be written |
| C | (10) (14) | services only -- capacity signing, relay key shape | no, independent of any genesis |
| D | (9) (17) | one message contract, one daemon design | (9) rides the reset; (17) is a design call with no recommendation |

Group A is the whole of what the reset can absorb without another question being answered first. Group C
can proceed at any time and is not blocked by anything here. Nothing in this table is a recommendation
about WHETHER to fix any of them -- it says only which ones the already-owed reset would make cheap, and
which ones need an answer before a line can be written.

## What this record does NOT claim

- **None of these is exploited today**, and several are unreachable in the running configuration: ⑪ and
  the epoch-9 fixes need parameter sets the live chain does not carry, ⑭ needs `enforce`, ⑯ needs a
  dispute. Saying otherwise would be the easy overclaim.
- **The recommendations are opinions**, not measurements. Each is one sentence and is marked as such.
- Three defects found in the same sweeps were **fixed rather than recorded here** — a compound slash that
  took ~96 % of an honest bond, a slash calibrated above 100 % that underflowed a `uint64`, and a dispute
  that could open with no deadline. They moved `CONSENSUS_EPOCH` 6 → 9 and are documented there.

## Consequences

- The nine are carried as a numbered index (⑨→⑰) that points here rather than repeating them.
- Whichever is arbitrated first, its pinning test **must flip**: each case asserts today's behaviour on
  purpose, and its red is the signal that the fix landed.
- Any of ⑪, ⑫, ⑬, ⑮, ⑯ changes the state machine and therefore moves `CONSENSUS_EPOCH` again, which
  already implies a fresh genesis — so the marginal deployment cost of taking several at once is nil.
