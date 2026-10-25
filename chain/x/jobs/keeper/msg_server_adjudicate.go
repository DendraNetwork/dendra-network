package keeper

import (
	"context"
	"strconv"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// AdjudicateDispute closes a dispute permissionlessly, in place of the interim authority of
// ResolveDispute. Governance is not needed: the chain reads the re-commits of a fresh committee and
// decides on its own.
//
// Mechanism:
//   - After the `dispute_window`, a fresh committee has re-executed the job off-chain and anchored its
//     results under the key "<jobId>__redo__<minerId>" (through the existing CreateCommit, signed by
//     each operator).
//   - Only registered miners count (real stake, anti-sybil), and only those outside the original
//     committee (derivable from the beacon, for independence and anti-collusion).
//   - The verdict is stake-weighted (splitting a stake does not change influence): a slashed miner
//     whose original commit gathers a strict majority of the fresh stake was in fact correct, so its
//     slash is reversed (restitution from the Treasury) and the disputer is refunded and rewarded.
//     Otherwise the disputer's bond goes to the Treasury (anti-grief).
//
// Trust moves from "honest majority of the committee" to "at least one honest bonded party plus a
// non-colluding fresh committee"; if the fresh committee colludes too, only the majority assumption
// remains (at scale, two independent colluding committees are far harder to obtain). Dormant by default
// (`dispute_window=0`).
func (k msgServer) AdjudicateDispute(ctx context.Context, msg *types.MsgAdjudicateDispute) (*types.MsgAdjudicateDisputeResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	p, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown job")
	}
	if !jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no open dispute for this job")
	}
	if jobIsResolved(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "dispute already resolved (anti-replay)")
	}
	// Re-commit window: gives the fresh committee time to re-submit its results off-chain.
	if p.DisputeWindow > 0 && sdk.UnwrapSDKContext(ctx).BlockHeight() < job.DisputeHeight+int64(p.DisputeWindow) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "re-adjudication window has not elapsed")
	}

	// The original committee (derivable from the beacon) is excluded from the fresh tally.
	orig, err := k.assignedCommittee(ctx, msg.JobId, effectiveWorkCommitteeSize(p))
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// The voting set of the fresh committee must be anchored, like the verdict tally.
	// Otherwise any registered miner outside the original committee could vote: three identities at
	// `min_stake` would hold 100% of `totalRedoStake`, enough to get an honest miner slashed or a real
	// cheater's slash refunded from the Treasury. No anchor means fail closed; the audit timeout is the way out.
	//
	// `authors` holds the authors of the contested commits, which differ from the drawn set. Anchoring
	// already refuses them a seat; this second read catches the one case anchoring cannot see: a juror
	// already anchored that afterwards writes its own commit on the disputed job, which nothing forbids.
	authors, aErr := k.commitAuthorsOf(ctx, msg.JobId)
	if aErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, aErr.Error())
	}

	allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, msg.JobId)

	// Re-commits of the fresh committee (key "<jobId>__redo__<minerId>"): miners drawn and anchored, outside the original committee, stake-weighted.
	redoPrefix := msg.JobId + "__redo__"
	type freshVote struct {
		vec   []int64
		stake uint64
	}
	fresh := []freshVote{}
	var totalRedoStake uint64
	freshOps := map[string]int{} // votes per operator behind the counted re-commits
	if err := k.Commit.Walk(ctx, commitRange(redoPrefix), func(key string, c types.Commit) (bool, error) {
		if !redoAnchored {
			return true, nil // no committee drawn: no admissible vote (fail closed)
		}
		if !strings.HasPrefix(key, redoPrefix) {
			return false, nil
		}
		mid := strings.TrimPrefix(key, redoPrefix)
		if !allowedRedo[mid] {
			return false, nil // not drawn, so it does not vote (a sybil can no longer invite itself)
		}
		if orig[mid] || authors[mid] {
			return false, nil // excludes the drawn committee and the authors of the contested result
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // must be a registered miner (real stake, anti-sybil)
		}
		v := parseIntVec(c.ResultCommit)
		if v == nil {
			return false, nil
		}
		fresh = append(fresh, freshVote{vec: v, stake: m.Stake})
		totalRedoStake += m.Stake
		freshOps[m.Operator]++
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Verdicts of the fresh committee (key "<jobId>__verdict__<minerId>", LLM-as-judge).
	// A participation floor is required (>= effectiveSlashFloor(p) distinct voters, stake-weighted): a slash
	// is never pronounced below that floor (against false positives, cross-GPU non-determinism and sybils).
	// Below the floor, with neither enough re-commits nor enough verdicts, the adjudication does not
	// conclude here, and the audit timeout decides instead.
	verdictCheated, verdictValid, verdictVoters, verdictInvalid, verdictSeats, verdictDistinctOps, verdictMaxOp := k.auditVerdictTally(ctx, msg.JobId, job.MinerId)
	verdictCanSlash := verdictVoters >= effectiveSlashFloor(p) // effective participation floor (governed when audit_min_quorum>0, else the v1 fallback) for a hard slash

	// The re-commit denominator is relative to the anchored seats, not to 3.
	//
	// A fixed gate of `len(fresh) >= CommitteeSize` = 3 ignores the number of seats actually drawn
	// (15). Three responders out of fifteen, 20% of the seats, would then pronounce a hard slash or a
	// restitution from the Treasury, with the majority computed over `totalRedoStake`, the stake of the
	// responders alone. That is the denominator mistake the relative bar closes on the verdict side,
	// a correct threshold whose base changed underneath it. The same rule applies here (participation
	// quorum at ceil(2/3 of the anchored seats)), so the decision requires a supermajority of the
	// convened committee rather than a handful of volunteers. The absolute floor is CommitteeSize, so
	// it never drops below 3.
	// A seat whose holder may not vote is not a seat. This used to count every anchored seat while the
	// filter above drops the excluded ones, so widening the exclusion shrank the numerator and left the
	// denominator untouched: each exclusion silently raised the bar for the remaining jurors, and
	// enough of them made the quorum unreachable on a job already paid for. Excluded seats therefore
	// leave both sides of the ratio at once.
	redoSeats := 0
	for mid := range allowedRedo {
		if orig[mid] || authors[mid] {
			continue
		}
		redoSeats++
	}
	redoQuorum := (2*redoSeats + 2) / 3 // ceil(2/3 x anchored seats), integer arithmetic
	if redoQuorum < redoQuorumFloor {   // the redo floor, not the governed work committee (policy_params.go)
		redoQuorum = redoQuorumFloor
	}
	if len(fresh) < redoQuorum && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "fresh committee below quorum (re-commits < 2/3 of the anchored seats and verdicts < participation floor) -> the audit timeout will decide")
	}
	if totalRedoStake == 0 && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "fresh committee carries no stake")
	}

	// Participation is measured on success too, otherwise the sample is censored.
	//
	// The timeout emits `redo_participation` only for disputes that did not reach quorum, by definition
	// of a timeout. Measuring there alone observes only the left tail: every measurement would show
	// `responders < quorum` because that is the sample being collected, and calibration would be done on
	// censored data. Here the gate has been cleared and the adjudication is going to commit, so the event
	// survives (no rollback). The two emission points together give the full distribution, and
	// `resolved_by` separates the two populations at analysis time.
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
		"redo_participation",
		sdk.NewAttribute("job_id", msg.JobId),
		sdk.NewAttribute("seats", strconv.Itoa(len(allowedRedo))),
		sdk.NewAttribute("responders", strconv.Itoa(len(fresh))),
		sdk.NewAttribute("resolved_by", "adjudication"),
	))

	pools, perr := k.Pools.Get(ctx)
	if perr != nil {
		pools = types.Pools{}
	}

	// Stake-weighted verdict: a slashed miner is vindicated if its original commit gathers a strict
	// majority of the fresh stake (totalRedoStake <= the 1e13 udndr supply, so agree*2 cannot overflow uint64).
	upheld := false
	for _, rec := range job.SlashRecords {
		oc, e := k.Commit.Get(ctx, msg.JobId+"__"+rec.MinerId)
		if e != nil {
			continue
		}
		ov := parseIntVec(oc.ResultCommit)
		if ov == nil {
			continue
		}
		var agree uint64
		for _, fv := range fresh {
			if cosineGE(ov, fv.vec, semanticThresholdBps) {
				agree += fv.stake
			}
		}
		if agree*2 <= totalRedoStake {
			continue // no strict majority of the fresh stake: not vindicated
		}
		// vindicated: restitution (accounting only, since the coins have been in the module since the slash), bounded.
		amt := rec.Amount
		if amt > pools.Treasury {
			amt = pools.Treasury
		}
		if amt > 0 {
			if mm, me := k.Miner.Get(ctx, rec.MinerId); me == nil {
				mm.Stake += amt
				if err := k.Miner.Set(ctx, mm.MinerId, mm); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
				pools.Treasury -= amt
			}
		}
		upheld = true
	}

	// Optimistic audit resolution. The k=1 settlement paid a primary without slashing it (no
	// SlashRecord). If that optimistic job is challenged (VRF audit M3, or a human dispute), the primary's
	// original commit is compared to the stake majority of the fresh committee. Divergence means the primary
	// was paid for a false result: hard slash (SlashLeakBps) + SlashRecord (refundable if it later
	// challenges successfully) + valid dispute. Agreement means the primary is vindicated and nothing moves.
	// This is what makes cheating -EV (Nash).
	// Fee-hold v2: the optimistic verdict is remembered so that the retention is consumed at the end of the
	// resolution; otherwise HeldFee/HeldBurn would stay frozen on this permissionless path.
	optimisticCheated := false
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		cheated := false
		if verdictCanSlash {
			// The decision rule in one sentence: slash if and only if participation >= the effective floor,
			// "invalid" >= ceil(2/3 of the anchored seats), and a stake majority is invalid. The bar is
			// relative to the anchored seats in both branches. Detail and cost: `auditRelativeBar`.
			cheated = auditSlashDecision(p, verdictCheated, verdictVoters, verdictInvalid, verdictSeats)
			// The vindication bar is the twin of the slash bar and must be just as relative. This path used
			// to vindicate at the absolute floor: `verdictCanSlash` (4 voters) was enough to enter here, and
			// "not cheating" ended in `releaseHeld` below, a vindication pronounced by roughly 27% of the
			// stake, the hole the timeout path closes. Now, with neither slash nor vindication at the relative
			// bar, the adjudication does not conclude (error, no funds move, the audit stays open, the
			// timeout decides). A permissionless tx that refuses is the equivalent of the EndBlocker
			// deferring.
			if !cheated && !auditVindicateDecision(p, verdictValid, verdictVoters, verdictVoters-verdictInvalid, verdictSeats) {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
					"verdicts below the relative bar (neither slash nor vindication at 2/3 of the anchored seats) -> the adjudication does not conclude; the audit timeout will defer")
			}
		} else {
			// Fallback (M4): cosine comparison of the primary's original commit against the fresh stake
			// majority (reachable only with >= CommitteeSize re-commits, which the gate above guarantees).
			if oc, e := k.Commit.Get(ctx, msg.JobId+"__"+job.MinerId); e == nil {
				if ov := parseIntVec(oc.ResultCommit); ov != nil {
					var agree uint64
					for _, fv := range fresh {
						if cosineGE(ov, fv.vec, semanticThresholdBps) {
							agree += fv.stake
						}
					}
					// The `totalRedoStake > 0` guard is needed: without it `0*2 <= 0` is true,
					// so an empty electorate (committee not anchored, or drawn but silent) would pronounce
					// "the primary cheated". Silence does not convict; the audit timeout handles the
					// absence of an answer.
					//
					// A tie neither convicts nor vindicates. Written as the non-strict
					// `agree*2 <= totalRedoStake`, this test sent an exact 50/50 to a hard slash
					// (SlashLeakBps), while the rule here is "disagrees with the majority", and a tie is
					// not a majority. The twin computation, `auditVerdictTally`, decides strictly on both
					// sides (`invalidStake*2 > totalStake` for cheated, `<` for valid) and leaves a tie
					// undecided: two tables of the same shape must not carry two different rules. This is
					// a real case: the redo quorum is satisfied by responders of equal stake (min_stake),
					// so 5 against 5 over 10 seats is an exact split, and an adversary holding half the
					// fresh stake, not a majority, could get an honest miner slashed by 80%.
					// The rule is therefore strict on both sides, and a tie refuses the adjudication, like
					// the verdict branch above. No funds move, the audit stays open, the timeout decides;
					// a permissionless tx that refuses is the equivalent of the EndBlocker deferring.
					//
					// The price of this rule, stated where it will be read: it moves the harm without
					// removing it. An adversary holding exactly half the fresh stake can force the tie and
					// turn the adjudication into a deferral, buying time at will for as long as it holds
					// parity. That is the trade-off optimistic verification assumes (protect the honest party, accept
					// some evasion), and it remains the right one: a deferral pays no one and erases
					// nothing, whereas a false slash is irreversible in reputation. The stalling lever is
					// declared so that the next reader does not "fix" it in the direction that reopens the
					// false slash. If stalling is ever observed, the answer is to bound the number of
					// deferrals, not to make a tie convict.
					if totalRedoStake > 0 {
						switch {
						case agree*2 < totalRedoStake:
							cheated = true // strict majority disagreement in fresh stake
						case agree*2 == totalRedoStake:
							return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
								"fresh stake split evenly (neither majority disagreement nor majority agreement) -> the adjudication does not conclude; the audit timeout will defer")
						}
					}
				}
			}
		}
		optimisticCheated = cheated
		if cheated {
			// `upheld` follows the verdict, not the survival of the miner. The primary may be absent: a
			// miner that exited or was pruned before the exit guard existed can outlive its job. Setting
			// `upheld = true` inside the successful `Miner.Get` block meant that a cheat confirmed at the
			// bar with a vanished primary left `upheld=false`, so the bond of a disputer who was right went
			// to the Treasury as "anti-grief". Two decisions, one basis each: the bond follows what the
			// committee said, the slash follows what is left to slash. On this path, upheld ==
			// optimisticCheated by construction.
			upheld = true
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				amt := m.Stake * p.SlashLeakBps / 10000
				m.Stake -= amt
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
				// What already left the module is owed by the bond, on this path too.
				// `slashCheatedPrimary` (the timeout path) has always applied it: the client is refunded
				// from the retention, and whatever was already paid out (the immediate part at a partial
				// hold) comes out of the slash, the rest going to the Treasury. This path skipped it and
				// sent the whole slash to the Treasury. Until epoch 14 that was hidden on a job already
				// final, because the refund reversed "the cut" from Pools; since the cut is paid out at
				// finality, a cheat proven after it left the client with nothing back. It is paid first,
				// before the whistleblower's reward reads the Treasury: restitution to the party that paid
				// comes before a bonus.
				fromBond := uint64(0)
				if amt > 0 {
					if retained := k.retainedTotal(ctx, job.JobId); job.Fee > retained {
						fromBond = k.refundClient(ctx, &job, job.Fee-retained, amt)
					}
				}
				pools.Treasury += amt - fromBond
				if amt > 0 {
					job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
				}
			}
		}
	}

	if upheld {
		// Valid dispute: refund the bond and reward the disputer (whistleblower), bounded by the remaining Treasury.
		bond := job.DisputeBond
		reward := bond
		if reward > pools.Treasury {
			reward = pools.Treasury
		}
		if payout := bond + reward; payout > 0 {
			if dBz, de := k.addressCodec.StringToBytes(job.Disputer); de == nil {
				coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(payout)))
				if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(dBz), coins); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
				}
			}
		}
		pools.Treasury -= reward
	} else {
		// Dispute rejected by the fresh committee: bond to the Treasury (anti-grief; the coins stay in the module).
		pools.Treasury += job.DisputeBond
	}
	// The bond is disposed of in both branches (returned or confiscated), so it is cleared on the job, as
	// the timeout path does ("consumed, not replayable"). The real anti-replay is the `+resolved` guard at
	// the top; a non-zero field that survives its own disposal would misreport the state.
	job.DisputeBond = 0
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Fee-hold v2: consume the retention on this permissionless path too, otherwise HeldFee/HeldBurn stay
	// frozen after `+resolved`. Confirmed cheater: the client is refunded from the retention
	// (held+cut+burn) and Demand is reversed (as in slashCheatedPrimary). Vindicated primary: the
	// retention is released to the miner and the burn happens at finality (as in the timeout). No-op when
	// nothing is retained (hold_bps=0).
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		if optimisticCheated {
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				k.refundRetainedToClient(ctx, &job, p, &m)
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
			} else {
				// Primary vanished: refunding the client does not depend on the miner's survival
				// (nil = no Demand to reverse). Skipping the whole block left the retention frozen on
				// a `+resolved+adjudicated` job, an orphan debt.
				k.refundRetainedToClient(ctx, &job, p, nil)
			}
		} else {
			k.releaseHeld(ctx, &job) // nil-safe: miner gone, so the debt is kept and stays replayable
		}
	}
	// `+adjudicated`. This path used to mark a bare `+resolved`, so an adjudicated audit
	// could not be told apart from a legacy `+resolved`: the `resolved_by_adjudication` class of the
	// partition was invisible and the sum could not add up. The plurality of the decision base is recorded
	// too (distinct + voters + max per operator): LLM verdicts when the floor is reached (the branch the
	// code prefers), otherwise the re-commits.
	if verdictCanSlash {
		job.AuditDistinctVoters = uint64(verdictDistinctOps)
		job.AuditVoters = uint64(verdictVoters)
		job.AuditMaxOperatorVotes = uint64(verdictMaxOp)
	} else {
		maxFreshOp := 0
		for _, n := range freshOps {
			if n > maxFreshOp {
				maxFreshOp = n
			}
		}
		job.AuditDistinctVoters = uint64(len(freshOps))
		job.AuditVoters = uint64(len(fresh))
		job.AuditMaxOperatorVotes = uint64(maxFreshOp)
	}
	job.State = job.State + "+resolved+adjudicated"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	k.clearAuditCommittee(ctx, job.JobId) // audit adjudicated: purge the anchored seats
	return &types.MsgAdjudicateDisputeResponse{}, nil
}
