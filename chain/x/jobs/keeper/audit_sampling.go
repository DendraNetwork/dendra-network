package keeper

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"strconv"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// auditDomain separates the audit draw from the committee seed, so the two never collide.
const auditDomain = "dendra/audit/v1|"

// auditDeferStride is how many blocks an audit draw is pushed back when no decentralized seed exists.
// It balances two costs. A short stride rewrites state every block for nothing: a missing seed
// typically lasts dozens of blocks at genesis, and possibly forever if no validator anchors a VRF key.
// A long stride delays the audit once the seed is back. 20 blocks is about 20 s at the target block
// time, small next to `audit_resolve_timeout` (120), with 20x fewer writes while the seed is missing.
const auditDeferStride = 20

// auditMaxPerBlock caps how many due audit entries a single EndBlock takes on each of the three
// queues. Walking only the exact height with no cap works only while a batch cannot exceed one
// block's worth of settlements, and `auditDeferStride` breaks that: deferring by
// a fixed stride makes every batch of the same residue class land on the same height and merge, so a
// seed that stays centralized (or a jury that stays short) grows the per-block batch without bound,
// the defect already closed on the reveal pass (`revealMaxPerBlock`, job_expiry.go). A cap alone would
// strand entries, so the range is also widened to every due height (`K1 <= h`): what does not fit
// stays in the store and the next block takes it, oldest first. The two changes only work together.
const auditMaxPerBlock = 200

// runOptimisticAudit (ADR-025 M3) draws, at height `h`, an audit for every optimistically settled job
// scheduled at that height, with the effective probability (see effectiveAuditBps: base + adaptive +
// probation). On a hit it opens a protocol dispute (`+disputed`, disputer = governance account, bond
// 0) that a fresh committee will adjudicate (M4), and schedules a self-resolution timeout for
// liveness. The `seed` (AppHash / VRF of the current block) comes after the commit, so the miner
// cannot predict the draw (anti-grinding).
// Dormant: no-op when verification_mode != 1; PendingAudit is only fed by settleOptimistic (mode 1).
func (k Keeper) runOptimisticAudit(ctx context.Context, h int64, seed string) error {
	p, err := k.Params.Get(ctx)
	if err != nil || p.VerificationMode != 1 {
		return nil // redundant mode (or missing params): dormant, never blocks EndBlock
	}
	var due []collections.Pair[int64, string]
	if err := k.PendingAudit.Walk(ctx,
		new(collections.Range[collections.Pair[int64, string]]).
			EndExclusive(collections.PairPrefix[int64, string](h+1)),
		func(key collections.Pair[int64, string]) (bool, error) {
			due = append(due, key)
			return len(due) >= auditMaxPerBlock, nil
		}); err != nil {
		return err
	}
	if len(due) == 0 {
		return nil
	}
	// Proposer anti-grinding: a draw happens only on a decentralized seed.
	//
	// `ProcessProposal` accepts a proposal that carries no vote-extension injection. Without injection
	// there is no decentralized seed at that height, and `committeeBaseSeed` falls back to
	// `AppHash(H-1):h`, which the proposer of block h knows before proposing. It could compute the
	// favourable draw off-chain and then choose whether to inject: two seeds to pick from at every block
	// it proposes, and more if it drops votes beyond the required 2/3. That is the predictability
	// ADR-032 closes on the sybil side, reopened on the consensus side.
	//
	// Rejecting the block would hand the attacker a chain-halt switch (propose without injection in a
	// loop, get rejected in a loop). The benefit is removed instead: with no decentralized seed no draw
	// happens and the due jobs are deferred. Omitting the injection therefore gains nothing, and the
	// backlog drains as soon as an honest proposer injects.
	if _, decentralized := k.committeeBaseSeedSourced(ctx, seed); !decentralized && p.CommitteeSeedSource == 1 {
		sdkCtx := sdk.UnwrapSDKContext(ctx)
		// Defer by a wide stride rather than one block.
		//
		// Deferring is right, since without a decentralized seed drawing would let the proposer choose, but
		// deferring by one block replays the deferral at every block: two KV writes per job per block, for a
		// duration nothing bounds. A missing seed is the normal state between block 1 and the moment
		// validators anchor their VRF keys, and it can last indefinitely if none of them does, so the cost
		// would grow with traffic in the phase where the network is most fragile.
		//
		// A stride of `auditDeferStride` blocks divides the write amplification by as much and changes no
		// property: the held fee stays held (fail closed, no funds move) and the backlog drains as soon as a
		// seed exists. The deferral never gives up after N attempts: releasing because the audit could not run
		// would let an abstaining validator push jobs through unaudited.
		//
		// ADR-048 item 2: this deferral must also end. The jury-floor and below-quorum deferrals both call
		// ADR-044's unwind; if this one rescheduled forever, a network that lost its seed for good would keep
		// every retention held for good, the outcome ADR-044 exists to remove. The unwind is not a release:
		// the client is refunded and the miner is paid nothing, so an abstaining validator gains no way to push
		// a job through unaudited; it can only cost the miner the payment, after `audit_unwind_blocks`, as a
		// missing jury does. Dormant at 0, where the unwind returns before reading anything.
		unwound, dropped := 0, 0
		for _, key := range due {
			// A due entry whose job is no longer an open optimistic payment has nothing left to audit, and
			// it is dropped, as the drawing loop below drops it. Otherwise the deferral kept a stale entry
			// alive forever, and the unwind could refund a resolved job whose retention is owed to its
			// miner (vindicated, release pending a missing primary).
			if job, jErr := k.Job.Get(ctx, key.K2()); jErr != nil || !jobIsPaid(job.State) || jobIsResolved(job.State) {
				if err := k.PendingAudit.Remove(ctx, key); err != nil {
					return err
				}
				dropped++
				continue
			}
			if k.unwindStrandedRetention(ctx, h, key.K2(), p) {
				// Settled, not postponed: the entry is removed, never moved forward to collect again a job
				// that no longer has a debt.
				if err := k.PendingAudit.Remove(ctx, key); err != nil {
					return err
				}
				unwound++
				continue
			}
			// The key is removed by its own height, which is not necessarily `h`: the walk covers every
			// due height, so an entry left behind by a capped block carries an older one.
			if err := k.PendingAudit.Remove(ctx, key); err != nil {
				return err
			}
			if err := k.PendingAudit.Set(ctx, collections.Join(h+auditDeferStride, key.K2())); err != nil {
				return err
			}
		}
		sdkCtx.Logger().Error("SECURITY: no decentralized VRF seed at this height -> NO audit draw. Jobs deferred; held fees stay held until audit_unwind_blocks, then refunded to the client. Usual cause: no validator has anchored a VRF key yet, or vote-extensions are not enabled. If this message persists, the network verifies nothing.",
			"height", h, "jobs_deferred", len(due)-unwound-dropped, "jobs_unwound", unwound, "jobs_dropped", dropped, "next_attempt", h+auditDeferStride)
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"audit_draw_deferred",
			sdk.NewAttribute("height", strconv.FormatInt(h, 10)),
			// A stale entry dropped above was never deferred; counting it made the event overstate
			// the backlog.
			sdk.NewAttribute("jobs", strconv.Itoa(len(due)-unwound-dropped)),
			sdk.NewAttribute("unwound", strconv.Itoa(unwound)),
			sdk.NewAttribute("dropped", strconv.Itoa(dropped)),
			sdk.NewAttribute("reason", "seed is not decentralized"),
		))
		return nil
	}

	disputer, _ := k.addressCodec.BytesToString(k.GetAuthority()) // governance module account (zero bond)
	for _, key := range due {
		jobId := key.K2()
		// The condition is `!jobIsResolved`, not `!jobIsDisputed`.
		//
		// Under `!jobIsDisputed` an already disputed job was skipped, yet its deadline was removed a few
		// lines below for every due job, so the audit never happened. `DisputeVerdict` only requires
		// "paid and not already disputed": it does not forbid the primary from disputing its own job, and
		// the audit is scheduled one block after settlement, an observable one-block window.
		//
		// A miner could therefore buy its way out of an audit for the price of `dispute_bond`. The bond
		// is a fixed cost and the slash is proportional to stake, so past `dispute_bond / slash_leak_bps`
		// of stake the evasion pays, and pays more the bigger the miner, the opposite of what an
		// anti-cheat guard must produce. The job then fell into the "unfounded dispute" branch, which
		// releases the held fee, finalizing an unverified payment.
		//
		// A job disputed by a human is now audited anyway. Its committee is anchored, so
		// `resolveDisputedAudit` takes the real audit path instead of the unfounded branch, and
		// disputing protects from nothing.
		if job, jErr := k.Job.Get(ctx, jobId); jErr == nil && jobIsPaid(job.State) && !jobIsResolved(job.State) {
			// ADR-034 C3: an audit already won is never drawn again. A job whose draw was won
			// (`auditDraw < bps`) and then deferred for lack of a jury carries the `AuditSelected` marker.
			// Without it the retry replayed the lottery against the seed of a new block: at
			// audit_sample_bps=5000, an already selected audit was cancelled one time out of two, then
			// `releaseHeld` finalized the payment with neither verification nor marker, giving the
			// cheater its escape back. A selected job stays selected: the lottery is skipped and only the
			// committee draw is retried.
			alreadySelected, aErr := k.AuditSelected.Has(ctx, jobId)
			if aErr != nil {
				return aErr
			}
			if alreadySelected || auditDraw(seed, jobId) < k.effectiveAuditBps(ctx, p, job) {
				// ADR-034 partition: count the opening once per job, on the first selection (a retry
				// arrives with alreadySelected=true), and never when a human dispute already counted it
				// (the audit adds to the dispute and does not replace it, for the same reason Disputer
				// is not overwritten below).
				if !alreadySelected && !jobIsDisputed(job.State) {
					k.bumpAuditsOpened(ctx)
				}
				// Do not overwrite an open human dispute. The job may already be `+disputed` by someone
				// who escrowed a real bond. Rewriting `Disputer` and resetting `DisputeBond` to 0 would
				// leave those coins in the module account with no owner, funds orphaned by the guard
				// meant to protect them. The audit adds to the human dispute and does not replace it.
				if !jobIsDisputed(job.State) {
					job.Disputer = disputer
					job.DisputeBond = 0
					job.DisputeHeight = h
					job.State = job.State + "+disputed"
				}
				// ADR-032: anchor the audit committee at draw time. This turns "any registered miner may
				// vote" into "only the summoned vote".
				//
				// Splitting into identities lowers the chance of taking slot 0. At constant total stake, that
				// probability falls from 51.4 % with one identity to 38.6 % with sixteen:
				// dividing the stake divides the score (measured on the comparator lessByStakeScore uses).
				// That does not hold for seats: the draw seats each identity at most once and takes every
				// eligible miner when there are no more of them than seats (drawMembersWithDomain), so a
				// split can hold more seats.
				//
				// Unpredictability alone does not keep a miner out of the committee. The seed comes after the
				// primary's commit, which stops an already registered miner from placing itself, but not a
				// miner created after the seed is public, which can try `miner_id` values offline until
				// sha256(seed|id) is small. Hashing is free and stake is not: an attacker holding 1 stake
				// against an honest miner holding 1,000,000 takes slot 0 in 38 % of draws at 2^20 tries,
				// 94 % at 2^24, 99.9 % at 2^30. Stake weighting protects nothing against an identifier
				// chosen after the seed.
				//
				// The pool freeze (ADR-037) closes this: the identifier must exist before the seed does, so
				// drawAuditCommittee only admits miners whose registration height is at or before the job's
				// frozen pool height (JobPoolFreezeHeight, set once at OpenJob and shared by all three
				// draws).
				members, mErr := k.drawAuditCommittee(ctx, seed, jobId, job.MinerId, disputer)
				if mErr != nil {
					return mErr
				}
				// Jury too small: defer, with no sanction (attack and detail in audit_jury_floor.go).
				// Opening a dispute whose bar the floor makes unreachable condemns the primary before any
				// vote: at the timeout, `runAuditResolveTimeout` would claw back its payment without a
				// single juror having ruled.
				if floor := effectiveSlashFloor(p); len(members) < floor {
					// C3: mark before deferring, so the retry does not replay the lottery.
					if err := k.AuditSelected.Set(ctx, jobId); err != nil {
						return err
					}
					if err := k.deferAuditForSmallJury(ctx, h, key.K1(), jobId, len(members), floor, p); err != nil {
						return err
					}
					continue
				}
				// Committee large enough: the audit opens. The selection marker has no purpose left (the
				// job is now tracked by AuditCommittee + PendingAuditResolve), so it is removed to keep
				// the KeySet from leaking entries.
				if err := k.AuditSelected.Remove(ctx, jobId); err != nil {
					return err
				}
				// anchorCommittee writes the seats and the anchoring height together: a juror lock is
				// only measurable if every seat carries the date it was taken.
				if err := k.anchorCommittee(ctx, jobId, strings.Join(members, ",")); err != nil {
					return err
				}
				if err := k.Job.Set(ctx, jobId, job); err != nil {
					return err
				}
				// Liveness: schedule self-resolution in case no fresh committee adjudicates in time.
				if p.AuditResolveTimeout > 0 {
					if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+int64(p.AuditResolveTimeout), jobId)); err != nil {
						return err
					}
				}
				// The members are carried in the event: anyone can replay the draw, and an off-chain
				// judge learns it is summoned instead of guessing.
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
					sdk.NewEvent("audit_requested",
						sdk.NewAttribute("job_id", jobId),
						sdk.NewAttribute("committee", strings.Join(members, ",")),
					),
				)
			} else {
				// Not audited means the optimistic payment is final: release the held fee to the primary.
				k.releaseHeld(ctx, &job)
			}
		}
		if err := k.PendingAudit.Remove(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// effectiveAuditBps (ADR-025 M6) returns the effective audit rate of an optimistic job. Everything
// below is dormant by default:
//   - probation (audit_probation_jobs>0): 100 % while the primary has served <= N jobs (anti-Sybil
//     and cold start).
//   - adaptive (audit_adaptive>0): otherwise base x (1 + fee/ref) capped at 10000, so a large job is
//     audited more often.
//   - otherwise audit_sample_bps unchanged (M3 behaviour).
func (k Keeper) effectiveAuditBps(ctx context.Context, p types.Params, job types.Job) uint64 {
	if p.AuditProbationJobs > 0 && job.MinerId != "" {
		if cnt, err := k.MinerOptimisticCount.Get(ctx, job.MinerId); err == nil && cnt <= p.AuditProbationJobs {
			return 10000
		}
	}
	eff := p.AuditSampleBps
	if p.AuditAdaptive > 0 {
		eff += p.AuditSampleBps * job.Fee / p.AuditAdaptive // fee<=1e13, bps<=1e4: the product is under 2^57, no overflow
		if eff > 10000 {
			eff = 10000
		}
	}
	return eff
}

// runAuditResolveTimeout (ADR-025 liveness + ADR-028 anti-evasion) handles, at `h`, the optimistic
// audits that reached their deadline without being adjudicated. The optimistic primary has been paid,
// so it is the suspect. The decision rests on the fresh committee's verdicts
// (`<jobId>__verdict__<minerId>`, "0" = invalid, stake-weighted tally) against a symmetric relative
// bar (⌈2/3⌉ of the anchored seats, with the governed quorum as a lower bound):
//   - invalidVotes >= bar and a stake majority for "invalid": cheating, so hard slash + clawback. A
//     silent primary arrives here without any forgeable on-chain marker: the committee itself posts
//     "0" when it cannot obtain a usable reveal, a signal the primary cannot fake.
//   - validVotes >= bar and a stake majority for "valid": vindicated, at the same bar as the slash.
//     About 27 % of the stake no longer buys the vindication of a cheat: only the direction of the
//     majority differs between the two outcomes, never the bar.
//   - otherwise: defer. This is an audit that did not take place, not a verdict. Nothing is closed
//     and nothing moves (fee held, stake intact, bond kept), and a new deadline is set. The deferral
//     is unbounded on purpose: bounding it would make evasion free.
//     There is no `<jobId>__reveal__` branch: the decision rests on verdicts alone, and the reveal
//     stays an off-chain exchange between the primary and the committee.
//
// A non-optimistic (human) dispute left unhonoured keeps innocence by default: the disputer is the
// accuser. Dormant: nothing is scheduled when audit_resolve_timeout == 0.
func (k Keeper) runAuditResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil // missing params: never blocks EndBlock
	}
	var due []collections.Pair[int64, string]
	if err := k.PendingAuditResolve.Walk(ctx,
		new(collections.Range[collections.Pair[int64, string]]).
			EndExclusive(collections.PairPrefix[int64, string](h+1)),
		func(key collections.Pair[int64, string]) (bool, error) {
			due = append(due, key)
			return len(due) >= auditMaxPerBlock, nil
		}); err != nil {
		return err
	}
	for _, key := range due {
		if err := k.resolveDisputedAudit(ctx, h, key.K2(), p, true); err != nil { // allowAppeal=true: may defer to an appeal
			return err
		}
		if err := k.PendingAuditResolve.Remove(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// runAppealResolveTimeout handles the second, appeal deadline. A primary deferred at the first timeout
// (appeal_window>0) is judged again here unless it was resolved in the meantime, typically by
// AdjudicateDispute during the window. The tally runs again: a late reveal may have produced a valid
// quorum, which vindicates the primary and releases the held fee. Otherwise `allowAppeal=false` falls
// into the default branch and defers again: an audit without a verdict is not closed, it waits, in the
// same deferral loop the audit timeout uses. Dormant: PendingAppealResolve is only fed when
// appeal_window > 0.
func (k Keeper) runAppealResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil
	}
	var due []collections.Pair[int64, string]
	if err := k.PendingAppealResolve.Walk(ctx,
		new(collections.Range[collections.Pair[int64, string]]).
			EndExclusive(collections.PairPrefix[int64, string](h+1)),
		func(key collections.Pair[int64, string]) (bool, error) {
			due = append(due, key)
			return len(due) >= auditMaxPerBlock, nil
		}); err != nil {
		return err
	}
	for _, key := range due {
		if err := k.resolveDisputedAudit(ctx, h, key.K2(), p, false); err != nil { // allowAppeal=false: last instance
			return err
		}
		if err := k.PendingAppealResolve.Remove(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// lotteryPending reports whether the job still has an entry in `PendingAudit`, that is, its audit
// lottery has not run (it was scheduled, or pushed forward by a missing seed). Ordered walk of a
// KeySet: deterministic.
func (k Keeper) lotteryPending(ctx context.Context, jobId string) (bool, error) {
	found := false
	err := k.PendingAudit.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		if key.K2() == jobId {
			found = true
			return true, nil
		}
		return false, nil
	})
	return found, err
}

// resolveDisputedAudit handles one optimistic audit job at its deadline (audit timeout or appeal
// timeout), on the fresh committee's verdicts against the symmetric relative bar. Both timeouts share
// this function so they cannot diverge. Below quorum it defers: new deadline, job unchanged, no funds
// move; when `allowAppeal` and `appeal_window>0`, the deferral first goes through the appeal deadline,
// to leave room for a late reveal. The job is mutated and persisted only on a real resolution (slash,
// vindication, or one of the human-dispute branches).
func (k Keeper) resolveDisputedAudit(ctx context.Context, h int64, jobId string, p types.Params, allowAppeal bool) error {
	job, jErr := k.Job.Get(ctx, jobId)
	if jErr != nil || !jobIsDisputed(job.State) || jobIsResolved(job.State) {
		return nil // already resolved (for example by AdjudicateDispute during the window) or absent: nothing to do
	}
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		// Two silences that must not be confused.
		//
		// This path was designed for sampled jobs: an audit committee was drawn and anchored, so the
		// absence of verdicts is negative evidence ("the payment could not be verified"), and clawing the
		// price back is the conclusion ADR-028 wants.
		//
		// Since human disputes carry a deadline, jobs that were never sampled also arrive here. For them no
		// committee was ever summoned: `auditVerdictTally` reads `AuditCommittee[jobId]`, which only
		// `runOptimisticAudit` writes, so the tally always returns zero voters and the `default` branch
		// clawed back the fee of an honest, already paid miner. The attacker paid only gas, since
		// `dispute_bond` was 0, and the primary could not defend itself because no verdict could count.
		//
		// With nobody summoned, the dispute is not honoured: the held fee is released to the miner and the
		// bond goes to the Treasury, the anti-grief role of the bond. The only difference with a drawn
		// committee that stayed silent is that there the failure belongs to the network, so the bond is
		// returned.
		//
		// The condition needs both terms. "No anchored committee" alone is not enough: a sampled job whose
		// draw returned nobody (pool too small) has no anchor either, and its payment really is unverified,
		// so exempting it from the clawback would disarm ADR-028. Only "opened by a human and never
		// summoned" describes an unfounded dispute.
		//
		// Two committees may have been summoned, under two keys: `<jobId>` (sampling) and `<jobId>__redo`
		// (human dispute). Reading only the first turns a human dispute whose redo committee was anchored
		// but silent into "no committee", which falls through to "unfounded" and confiscates the bond of
		// an honest disputer because the drawn jurors did not answer.
		_, auditAnchored := k.auditCommitteeAllowed(ctx, jobId)
		allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, jobId)
		// ADR-048: a job the lottery selected was summoned, even without an anchored jury.
		// Below the jury floor, `deferAuditForSmallJury` marks the job `AuditSelected` and anchors no
		// audit committee. Branches (i) and (ii) below read "no audit committee" as "nobody ever audited
		// this job" and release the retention to the primary. A primary that saw itself selected could
		// then dispute its own job and be paid, unverified, at the dispute deadline, which always fires
		// before the unwind since Validate keeps `audit_unwind_blocks >= audit_resolve_timeout`.
		// Measured: 80000 released to the operator with no redo jury, 81000 with a one-seat redo jury that
		// can never conclude, while the control without a dispute kept the 80000 held for the unwind.
		// Items 1 and 5 make "below the floor" the normal state of a small network, so this was the
		// ordinary path.
		// A selected job therefore skips both release branches and goes to the tally below, where no
		// verdict means the default branch: deferred, then unwound to the client past the bound, the
		// outcome ADR-048 item 1 promises below the floor. A read error counts as selected: an unknown
		// selection is never a licence to release.
		//
		// A job whose lottery has not run yet was not "never summoned" either. A missing seed defers the
		// draw itself (the `PendingAudit` entry is pushed forward, unselected and uncleared), so during a
		// seed outage a primary could dispute its own job and be released at the dispute deadline before
		// the lottery decided. The walk is bounded by the pending set and runs only when a dispute reaches
		// its deadline.
		selected, selErr := k.AuditSelected.Has(ctx, jobId)
		pending, pendErr := k.lotteryPending(ctx, jobId)
		selected = selected || selErr != nil || pending || pendErr != nil
		if !auditAnchored && redoAnchored && !selected {
			// (i) Committee summoned but silent (or below quorum). A redo committee exists and the
			// adjudication did not conclude by the deadline: a network failure, not the disputer's
			// fault. The held fee is released to the primary (no proof of cheating) and the bond is
			// returned.
			//
			// The `responders / seats` pair is emitted before resolving. It is the measurement point
			// for the quorum parameter: it cannot be rolled back, and it captures the disputes whose
			// permissionless adjudication failed for lack of quorum.
			seats := len(allowedRedo)
			responders := k.countRedoResponders(ctx, jobId, allowedRedo)
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"redo_participation",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("seats", strconv.Itoa(seats)),
				sdk.NewAttribute("responders", strconv.Itoa(responders)),
				sdk.NewAttribute("resolved_by", "timeout"), // the "below quorum" population; see the "adjudication" counterpart in msg_server_adjudicate
			))
			k.releaseHeld(ctx, &job)
			if err := k.refundDisputeBond(ctx, &job); err != nil {
				return err
			}
			job.State = job.State + "+resolved"
			if err := k.Job.Set(ctx, jobId, job); err != nil {
				return err
			}
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"dispute_committee_silent",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("reason", "re-adjudication committee summoned but silent -> bond returned (network failure)"),
			))
			k.clearAuditCommittee(ctx, jobId) // audit closed: purge the anchored seats
			return nil
		}
		if !auditAnchored && !selected && k.isHumanDispute(ctx, jobId) {
			// (ii) Unfounded dispute. Neither an audit committee nor a redo committee: nobody was ever
			// summoned. The disputer mobilized no juror, so the bond goes to the Treasury (anti-grief).
			k.releaseHeld(ctx, &job)
			if pools, pErr := k.Pools.Get(ctx); pErr == nil {
				pools.Treasury += job.DisputeBond
				if err := k.Pools.Set(ctx, pools); err != nil {
					return err
				}
			}
			job.DisputeBond = 0 // consumed: neither confiscated twice nor refunded below
			job.State = job.State + "+resolved"
			if err := k.Job.Set(ctx, jobId, job); err != nil {
				return err
			}
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"dispute_unfounded",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("reason", "no committee (audit or redo) was ever summoned"),
			))
			k.clearAuditCommittee(ctx, jobId) // audit closed: purge the anchored seats
			return nil
		}
		// (iii) Otherwise (an anchored audit committee, or an empty sampling draw with no human
		// dispute) the verdict tally decides, with a clawback when the payment is not verified.
		cheated, valid, voters, invalidVotes, seats, distinctOps, maxOpVotes := k.auditVerdictTally(ctx, jobId, job.MinerId)
		// ADR-034 partition: the plurality behind the decision, recorded on the job whatever the
		// outcome (0 when nobody voted). The release gate reads the min of distinct_voters and the
		// largest share held by a single operator (`max_operator_votes/voters < 50 %`) over the
		// audits closed by quorum: "distinct >= 2" alone holds when one operator has 9 votes of 10.
		job.AuditDistinctVoters = uint64(distinctOps)
		job.AuditVoters = uint64(voters)
		job.AuditMaxOperatorVotes = uint64(maxOpVotes)
		switch {
		case auditSlashDecision(p, cheated, voters, invalidVotes, seats):
			// Cheating at the bar: hard slash + clawback. A silent primary lands here too: the
			// committee posts "0" when a reveal is missing, which the primary cannot forge.
			if err := k.slashCheatedPrimary(ctx, &job, p); err != nil {
				return err
			}
			// `+quorum` means the audit really took place: verdicts at the bar, "invalid" majority.
			// It is the only clawback that attests to a verification, unlike a `+noquorum` state,
			// which only attests to an absence of jurors.
			job.State = job.State + "+resolved+clawed+quorum"
		case auditVindicateDecision(p, valid, voters, voters-invalidVotes, seats):
			// Vindication requires the same relative bar as the slash:
			// `validVotes >= max(governed quorum, ⌈2/3 of the anchored seats⌉)` and a stake majority.
			// An absolute floor of 4 over 15 seats let roughly 27 % of the stake buy the vindication
			// of any cheat, where 67 % was needed to slash it: the absolute-versus-relative threshold
			// mistake ADR-032 and ADR-033 each closed elsewhere. Slash and vindication differ only by
			// the direction of the majority, never by the bar.
			//
			// This bar and the deferral below go together: without the deferral, an honest primary
			// short of ⌈2/3⌉ would fall from "vindicated" straight to "clawed back", so the stricter
			// bar would punish it.
			k.releaseHeld(ctx, &job) // held fee released, deferred burn executed at finality
			// A defeated griefer does not get its bond back. A vindication at the ⌈2/3⌉ bar is a
			// committee that ruled: the disputer was wrong. Falling through to refundDisputeBond at
			// the end of the function would make disputing an honest job free as soon as the bond is
			// armed, which erases the anti-grief property (dormant only while dispute_bond = 0). The
			// bond goes to the Treasury, as in the unfounded-dispute branch (ii) and in
			// AdjudicateDispute. The rule is uniform: return the bond when the disputer is right or
			// when no instance remains, confiscate it when the disputer is wrong, and keep it in
			// escrow while an instance is still possible.
			if job.DisputeBond > 0 {
				if pools, pErr := k.Pools.Get(ctx); pErr == nil {
					pools.Treasury += job.DisputeBond
					if err := k.Pools.Set(ctx, pools); err != nil {
						return err
					}
				}
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
					"dispute_bond_forfeited",
					sdk.NewAttribute("job_id", jobId),
					sdk.NewAttribute("reason", "primary vindicated at the relative bar -> the disputer was wrong (anti-grief)"),
				))
				job.DisputeBond = 0 // consumed: the refund at the end of the function becomes a no-op
			}
			// `+vindicated+quorum`: a committee at the relative bar confirmed the primary's honesty.
			job.State = job.State + "+resolved+vindicated+quorum"
		default:
			// Below quorum: defer. Neither threshold reached means an audit that did not take place,
			// the same diagnosis as a jury under the floor (audit_jury_floor.go), hence the same
			// answer. Nothing is closed: the fee stays held, the stake intact, the bond in escrow, the
			// anchored committee in place (the jurors remain summoned and hasOpenObligation protects
			// them from pruning), and a new deadline is set. The deferral is unbounded by design.
			// Releasing after N attempts would make evasion free, since an attacker able to keep the
			// committee silent would convert "audited" into "paid without verification"; clawing back
			// would punish an honest primary for a network failure, as it once did on real jobs.
			//
			// The clawback-on-silence path is gone, and with it the 27 % denial of service: the same
			// handful of silent seats that forced a no-quorum no longer make anyone lose.
			//
			// Accepted consequence: the volume of held escrow can grow on a network with a thin pool.
			// `held_total` and `held_oldest_age` make "held is not lost" falsifiable. `clawbackPayment`
			// (and silence_slash_bps) has no caller left on this path; a silent primary is still
			// punished by the slash branch, where ⌈2/3⌉ of "0" verdicts posted against a missing
			// reveal form a cheating quorum.
			// ADR-044, gesture 2: this comes before the appeal branch, and the order matters. An
			// appeal is one more postponement, and granting it on a debt already older than
			// `audit_unwind_blocks` is the unbounded deferral this bound exists to end. Dormant at 0,
			// where `unwindStrandedRetention` returns before reading anything. On success the helper
			// has resolved and persisted the job, so we return at once: the `job` held here is stale
			// from that point and must not be written back over it.
			if k.unwindStrandedRetention(ctx, h, jobId, p) {
				return nil
			}
			if allowAppeal && p.AppealWindow > 0 {
				// Governed appeal window, dormant in the shipped genesis. It converges on this same
				// deferral at the appeal deadline (allowAppeal=false, so default, so defer).
				if err := k.PendingAppealResolve.Set(ctx, collections.Join(h+int64(p.AppealWindow), jobId)); err != nil {
					return err
				}
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
					sdk.NewEvent("audit_appeal_scheduled", sdk.NewAttribute("job_id", jobId)),
				)
				return nil // job unchanged (+disputed, !resolved)
			}
			// Retry at the same cadence as the audit deadline, falling back to the deferral stride
			// when the timeout is not governed: the job must never become unreachable, which is the
			// liveness of the deferral loop itself.
			retry := int64(auditDeferStride)
			if p.AuditResolveTimeout > 0 {
				retry = int64(p.AuditResolveTimeout)
			}
			if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+retry, jobId)); err != nil {
				return err
			}
			sdkCtx := sdk.UnwrapSDKContext(ctx)
			sdkCtx.Logger().Error("SECURITY: audit committee anchored but below the relative quorum at the deadline -> audit deferred, no sanction. The primary is neither clawed back nor slashed: the silence of a committee is not a verdict. If this message persists, the network verifies nothing (eligible pool or juror participation too small).",
				"job_id", jobId, "voters", voters, "seats", seats, "next_attempt", h+retry)
			sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
				"audit_resolve_deferred",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("voters", strconv.Itoa(voters)),
				sdk.NewAttribute("seats", strconv.Itoa(seats)),
				sdk.NewAttribute("reason", "below quorum -> deferred (neither payment nor sanction)"),
			))
			return nil // job unchanged (+disputed, !resolved): nothing is closed, nothing moves
		}
	} else {
		job.State = job.State + "+resolved" // unhonoured human dispute: innocence by default
	}
	// The complete bond rule, case by case:
	//   slash (the disputer was right)                     -> bond returned (refundDisputeBond below);
	//   vindication (the disputer was wrong)               -> bond to the Treasury (consumed there);
	//   below quorum (silent committee, undecided)         -> kept in escrow and deferred (never here);
	//   unfounded dispute, no committee (ii)               -> Treasury (inside that branch);
	//   silent redo, last instance (i), or non-optimistic  -> returned (network failure, not the
	//   disputer's fault).
	// Return when the disputer is right or when no instance remains, confiscate when it is wrong, keep
	// in escrow while an instance is still possible. Once vindication sits at the relative bar, "only an
	// adjudication confiscates; a timeout means no committee ruled" no longer holds: a vindication at the
	// bar is a committee that ruled.
	if err := k.refundDisputeBond(ctx, &job); err != nil {
		return err
	}
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		return err
	}
	k.clearAuditCommittee(ctx, jobId) // audit resolved: purge the anchored seats (no lingering immunity, no unbounded growth)
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
		sdk.NewEvent("audit_expired", sdk.NewAttribute("job_id", jobId)),
	)
	return nil
}

// bumpAuditsOpened increments the monotonic counter of opened audits (ADR-034 partition).
// Best effort: this counter is observability (the right-hand side of the partition check, read by the
// AuditDeferred query), never a guard, so a store error must not prevent a real audit from opening. A
// wrong counter shows up because the sum stops balancing; it does no harm.
func (k Keeper) bumpAuditsOpened(ctx context.Context) {
	n, err := k.AuditsOpened.Get(ctx)
	if err != nil {
		n = 0
	}
	_ = k.AuditsOpened.Set(ctx, n+1)
}

// auditDraw is a deterministic draw in [0,10000): the first 8 bytes of H(domain || seed || jobId), mod
// 10000. Pure, so it is testable outside the keeper. `seed` comes after the commit, so the miner cannot
// predict the draw (anti-grinding).
func auditDraw(seed, jobId string) uint64 {
	sum := sha256.Sum256([]byte(auditDomain + seed + "|" + jobId))
	return binary.BigEndian.Uint64(sum[:8]) % 10000
}
