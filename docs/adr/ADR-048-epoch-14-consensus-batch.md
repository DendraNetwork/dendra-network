# ADR-048 — Consensus epoch 14: what the relaunch changes in the state machine

**Status:** Accepted by the project owner, 2026-10-04.
**Implementation:** Items 1 to 7 implemented in the chain, item 8 in the five Python services; the
launcher, the compose files and `join.sh` still carry the old name and move with the network file.
Each item names the bench that pins it. All 31 mutations of the shipped code listed in the mutation
run turned at least one of these benches red.

## Context

The relaunch of [ADR-046](ADR-046-single-validator-relaunch.md) starts from a fresh genesis, so changes
to the state machine cost nothing that they would cost on a running chain. A review of the epoch-13 code
against the relaunch found behaviours that would leave the new network unable to pay anyone, or paying
what it announces it does not.

## Decision

1. **A job can open below the jury floor when retentions can be unwound.** `MsgOpenJob` refused any job
   while fewer than `effectiveSlashFloor + 1` miners were eligible, because a retention no jury could
   judge stayed held for ever. With `audit_unwind_blocks > 0` that retention goes back to the client, so
   the refusal no longer protects anything; the floor still applies when unwinding is dormant.
   *Pinned by* `epoch14_test.go::TestEpoch14_OneMinerOpensAJobWhenUnwindAndFullHoldAreArmed` and its three
   refusals (unwind dormant, partial hold, nobody to serve). The full hold is a condition: at a partial hold
   the part paid at settlement would reach the miner unverified.
2. **The no-seed deferral can unwind too.** A due audit deferred because no decentralised seed exists was
   rescheduled for ever, while the two other deferrals already called the unwind. All three now share it.
   *Pinned by* `TestEpoch14_NoSeedDeferralUnwindsARetentionOlderThanTheBound` and its dormant twin.
3. **The subsidy right is born when the miner is paid.** `Demand` was credited at settlement and could be
   claimed before the job was final; a refund then cancelled the demand but never the subsidy already
   taken. It is now credited when the retention is released, and a refund has nothing left to undo.
   *Pinned by* `TestEpoch14_FinalityPaysEachShareToItsOwnerAndCreditsTheRight`,
   `TestEpoch14_ASlashAfterFinalityReversesTheCreditedRight`, `TestEpoch14_SelfDealingEarnsNoRight`.
4. **The validators' and the team's shares of every job are paid.** They were counters that nothing paid
   out. At finality — on the same branch that burns — the validators' share goes to `fee_collector` and is
   distributed by `x/distribution`, the team's share goes to a governed `team_address`. On a refund both
   go back to the client. The anti-wash inequality is re-checked with the new flow.
   *Pinned by* `TestEpoch14_ARefundBeforeFinalityReturnsThisJobsCutAndNothingElse`,
   `TestEpoch14_AtZeroHoldFinalityStillPaysTheCut`, `types/params_invariant_test.go::TestInvariant8_GuardDiscriminates`
   (the bound now counts the validators' share as recoverable by a staking washer: `work_gate_bps` < 16 667).
5. **Only present miners are drawn.** When availability windows are armed (`avail_epoch_blocks > 0`), a
   miner that did not prove presence in the previous window is neither drawn as a primary nor seated as a
   juror. A registered miner whose machine is off can no longer freeze a job by being drawn.
   Present means: proved the current or the previous window, or registered in one of them.
   *Pinned by* `TestEpoch14_PresenceRuleEdges` and one case per draw site (primary, jury, redo, admission).
6. **Policy constants become bounded parameters**: the job expiry, the work committee size, the assignment
   stake cap and the audit committee draw size, each with a lower and an upper bound in `Validate` and its
   direction declared to the genesis guard.
   *Pinned by* `types/params_epoch14_test.go` (both sides of every bound) and one keeper case per parameter.
7. **Dead weight is removed**: the `x/auth/vesting` module, unused by the network and the reason the
   scanner reports GO-2024-2584 (its accepted entry in `chain/.govulncheck-accepted` goes in the same
   change, since the filter refuses an entry whose advisory is no longer reported), and the upgrade plans
   written for chains that no longer exist.
   The Cosmos simulation now generates plain accounts only (`app/sim_accounts.go`); the four simulations pass.
8. **The chain id is read, never written.** Every service, script and compose file takes it from the
   network's configuration and refuses to start without it.
   *Pinned by* `services/tests/test_chain_id.py`, which drives all five states (declared, served,
   agreeing, disagreeing, neither).

## Consequences

- `docker/CONSENSUS_EPOCH` moves to 14: a binary from epoch 13 forks at the first block that exercises
  any of the changes above.
- Items 1 and 5 together make the network serve from its first miner, which is what the reward programme
  of [ADR-047](ADR-047-final-testnet-season-reward-programme.md) needs.
- Item 4 changes where 10.5 % of every job ends up. Every public page that describes the split of a
  payment is checked against the new flow in the same change.

## Found by the review of this batch

Three independent reviews (money, draws and liveness, parameters and wiring) confirmed that coins are
conserved and found six defects, all fixed in this batch with a bench each
(`chain/x/jobs/keeper/epoch14_review_test.go`): a cheat proven after finality refunded the client
nothing on the permissionless path; a job the audit lottery selected could be released to its own primary
by a self-dispute below the jury floor; a resolved job could be disputed and its bond stranded; governance
resolution left the retention frozen; `Payout` let a lone commit be paid in optimistic mode; a
foreign-prefix `team_address` would have queued every finality for ever.

The adversarial review of the whole relaunch (2026-10-04) found three more, fixed with a bench each in the
same file, each red on the code before it: a job whose lottery had **not run yet** (deferred for want of a
seed) was not counted as selected, so a self-dispute during a seed outage still released it to its
primary — `resolveDisputedAudit` now treats a pending lottery like a selection; a `team_address` that is a
**module account of this chain** passed the prefix check and would have queued every finality as well;
and the no-seed deferral event counted the stale entries it dropped as deferred jobs.

## Limits that stay, stated

- **Below the jury floor, nobody can be slashed.** Items 1 and 5 open a job from a single present miner;
  a job the lottery draws there is refunded to its client after `audit_unwind_blocks`, because no jury of
  `audit_min_quorum` can form. Cheating then costs the miner only the payment of the drawn share, at the
  rate `audit_sample_bps`, and no slash: the Nash guard of `MsgOpenJob` still assumes a slash it cannot
  reach in that regime. This is the announced price of serving from the first miner; the public pages
  state the deterrent as conditional on the jury floor, and the reward programme samples the answers to
  its own requests with a model (ADR-047, owner's decision of 2026-10-04) rather than relying on it.

- **At `hold_bps = 0`**, a job whose audit never concludes keeps its cut and its subsidy right retained
  for good: the unwind needs a retention to date. The launcher refuses anything below a full hold.
- **A missing seed for `audit_unwind_blocks`** refunds every due job, including those the lottery would
  not have selected: the miners of that window are not paid. That is the price of item 2. With one
  validator it means the operator's own seed outage, so the launcher must keep checking that the
  validator's VRF key is anchored and loaded.
- **Changing `avail_epoch_blocks` by governance** re-indexes the presence windows: arming or lowering it
  makes every miner absent for about one window, during which no job opens.
- **Raising `work_committee_size`** strands redundant-mode jobs already in progress. Optimistic mode is
  unaffected.
- **The anti-wash bound does not hold for the holder of `team_address`**, who recovers the team share as
  well. That is an insider drain no parameter can price; it is named in the code so that the guard is
  never read as a protection against the operator.
