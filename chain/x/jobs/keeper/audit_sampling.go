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

// auditDomain — domain separation for the audit draw: it must never collide with the committee seed.
const auditDomain = "dendra/audit/v1|"

// auditDeferStride — how many blocks an audit draw is pushed back when no decentralized seed exists.
// The value balances two opposite costs. Too short a stride rewrites state every block for nothing:
// the absence of a seed typically lasts dozens of blocks at genesis, and potentially forever if no
// validator anchors a VRF key. Too long a stride delays the audit once the seed is back. 20 blocks is
// roughly 20 s at the target block time: invisible at the scale of `audit_resolve_timeout` (120), and
// 20x fewer writes while the seed is missing.
const auditDeferStride = 20

// runOptimisticAudit (ADR-025 M3) — at height `h`, for every OPTIMISTICALLY settled job scheduled at
// that height, draws an audit with the EFFECTIVE probability (see effectiveAuditBps: base + adaptive +
// probation). On a hit it OPENS a protocol dispute (`+disputed`, disputer = governance account, bond 0)
// that a fresh committee will adjudicate (M4), and schedules a self-resolution TIMEOUT for liveness.
// The `seed` (AppHash / VRF of the current block) is POSTERIOR to the commit, so the draw is
// unpredictable to the miner (anti-grinding).
// DORMANT: no-op when verification_mode != 1; PendingAudit is only fed by settleOptimistic (mode 1).
func (k Keeper) runOptimisticAudit(ctx context.Context, h int64, seed string) error {
	p, err := k.Params.Get(ctx)
	if err != nil || p.VerificationMode != 1 {
		return nil // redundant mode (or missing params) -> dormant; never blocks EndBlock
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAudit.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	if len(due) == 0 {
		return nil
	}
	// PROPOSER ANTI-GRINDING — a draw happens ONLY on a decentralized seed.
	//
	// `ProcessProposal` accepts a proposal that carries no vote-extension injection. Without injection
	// there is no decentralized seed at that height, and `committeeBaseSeed` falls back to
	// `AppHash(H-1):h` — which the proposer of block h knows BEFORE proposing. It could therefore
	// compute the favourable draw off-chain and then choose whether to inject: two seeds to pick from
	// at every block it proposes, and more if it drops votes beyond the required 2/3. That is exactly
	// the predictability ADR-032 closes on the sybil side, reopened on the consensus side.
	//
	// Rejecting the block is the intuitive answer; it also hands the attacker a chain halt switch
	// (propose without injection in a loop, get rejected in a loop). The BENEFIT is removed instead:
	// with no decentralized seed no draw happens, and the due jobs are simply DEFERRED. Omitting the
	// injection therefore moves nothing, and the backlog drains as soon as an honest proposer injects.
	if _, decentralized := k.committeeBaseSeedSourced(ctx, seed); !decentralized && p.CommitteeSeedSource == 1 {
		sdkCtx := sdk.UnwrapSDKContext(ctx)
		// DEFER BY A WIDE STRIDE, NOT BY ONE BLOCK.
		//
		// Deferring is the right answer — without a decentralized seed, drawing would let the proposer
		// choose — but deferring by ONE block replays it at EVERY block: two KV writes per job per
		// block, for a duration nothing bounds. The absence of a seed is not a brief incident: it is
		// the NORMAL state between block 1 and the moment validators anchor their VRF keys, and it can
		// last indefinitely if none of them ever does. The cost would therefore grow with traffic
		// during precisely the phase where the network is most fragile.
		//
		// A stride of `auditDeferStride` blocks divides the write amplification by as much, and changes
		// no property: the held fee stays HELD (fail closed, no funds move) and the backlog drains as
		// soon as a seed exists. The deferral never "gives up" after N attempts: releasing because the
		// audit could not run would hand an abstaining validator the power to push jobs through
		// unaudited — the same grinding, one step removed.
		for _, jobId := range due {
			if err := k.PendingAudit.Remove(ctx, collections.Join(h, jobId)); err != nil {
				return err
			}
			if err := k.PendingAudit.Set(ctx, collections.Join(h+auditDeferStride, jobId)); err != nil {
				return err
			}
		}
		sdkCtx.Logger().Error("SECURITY: no decentralized VRF seed at this height -> NO audit draw. Jobs deferred; held fees stay HELD. Usual cause: no validator has anchored a VRF key yet, or vote-extensions are not enabled. If this message persists, the network verifies NOTHING.",
			"height", h, "jobs_deferred", len(due), "next_attempt", h+auditDeferStride)
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"audit_draw_deferred",
			sdk.NewAttribute("height", strconv.FormatInt(h, 10)),
			sdk.NewAttribute("jobs", strconv.Itoa(len(due))),
			sdk.NewAttribute("reason", "seed is not decentralized"),
		))
		return nil
	}

	disputer, _ := k.addressCodec.BytesToString(k.GetAuthority()) // governance module account (zero bond)
	for _, jobId := range due {
		// The condition is `!jobIsResolved`, NOT `!jobIsDisputed`, and that is the whole point.
		//
		// Under `!jobIsDisputed` an already-disputed job was SKIPPED — yet its deadline was removed a
		// few lines below, for every due job without distinction, so the audit never happened.
		// `DisputeVerdict` only requires "paid and not already disputed": it does not forbid the
		// PRIMARY from disputing its own job, and the audit is scheduled one block after settlement,
		// a perfectly observable one-block window.
		//
		// A miner could therefore BUY ITS WAY OUT OF AN AUDIT for the price of `dispute_bond`. The
		// arithmetic is structurally unfavourable: the bond is a FIXED cost, the slash is
		// PROPORTIONAL to stake. Past `dispute_bond / slash_leak_bps` of stake the evasion turns
		// profitable, and the bigger the miner the more profitable it becomes — the exact opposite of
		// what an anti-cheat guard must produce. The job then fell into the "unfounded dispute"
		// branch, which releases the held fee: the unverified payment was finalized.
		//
		// A job disputed by a human is now AUDITED ANYWAY. Its committee is anchored, so
		// `resolveDisputedAudit` takes the real audit path instead of the unfounded branch, and
		// disputing protects from nothing.
		if job, jErr := k.Job.Get(ctx, jobId); jErr == nil && jobIsPaid(job.State) && !jobIsResolved(job.State) {
			// ADR-034 C3 — AN AUDIT ALREADY WON IS NEVER RE-DRAWN. A job whose draw was won
			// (`auditDraw < bps`) and then DEFERRED for lack of a jury carries the `AuditSelected`
			// marker. Without it the retry replayed the lottery against the seed of a NEW block: at
			// audit_sample_bps=5000, an already-selected audit was cancelled one time out of two ->
			// `releaseHeld` -> payment finalized with neither verification nor marker. That hands the
			// cheater its escape back through the side door. A selected job STAYS selected: the lottery
			// is skipped and only the committee draw is retried.
			alreadySelected, aErr := k.AuditSelected.Has(ctx, jobId)
			if aErr != nil {
				return aErr
			}
			if alreadySelected || auditDraw(seed, jobId) < k.effectiveAuditBps(ctx, p, job) {
				// ADR-034 partition — count the OPENING once per job: on the FIRST selection (a
				// retry arrives with alreadySelected=true), and never when a human dispute already
				// counted it (the audit ADDS to the dispute, it does not replace it — same reasoning
				// as not overwriting Disputer below).
				if !alreadySelected && !jobIsDisputed(job.State) {
					k.bumpAuditsOpened(ctx)
				}
				// DO NOT OVERWRITE AN OPEN HUMAN DISPUTE. The job may already be `+disputed` by
				// someone who escrowed a real bond. Rewriting `Disputer` and resetting `DisputeBond`
				// to 0 would leave those coins in the module account with no owner — orphaned funds
				// created by the very guard meant to protect them. The audit adds to the human
				// dispute; it does not replace it.
				if !jobIsDisputed(job.State) {
					job.Disputer = disputer
					job.DisputeBond = 0
					job.DisputeHeight = h
					job.State = job.State + "+disputed"
				}
				// ADR-032 — ANCHOR THE AUDIT COMMITTEE at draw time, not after. This is what turns
				// "any registered miner may vote" into "only the summoned vote".
				//
				// Splitting into identities does not buy seats — it costs them. At constant total
				// stake, the probability of taking slot 0 falls from 51.4 % with one identity to
				// 38.6 % with sixteen: dividing the stake divides the score. Measured on the exact
				// comparator used by lessByStakeScore.
				//
				// What unpredictability does NOT buy. "The seed is posterior to the primary's commit,
				// therefore nobody can place itself in the committee" is false. Unpredictability stops
				// an ALREADY REGISTERED miner from placing itself; it stops nothing for a miner
				// CREATED AFTER the seed is public, which can try `miner_id` values offline until
				// sha256(seed|id) is small. Hashing is free, stake is not: an attacker holding 1 stake
				// against an honest miner holding 1,000,000 takes slot 0 in 38 % of draws at 2^20
				// tries, 94 % at 2^24, 99.9 % at 2^30. Stake weighting protects nothing against an
				// identifier chosen AFTER the seed.
				//
				// The guard that closes this is not here: it is the POOL FREEZE (ADR-037). The
				// identifier must be committed BEFORE the seed exists, so drawAuditCommittee only
				// admits miners whose registration height is at or before the job's frozen pool
				// height (JobPoolFreezeHeight, set once at OpenJob and shared by all three draws).
				members, mErr := k.drawAuditCommittee(ctx, seed, jobId, job.MinerId, disputer)
				if mErr != nil {
					return mErr
				}
				// JURY TOO SMALL -> DEFER, NO SANCTION (attack and detail: audit_jury_floor.go).
				// Opening a dispute whose bar the floor makes unreachable condemns the primary BEFORE
				// any vote: at the timeout, `runAuditResolveTimeout` claws back its payment without a
				// single juror having ruled.
				if floor := effectiveSlashFloor(p); len(members) < floor {
					// C3: MARK before deferring — the retry must not replay the lottery.
					if err := k.AuditSelected.Set(ctx, jobId); err != nil {
						return err
					}
					if err := k.deferAuditForSmallJury(ctx, h, jobId, len(members), floor); err != nil {
						return err
					}
					continue
				}
				// Committee large enough: the audit opens. The selection marker has no purpose left
				// (the job is now tracked by AuditCommittee + PendingAuditResolve) — remove it so the
				// KeySet does not leak entries.
				if err := k.AuditSelected.Remove(ctx, jobId); err != nil {
					return err
				}
				// anchorCommittee writes the seats AND the anchoring height together: a juror lock is
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
				// Not audited means the optimistic payment is FINAL: release the held fee to the primary.
				k.releaseHeld(ctx, &job)
			}
		}
		if err := k.PendingAudit.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// effectiveAuditBps (ADR-025 M6) — the EFFECTIVE audit rate of an optimistic job. Everything below is
// dormant by default:
//   - PROBATION (audit_probation_jobs>0): 100 % while the primary has served <= N jobs (anti-Sybil and
//     cold start).
//   - ADAPTIVE (audit_adaptive>0): otherwise base·(1 + fee/ref) capped at 10000, so a large job is
//     audited more often.
//   - otherwise -> audit_sample_bps unchanged (M3 behaviour).
func (k Keeper) effectiveAuditBps(ctx context.Context, p types.Params, job types.Job) uint64 {
	if p.AuditProbationJobs > 0 && job.MinerId != "" {
		if cnt, err := k.MinerOptimisticCount.Get(ctx, job.MinerId); err == nil && cnt <= p.AuditProbationJobs {
			return 10000
		}
	}
	eff := p.AuditSampleBps
	if p.AuditAdaptive > 0 {
		eff += p.AuditSampleBps * job.Fee / p.AuditAdaptive // fee<=1e13, bps<=1e4 -> product <2^57, no overflow
		if eff > 10000 {
			eff = 10000
		}
	}
	return eff
}

// runAuditResolveTimeout (ADR-025 liveness + ADR-028 anti-evasion) — at `h`, handles the optimistic
// audits that reached their deadline without being adjudicated. The optimistic primary has been PAID,
// so it is the SUSPECT. The decision rests on the fresh committee's VERDICTS
// (`<jobId>__verdict__<minerId>`, "0" = invalid, stake-weighted tally) against a SYMMETRIC RELATIVE BAR
// (⌈2/3⌉ of the anchored seats, with the governed quorum as a lower bound):
//   - invalidVotes >= bar AND a stake majority for "invalid" -> CHEATING -> hard slash + clawback. A
//     SILENT primary arrives here without any forgeable on-chain marker: the committee itself posts "0"
//     when it cannot obtain a usable reveal, which is a signal the primary cannot fake.
//   - validVotes >= bar AND a stake majority for "valid" -> VINDICATED, at the SAME bar as the slash.
//     About 27 % of the stake no longer buys the vindication of a cheat: only the DIRECTION of the
//     majority differs between the two outcomes, never the bar.
//   - otherwise -> DEFER. This is not a verdict, it is an audit that did not take place. Nothing is
//     closed and nothing moves (fee held, stake intact, bond kept), and a new deadline is set. The
//     deferral is unbounded on purpose: bounding it would make evasion free.
//     There is no `<jobId>__reveal__` branch: the decision rests on verdicts ALONE — the reveal stays
//     an OFF-CHAIN exchange between the primary and the committee.
//
// A NON-optimistic (human) dispute left unhonoured keeps innocence by default: the disputer is the
// accuser. DORMANT: nothing is scheduled when audit_resolve_timeout == 0.
func (k Keeper) runAuditResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil // missing params -> never blocks EndBlock
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAuditResolve.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if err := k.resolveDisputedAudit(ctx, h, jobId, p, true); err != nil { // allowAppeal=true: may defer to an appeal
			return err
		}
		if err := k.PendingAuditResolve.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// runAppealResolveTimeout — the SECOND, appeal deadline. A primary deferred at the first timeout
// (appeal_window>0) is re-judged here unless it was resolved in the meantime, typically by
// AdjudicateDispute during the window. The tally runs again: a LATE reveal may have produced a VALID
// quorum, which vindicates the primary and releases the held fee. Otherwise `allowAppeal=false` falls
// into the default branch and DEFERS again — an audit without a verdict is not closed, it waits, and
// the deferral loop is the same one the audit timeout uses. DORMANT: PendingAppealResolve is only fed
// when appeal_window > 0.
func (k Keeper) runAppealResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAppealResolve.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if err := k.resolveDisputedAudit(ctx, h, jobId, p, false); err != nil { // allowAppeal=false: last instance
			return err
		}
		if err := k.PendingAppealResolve.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// resolveDisputedAudit — handles ONE optimistic audit job at its deadline (audit timeout or appeal
// timeout), on the fresh committee's verdicts against the symmetric relative bar. Both timeouts share
// this function so they can never diverge. Below quorum it DEFERS: new deadline, job unchanged, no
// funds move; when `allowAppeal` and `appeal_window>0`, the deferral goes through the second, appeal
// deadline first, to leave room for a late reveal. The job is mutated and persisted ONLY on a real
// resolution (slash, vindication, or one of the human-dispute branches).
func (k Keeper) resolveDisputedAudit(ctx context.Context, h int64, jobId string, p types.Params, allowAppeal bool) error {
	job, jErr := k.Job.Get(ctx, jobId)
	if jErr != nil || !jobIsDisputed(job.State) || jobIsResolved(job.State) {
		return nil // already resolved (e.g. by AdjudicateDispute during the window) or absent -> nothing to do
	}
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		// TWO SILENCES THAT MUST NOT BE CONFLATED.
		//
		// This path was designed for SAMPLED jobs: an audit committee was drawn and ANCHORED, so the
		// absence of verdicts is NEGATIVE EVIDENCE ("the payment could not be verified") and clawing
		// the price back is the conclusion ADR-028 wants.
		//
		// Since HUMAN disputes carry a deadline, jobs that were NEVER sampled also arrive here. For
		// them no committee was ever summoned: `auditVerdictTally` reads `AuditCommittee[jobId]`,
		// which only `runOptimisticAudit` writes, so the tally STRUCTURALLY returns zero voters and
		// the `default` branch clawed back the fee of an honest, already-paid miner. Cost to the
		// attacker: the gas, since `dispute_bond` is 0. The primary had no way to defend itself
		// because no verdict could ever count.
		//
		// Nobody's silence is not a committee's silence. With no body summoned the dispute is not
		// honoured: the held fee is released to the miner and the bond goes to the Treasury — exactly
		// the anti-grief role of the bond. The only difference with a drawn committee that stayed
		// silent is that there the failure belongs to the network, so the bond is returned.
		//
		// THE CONDITION NEEDS BOTH TERMS. "No anchored committee" alone is NOT enough: a genuinely
		// sampled job whose draw returned nobody (pool too small) has no anchor either, and its
		// payment really is unverified — exempting it from the clawback would disarm ADR-028. Only
		// the conjunction "opened by a human AND never summoned" describes an unfounded dispute.
		//
		// Two committees may have been summoned, under TWO keys: `<jobId>` (sampling) and
		// `<jobId>__redo` (human dispute). Reading only the first turns a human dispute whose redo
		// committee was ANCHORED but SILENT into "no committee", which falls through to "unfounded"
		// and confiscates the bond of an honest disputer because the DRAWN jurors did not answer.
		_, auditAnchored := k.auditCommitteeAllowed(ctx, jobId)
		allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, jobId)
		if !auditAnchored && redoAnchored {
			// (i) COMMITTEE SUMMONED BUT SILENT (or below quorum). A redo committee exists and the
			// adjudication did not conclude by the deadline: a network failure, not the disputer's
			// fault. The held fee is released to the primary (no proof of cheating) AND the bond is
			// returned.
			//
			// The `responders / seats` pair is emitted before resolving. It is the measurement point
			// for the quorum parameter: it cannot be rolled back, and it captures exactly the disputes
			// whose permissionless adjudication failed for lack of quorum.
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
			k.clearAuditCommittee(ctx, jobId) // audit closed -> purge the anchored seats
			return nil
		}
		if !auditAnchored && k.isHumanDispute(ctx, jobId) {
			// (ii) UNFOUNDED DISPUTE. Neither an audit committee nor a redo committee: nobody was ever
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
			k.clearAuditCommittee(ctx, jobId) // audit closed -> purge the anchored seats
			return nil
		}
		// (iii) otherwise: an anchored audit committee, or an empty sampling draw with no human
		// dispute -> the verdict tally decides (clawback when the payment is not verified).
		cheated, valid, voters, invalidVotes, seats, distinctOps, maxOpVotes := k.auditVerdictTally(ctx, jobId, job.MinerId)
		// ADR-034 partition — the PLURALITY behind the decision, recorded on the job whatever the
		// outcome (0 when nobody voted). The release gate reads the MIN of distinct_voters AND the
		// largest share held by a single operator (`max_operator_votes/voters < 50 %`) over the audits
		// closed by quorum: "distinct >= 2" alone is satisfied when one operator holds 9 votes out of 10.
		job.AuditDistinctVoters = uint64(distinctOps)
		job.AuditVoters = uint64(voters)
		job.AuditMaxOperatorVotes = uint64(maxOpVotes)
		switch {
		case auditSlashDecision(p, cheated, voters, invalidVotes, seats):
			// Cheating at the bar -> HARD slash + clawback. A SILENT primary lands here too: the
			// committee posts "0" when a reveal is missing, which the primary cannot forge.
			if err := k.slashCheatedPrimary(ctx, &job, p); err != nil {
				return err
			}
			// `+quorum` means the audit REALLY took place: verdicts at the bar, "invalid" majority.
			// It is the only clawback that attests to a verification — never to be confused with a
			// `+noquorum` state, which attests only to an absence of jurors.
			job.State = job.State + "+resolved+clawed+quorum"
		case auditVindicateDecision(p, valid, voters, voters-invalidVotes, seats):
			// VINDICATION requires the SAME RELATIVE BAR as the slash:
			// `validVotes >= max(governed quorum, ⌈2/3 of the anchored seats⌉)` AND a stake majority.
			// An ABSOLUTE floor of 4 over 15 seats let roughly 27 % of the stake BUY the vindication
			// of any cheat, where 67 % was needed to slash it — the same absolute-versus-relative
			// threshold mistake ADR-032 and ADR-033 each closed elsewhere. What differs between slash
			// and vindication is the DIRECTION of the majority, never the bar.
			//
			// Raising this bar and the deferral below are ATOMIC: without the deferral, an honest
			// primary short of ⌈2/3⌉ would fall from "vindicated" straight to "clawed back", so the
			// stricter bar would PUNISH it.
			k.releaseHeld(ctx, &job) // held fee released, deferred burn executed at finality
			// A DEFEATED GRIEFER DOES NOT GET ITS STAKE BACK. A vindication at the ⌈2/3⌉ bar IS a
			// committee that ruled: the disputer was WRONG. Falling through to the refundDisputeBond
			// at the end of the function would make disputing an honest job FREE the moment the bond
			// is armed, which erases the whole anti-grief property (dormant only while
			// dispute_bond = 0). The bond goes to the Treasury, as in the unfounded-dispute branch (ii)
			// and in AdjudicateDispute. The rule is uniform: RETURN when the disputer is right or when
			// no instance remains; CONFISCATE when it is wrong; ESCROW while an instance is still
			// possible.
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
					sdk.NewAttribute("reason", "primary VINDICATED at the relative bar -> the disputer was wrong (anti-grief)"),
				))
				job.DisputeBond = 0 // consumed: the refund at the end of the function becomes a no-op
			}
			// `+vindicated+quorum`: a committee at the relative bar CONFIRMED the primary's honesty.
			job.State = job.State + "+resolved+vindicated+quorum"
		default:
			// BELOW QUORUM => DEFER. Neither threshold reached means this is not a verdict, it is an
			// audit that did not take place — the same diagnosis as a jury under the floor
			// (audit_jury_floor.go), hence the same answer. Nothing is closed: the fee stays HELD, the
			// stake INTACT, the bond KEPT in escrow, the anchored committee KEPT (the jurors remain
			// summoned, and are protected from pruning by hasOpenObligation), and a NEW deadline is
			// set. The deferral is unbounded by design. Releasing after N attempts would make evasion
			// free, since an attacker able to keep the committee silent would convert "audited" into
			// "paid without verification"; clawing back would punish an honest primary for a network
			// failure, and did so publicly on real jobs.
			//
			// The clawback-on-silence path disappears, and with it the 27 % denial of service: the
			// same handful of silent seats that forced a no-quorum no longer make anyone lose.
			//
			// ACCEPTED CONSEQUENCE: the volume of held escrow can grow on a network with a thin pool.
			// `held_total` and `held_oldest_age` are what make "held is not lost" FALSIFIABLE rather
			// than a promise. `clawbackPayment` (and silence_slash_bps) has no caller left on this
			// path — a genuinely silent primary is still punished by the slash branch, where ⌈2/3⌉ of
			// "0" verdicts posted against a missing reveal form a cheating quorum.
			if allowAppeal && p.AppealWindow > 0 {
				// Governed appeal window, dormant in the shipped genesis. It converges on this same
				// deferral at the appeal deadline (allowAppeal=false -> default -> defer).
				if err := k.PendingAppealResolve.Set(ctx, collections.Join(h+int64(p.AppealWindow), jobId)); err != nil {
					return err
				}
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
					sdk.NewEvent("audit_appeal_scheduled", sdk.NewAttribute("job_id", jobId)),
				)
				return nil // job UNCHANGED (+disputed, !resolved)
			}
			// RETRY at the same cadence as the audit deadline, falling back to the deferral stride
			// when the timeout is not governed: the job must NEVER become unreachable, which is the
			// liveness of the deferral loop itself.
			retry := int64(auditDeferStride)
			if p.AuditResolveTimeout > 0 {
				retry = int64(p.AuditResolveTimeout)
			}
			if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+retry, jobId)); err != nil {
				return err
			}
			sdkCtx := sdk.UnwrapSDKContext(ctx)
			sdkCtx.Logger().Error("SECURITY: audit committee ANCHORED but BELOW the relative quorum at the deadline -> audit DEFERRED, no sanction. The primary is neither clawed back nor slashed: the silence of a committee is not a verdict. If this message persists, the network verifies NOTHING (eligible pool or juror participation too small).",
				"job_id", jobId, "voters", voters, "seats", seats, "next_attempt", h+retry)
			sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
				"audit_resolve_deferred",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("voters", strconv.Itoa(voters)),
				sdk.NewAttribute("seats", strconv.Itoa(seats)),
				sdk.NewAttribute("reason", "below quorum -> deferred (neither payment nor sanction)"),
			))
			return nil // job UNCHANGED (+disputed, !resolved): nothing is closed, nothing moves
		}
	} else {
		job.State = job.State + "+resolved" // unhonoured human dispute -> innocence by default
	}
	// THE COMPLETE BOND RULE — a decision, not an accident of control flow:
	//   slash (the disputer was RIGHT)              -> bond RETURNED (refundDisputeBond below);
	//   vindication (the disputer was WRONG)        -> bond to the TREASURY (consumed in that case);
	//   below quorum (silent committee, undecided)  -> ESCROWED + deferred (never reaches here);
	//   unfounded dispute, no committee (ii)        -> TREASURY (inside that branch);
	//   silent redo, last instance (i), or non-optimistic -> RETURNED (network failure, not the
	//   disputer's fault).
	// RETURN when the disputer is right or when no instance remains; CONFISCATE when it is wrong;
	// ESCROW while an instance is still possible. "Only an adjudication confiscates; a timeout means
	// no committee ruled" is false once vindication sits at the relative bar: a vindication at the bar
	// IS a committee that ruled.
	if err := k.refundDisputeBond(ctx, &job); err != nil {
		return err
	}
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		return err
	}
	k.clearAuditCommittee(ctx, jobId) // audit resolved -> purge the anchored seats (no lingering immunity, no unbounded growth)
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
		sdk.NewEvent("audit_expired", sdk.NewAttribute("job_id", jobId)),
	)
	return nil
}

// bumpAuditsOpened — increments the MONOTONIC counter of opened audits (ADR-034 partition).
// Best effort: this counter is OBSERVABILITY (it is the right-hand side of the partition check, read
// by the AuditDeferred query), never a guard — a store error must not prevent a real audit from
// opening. A wrong counter SHOWS UP, because the sum stops balancing; it does not harm anything.
func (k Keeper) bumpAuditsOpened(ctx context.Context) {
	n, err := k.AuditsOpened.Get(ctx)
	if err != nil {
		n = 0
	}
	_ = k.AuditsOpened.Set(ctx, n+1)
}

// auditDraw — deterministic draw in [0,10000): the first 8 bytes of H(domain‖seed‖jobId), mod 10000.
// Pure, so it is testable outside the keeper. `seed` is posterior to the commit, which makes the draw
// unpredictable to the miner (anti-grinding).
func auditDraw(seed, jobId string) uint64 {
	sum := sha256.Sum256([]byte(auditDomain + seed + "|" + jobId))
	return binary.BigEndian.Uint64(sum[:8]) % 10000
}
