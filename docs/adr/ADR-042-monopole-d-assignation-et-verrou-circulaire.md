# ADR-042 — One miner holds every assignment, and the lock that keeps it there is circular

**Status:** Observed and measured on the live network on **2026-08-13**. No on-chain change enacted by
this record. It exists to name a state that no published indicator shows, and to rule out the
parameter changes that look like remedies and are not.

> ⚠️ **The network this record measures no longer exists.** It was destroyed and relaunched on a fresh
> genesis on **2026-08-16**: the thirteen registrations are gone. (Re-measured **2026-08-21**: one miner is
> registered, against the five an audit needs — read it rather than this line: `/dendra/jobs/v1/miner`.)
> The four mechanisms described below are unchanged in the code, so the composition they produce will
> recur as soon as miners register again — which is why this record is kept rather than retired.

> **The network has thirteen registered miners and has served exactly one.** Not because the others
> failed, and not because anything is broken: four independent mechanisms, each individually correct,
> compose into a state that none of them intends. Every one of them is documented, tested, and
> behaving as designed.

## Context — what was measured

All figures below were read from the live chain on 2026-08-13, anonymously, without any operator
secret. Job identifiers carry a millisecond timestamp, which gives a reliable chronological order.

| Measurement | Value | Source |
|---|---|---|
| Registered miners | 13 | `/dendra/jobs/v1/miner` |
| Miners that ever anchored a commit | **1** | `miner_last_commit_height` absent for the other 12 |
| Stake of that miner | **5 000 000** | vs **50 000** for each of the other twelve — a factor of **100** |
| Jobs on chain | 51 | `/dendra/jobs/v1/job` |
| Jobs whose drawn primary is that same miner | **51 / 51** | `/dendra/jobs/v1/assigned_committee/<jobId>` |
| Jobs whose eligible pool contains exactly **one** miner | **35** | idem |
| Jobs with a pool of 2 / of 3 | 7 / 9 | idem, capped by `CommitteeSize = 3` |
| Jobs settled optimistically | 30 | `state = open+paid+optimistic` |
| Retentions still held | **16**, totalling **272 406 udndr** | `/dendra/jobs/v1/held_summary` |
| Age of the oldest retention | **193 706 blocks** | idem — blocks, the unit the chain counts |
| Verdicts ever anchored | **0** | `jurors_locked_count = 12`, no verdict under any of them |

Two figures decide the whole record. **35 of 51 jobs have an eligible pool of exactly one miner** — so
on those, no draw takes place at all. And on the **9 jobs where the pool held three candidates, the
same miner was drawn primary 9 times out of 9**.

## The four mechanisms, and why each is right on its own

**① The pool is frozen at the job's birth.** `minerInFrozenPool` (`x/jobs/keeper/pool_freeze.go`)
admits a miner only when `reg <= freeze`: registered at or before the job's freeze height. This is
ADR-037's anti-evasion property, and it is load-bearing — without it, an attacker could register
identities *after* seeing a job and walk into its committee. The consequence here is arithmetic: the
twelve other miners registered between heights 82 392 and 152 314, so for every job opened before
that, **they are not eligible and can never become eligible**. Thirty-five jobs are addressed to a
machine that is powered off, and no parameter, no governance proposal and no stake change can
redirect them. That is not a defect to repair; it is a property working as specified.

**② Where a draw does happen, stake decides it.** `lessByStakeScore` (`committee.go`) orders
candidates by `sha256(seed|id) / stake` via an exact big-integer cross product. A miner with 100× the
stake carries a score 100× smaller, and sorts first. Deriving the probability from the formula: with
twelve competitors, the holder loses only when some competitor's hash falls below one hundredth of its
own, which happens about **6 %** of the time — so it takes primary on roughly **94 %** of draws. Nine
out of nine is what that predicts; it is not evidence of anything stronger, and this record does not
claim the draw is deterministic.

**③ The retention is total, and its release needs a verdict.** `hold_bps = 10000` (measured) means
`held = minerNet` and `immediate = 0` in `settleOptimistic`: **the branch that pays the operator at
settlement is never taken**. Payment happens later, at finality, and there are exactly two finalities
— `releaseHeld` when the job was *not* audit-sampled (`audit_sampling.go`: *"Not audited means the
optimistic payment is FINAL"*), or a verdict. This works: of 30 settled jobs, 14 were released. The
remaining **16 were sampled for audit**, and there the money stops.

**④ An audit without voters defers, forever, on purpose.** With no quorum of verdicts,
`resolveDisputedAudit` takes its default branch, which the code names precisely: *"otherwise -> DEFER.
This is not a verdict, it is an audit that did not take place. Nothing is closed and nothing moves
(fee held, stake intact, bond kept), and a new deadline is set. **The deferral is unbounded on
purpose: bounding it would make evasion free.**"* No node on this network runs `--judge`; `audit_min_quorum`
is 4. So all 16 audits defer, and will defer indefinitely.

## The lock is circular

Each mechanism holds the next, and the last holds the first:

1. 16 audits cannot conclude — no judges.
2. So 272 406 udndr stay in `HeldFee`.
3. So `hasOpenObligation` keeps returning true for that miner.
4. So `delete-miner` refuses with `code=18`, and the miner cannot leave.
5. So its 5 000 000 stake stays in every future draw.
6. So it keeps taking ~94 % of new work — on a machine that is switched off.
7. Which produces no commits, so no audit can be judged, **which is (1)**.

There is no step in this loop that is a bug. That is what makes it hard to see: every component
reports success, and the composition reports nothing at all.

## Decision

**We do not attempt to widen a frozen pool.** The 35 jobs whose pool holds one miner are structurally
stranded and are recorded as such. Any mechanism that retroactively admitted a miner into an already
frozen pool would delete ADR-037's anti-evasion property, which is worth more than 35 jobs.

**We do not lower `hold_bps` to unblock payments.** It is the obvious-looking lever and it is the
wrong one: the retention is what makes an optimistic settlement clawback-able. Setting it to 0 pays
every primary in full before any audit can contest the work — it would not release the 16 existing
retentions (already written to `HeldFee`), and it would remove the recourse for every job afterwards.
The measured problem is not that the retention exists; it is that **nothing can resolve an audit**.

**Judges resolve exactly one of the sixteen. The rest cannot be resolved at all on this chain.**

This section first read *"the unlock is judges, not parameters"*. That is wrong, and the correction is
the most important line in this record. `drawAuditCommittee` draws the jury **from the same frozen
pool** as the work assignment, minus the primary — the code says so where it explains why: an
identifier chosen after the seed is public could otherwise grind its way onto a jury, and the freeze is
what closes that. The consequence is arithmetic again, and unforgiving:

| Frozen pool | Jury available (pool − primary) | Floor `audit_min_quorum` | Outcome |
|---|---|---|---|
| 1 miner — **35 jobs** | **0** | 4 | can never be drawn |
| 2 miners — **7 jobs** | 1 | 4 | can never be drawn |
| ≥ 3 miners — 9 jobs | up to 12 (measured on the disputed job) | 4 | resolvable |

**Forty-two of fifty-one jobs can never form a jury, no matter how many judges join**, because a judge
that registered after the job opened is not in that job's pool and never will be. Reaching the quorum
is necessary for the ninth-and-newer jobs and irrelevant to the rest.

Worse, a job that was *selected* for audit cannot fall back to the release path. `deferAuditForSmallJury`
re-queues it with no bound and logs *"If this message persists, the network verifies NOTHING"*; and
because the selection is sticky (`alreadySelected` short-circuits the lottery on every retry), the
`else { releaseHeld }` branch is unreachable for it forever. A detail confirms the mechanism from the
outside: the `+disputed` marker is assigned to the in-memory job **before** the floor check, and the
small-jury branch `continue`s before `Job.Set`, so the state is never persisted — which is exactly why
the chain shows sixteen retentions and only **one** job marked disputed.

**And no governance message can settle a retention.** Of the five authority-gated messages in the
module — dispute, report-divergence, reward-training, settle-job, slash-miner — **none** touches
`HeldFee`, `releaseHeld` or `refundRetainedToClient`. There is no administrative release, by design:
the retention is money owed to the miner (`hasOpenObligation` clause (a) names it *"money owed to it"*),
and the block on `delete-miner` is protective, not punitive. The chain refuses to evict a miner it owes.

**Therefore the loop is not breakable by any transaction on this chain.** Not by judges (for 42 of 51
jobs), not by governance (no path), not by the miner itself (`UpdateMiner` discards `stake`, and no RPC
unbonds). The 272 406 udndr stay held, clause (a) stays true, the 5 000 000 stake stays in every draw.
The clean exit is the network reset already planned — which is a statement about what this state costs,
not an argument for hurrying it.

**One governance lever is real and is recorded as available, not as recommended.** `MsgSlashMiner` is
authority-gated and genuinely rewrites the stake (`miner.Stake -= amt`, then `Miner.Set`), at
`slash_leak_bps = 8000` per call. Three successive slashes would take 5 000 000 below the others'
50 000 and end the assignment monopoly **for new jobs only**. It is a real, irreversible confiscation
to the Treasury, it does nothing for the 35 frozen jobs, and it does not release a single retention.
It buys assignment diversity and nothing else, at a permanent cost.

## The asymmetry that makes it permanent: the jury checks vitality, the work draw does not

`drawAuditCommittee` calls `eligibleAsJurorInWindow` on every candidate inside its walk (`audit_committee.go`), which
refuses a miner that has neither committed nor registered within the freshness window. `assignedCommittee`
(`committee.go:134-141`) filters on **one** predicate — `minerInFrozenPool` — and on nothing else.
`eligibleAsJuror` is never called from the work draw.

**So a miner that has been powered off for weeks keeps winning work assignments, forever.** The
protocol has a liveness test, applies it to the role that only reviews, and omits it from the role that
actually has to answer. That is the whole reason this network hands most of its work to a machine that
stopped on 2026-08-11: nothing on chain notices that it stopped.

Nor can it be removed. `pruneInactiveMiners` exists exactly for inert identities and is
non-discretionary — but it routes through `hasOpenObligation`, whose clause (a) holds any miner with a
held fee on a job it is primary of. Those held fees can never release (no jury can be drawn for them).
The eviction path and the money path are locked to each other.

## What a relaunch has to change, and why each item is here

This section exists so the state described above cannot rebuild itself on a fresh chain. Every item is
a consequence of a measurement in this record, not a preference.

1. ✅ **DONE — a vitality predicate now gates the work draw**, not only the jury. `assignedCommitteeOrdered`
   calls `minerVitalAt` on every candidate, and it is evaluated **at the job's freeze height, never at
   the current height**: this draw is recomputed on every read, so a predicate that moved with the
   clock would silently reassign the primary of an already-settled job and `assigned_committee` would
   stop agreeing with what was paid. At the freeze height the question is the right one anyway — *was
   this identity producing when the job opened?* — and that is exactly the case that failed here. A
   miner that dies mid-job is a different problem, and it belongs to the audit.
   ⚠️ `minerVitalAt` is a thin wrapper over `eligibleAsJuror`, whose name predates its second caller.
   The predicate is role-neutral; the name is not. Renaming it touches 18 references across 8 files,
   six of them benches pinned to security behaviour, so it belongs to its own pass rather than to a
   consensus-breaking diff that has to be read for what it does to the protocol.
2. ✅ **DONE — `OpenJob` refuses while the eligible pool is below `audit_min_quorum + 1`.** A job whose
   pool is smaller than that is born unauditable and stays so for the life of the chain. The freeze is
   right; opening work before there is anyone to freeze is not.
   ⛔ **AND THE RELAUNCH PROVED IT WAS NOT THEORETICAL.** The first fresh chain reproduced the failure
   at block ~30: the launcher's own end-to-end check opened a job while zero miners were registered,
   freezing an empty pool — never assignable, never auditable, its escrow unreachable. Same class as
   the 42 stranded jobs, one relaunch later, with no attacker and no bug: only the ordinary order in
   which an operator brings a network up.
   Measured on the second fresh chain, at height 363: `job REFUSED: 0 eligible miner(s) at height 363,
   5 needed (audit_min_quorum=4, plus the miner under audit which the jury excludes)`. Zero jobs
   existed after 338 blocks where the previous launch had already created one condemned job.
   ⚠️ The `+ 1` is load-bearing: the jury excludes the miner under audit, so `quorum` jurors need
   `quorum + 1` eligible miners. Two neighbouring thresholds are not derived from one another — that
   mistake was published once already, on this very number.
   ⚠️ The message is explicit ON CHAIN. The public gateway deliberately does not relay it (`no raw
   error leaked to the client`), so a chat user still sees only a generic failure. That is a defensible
   choice for a public surface and a real gap for anyone debugging through it.
3. ✅ **DONE — the assignment carries a ceiling.** `capAssignmentWeights` saturates the draw weight at
   `assignmentStakeCapMultiple` (2) times `min_stake`. Staking beyond it buys nothing, so the
   incentive to hoard weight disappears rather than being merely bounded; the bond keeps its meaning,
   since a slash still takes the whole of it. It deliberately does **not** touch `lessByStakeScore`,
   which the audit draw shares — capping there would have changed the jury's arithmetic as a side
   effect of fixing the assignment.
   ⛔ **THE FIRST IMPLEMENTATION SCALED THE CEILING BY THE POOL'S OWN FLOOR, AND THAT WAS AN ATTACK.**
   Deriving it from the smallest stake present reads as elegant and hands the adversary the dial:
   stake dust and every honest weight collapses to twice that dust. The repository's own bench caught
   it — `c2Setup` plants a sybil at stake 1 precisely so it is *"deterministically excluded from the
   top 3"*, and the pool-relative cap brought that gap from twelve orders of magnitude down to two.
   `min_stake` moves only by governance. *A ceiling an adversary can lower is not a ceiling.*
   ⚠️ `assignmentStakeCapMultiple` is a Go constant, like `CommitteeSize`: the same debt, admitted in
   the same place. A constant that bounds the failure is worth more today than a parameter that does
   not exist yet, but it is a constant.
4. **Make `CommitteeSize` governable.** It is a Go constant of 3, commented "to be made governable
   through params later", and it caps the visible pool even when thirteen miners are eligible.
5. **Let a miner reduce its own stake, or leave.** `UpdateMiner` accepts `stake` in the proto and
   discards it (`msg_server_miner.go:163`); no RPC unbonds. An operator who wants out of the draw has
   no move that does not require governance.
6. **Bound the client's wait, end to end.** The gateway's three timeouts are each bounded (45 s, 240 s,
   180 s) and *compose* to roughly nine minutes. Measured on the public path: no response after 320 s.
   The honest failure message exists and arrives far too late to be honest in practice.

## Consequences

- Opening the relay's work queue was necessary and is not sufficient. A joined miner can now read the
  queue and will correctly find that nothing in it is addressed to it.
- The reward complaint from outside operators is accurate and has a cause distinct from the relay
  wall: they were never *assigned* anything. No amount of availability on their side changes that
  while ① and ② hold.
- `avail_slash_bps`, `avail_fail_k` and `avail_fail_window` are all **0** (measured): a primary that
  never delivers is not penalised. `silence_slash_bps = 2000` is armed in params, but its only carrier
  `clawbackPayment` has no production caller — so the absent primary is ignored, not slashed.
- `CommitteeSize = 3` is a Go constant, commented *"to be made governable through params later"*. It
  caps the pool at three even when thirteen miners are eligible, so widening the draw is a
  binary-upgrade matter, not a governance one.

## What this record does not establish

- Whether reducing the incumbent's stake would reassign the primary on jobs **already open** with a
  pool of 3, or only on jobs opened afterwards. The draw is recomputed on read, which suggests the
  former, but it was not measured before and after an actual stake change.
- The **exact** size of the frozen pool on the nine most recent jobs. `assigned_committee` caps its
  answer at `CommitteeSize = 3`, so a reported 3 means "at least 3", not "exactly 3". The one job that
  reached an anchored audit committee carries **12** jurors, implying a pool of 13 there — but that was
  read on a single job and does not settle the other eight.
- Which of the sixteen retentions sit on jobs with a pool of 1 versus 2. The oldest
  (`job1785643115494`, held for 193 706 blocks) is the very first job on the chain and has a pool of 1,
  so at least that one is in the unresolvable class; the split across the remaining fifteen was not
  enumerated one by one.
