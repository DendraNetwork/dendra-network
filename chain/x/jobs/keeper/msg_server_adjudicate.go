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

// AdjudicateDispute closes a dispute PERMISSIONLESSLY / TRUSTLESSLY, in place of the interim authority of
// ResolveDispute. Governance is not needed: the chain re-reads the RE-COMMITS of a FRESH committee and
// decides on its own.
//
// Mechanism (cf. docs/DISPUTE-FRAUDPROOF.md §8):
//   - After the `dispute_window`, a FRESH committee has re-executed the job off-chain and anchored its
//     results under the key "<jobId>__redo__<minerId>" (through the existing CreateCommit, signed by each
//     operator).
//   - Only REGISTERED miners count (real stake = anti-sybil), and only those OUTSIDE the original committee
//     (re-derivable from the beacon -> independence / anti-collusion).
//   - The VERDICT is STAKE-WEIGHTED (splitting a stake does not change influence): a slashed miner whose
//     ORIGINAL commit gathers a STRICT MAJORITY of the fresh stake was in fact CORRECT -> its slash is
//     REVERSED (restitution from the Treasury) and the disputer is refunded and rewarded. Otherwise the
//     disputer's bond goes to the Treasury (anti-grief).
//
// Stated honestly: trust moves from "honest majority of the committee" to ">=1 honest bonded party plus a
// non-colluding fresh committee"; if the fresh committee colludes TOO, the majority assumption is all that
// remains (at scale, two independent colluding committees are far harder to obtain). DORMANT by default
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

	// ORIGINAL committee (re-derivable from the beacon) -> EXCLUDED from the fresh tally.
	orig, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// ADR-033 — the voting set of the FRESH committee must be ANCHORED, just like the verdict tally
	// (ADR-032). Without this read, "being a registered miner outside the original committee" was enough
	// to vote: three identities at `min_stake` held 100% of `totalRedoStake` and could just as easily get
	// an honest miner slashed as get a real cheater's slash refunded from the Treasury.
	// No anchor = fully fail closed on this path (the audit timeout remains the way out).
	// The AUTHORS of the contested commits, which is not the set that was DRAWN. Anchoring already
	// refuses them a seat; this second reading catches the one case anchoring cannot see -- a juror
	// already anchored that afterwards writes its own commit on the disputed job, which nothing forbids.
	authors, aErr := k.commitAuthorsOf(ctx, msg.JobId)
	if aErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, aErr.Error())
	}

	allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, msg.JobId)

	// RE-COMMITS of the fresh committee (key "<jobId>__redo__<minerId>"): miners DRAWN AND ANCHORED, outside the original committee, stake-weighted.
	redoPrefix := msg.JobId + "__redo__"
	type freshVote struct {
		vec   []int64
		stake uint64
	}
	fresh := []freshVote{}
	var totalRedoStake uint64
	freshOps := map[string]int{} // ADR-034 partition: votes per operator behind the counted re-commits
	if err := k.Commit.Walk(ctx, commitRange(redoPrefix), func(key string, c types.Commit) (bool, error) {
		if !redoAnchored {
			return true, nil // no committee drawn -> no admissible vote (fail closed)
		}
		if !strings.HasPrefix(key, redoPrefix) {
			return false, nil
		}
		mid := strings.TrimPrefix(key, redoPrefix)
		if !allowedRedo[mid] {
			return false, nil // ADR-033: NOT DRAWN -> does not vote (a sybil can no longer invite itself)
		}
		if orig[mid] || authors[mid] {
			return false, nil // excludes the drawn committee AND the authors of the contested result
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // must be a registered miner (real stake = anti-sybil)
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
	// ADR-026 (J3) + ADR-028 — VERDICTS of the fresh committee (key "<jobId>__verdict__<minerId>", LLM-as-judge).
	// A participation FLOOR is required (>= effectiveSlashFloor(p) distinct voters, stake-weighted): a slash is
	// NEVER pronounced below that floor (anti false-positive / cross-GPU non-determinism / anti-sybil). Below the
	// floor, with neither enough re-commits nor enough verdicts, the adjudication does NOT conclude here (the
	// audit timeout decides instead: clawback if the primary stays silent).
	verdictCheated, verdictValid, verdictVoters, verdictInvalid, verdictSeats, verdictDistinctOps, verdictMaxOp := k.auditVerdictTally(ctx, msg.JobId, job.MinerId)
	verdictCanSlash := verdictVoters >= effectiveSlashFloor(p) // ADR-028 v2: EFFECTIVE participation floor (governed when audit_min_quorum>0, else the v1 fallback) for a hard slash

	// THE RE-COMMIT DENOMINATOR IS RELATIVE TO THE ANCHORED SEATS, NOT TO 3.
	//
	// A fixed gate of `len(fresh) >= CommitteeSize` = 3 is wrong here, because it ignores the number of
	// seats actually DRAWN (15). Three responders out of fifteen — 20% of the seats — would then pronounce
	// a hard slash OR a restitution from the Treasury, with the majority computed over `totalRedoStake`,
	// the stake of the responders alone. That is the denominator mistake the amended ADR-032 closes on the
	// verdict side: a correct threshold whose base changed underneath it. The same rule applies here — participation
	// quorum at ceil(2/3 of the ANCHORED seats) — so that the decision requires a supermajority of the
	// convened committee, not a handful of volunteers. Absolute floor at CommitteeSize so it never drops
	// below 3.
	// ⛔ A SEAT WHOSE HOLDER MAY NOT VOTE IS NOT A SEAT. This counted every anchored seat while the
	// filter above drops the excluded ones, so widening the exclusion shrank the NUMERATOR and left the
	// DENOMINATOR untouched -- each exclusion silently raising the bar the remaining jurors must clear,
	// and enough of them making the quorum unreachable on a job that has already been paid for.
	// Excluded seats therefore leave both sides of the ratio at once.
	redoSeats := 0
	for mid := range allowedRedo {
		if orig[mid] || authors[mid] {
			continue
		}
		redoSeats++
	}
	redoQuorum := (2*redoSeats + 2) / 3 // ceil(2/3 x anchored seats), integer arithmetic
	if redoQuorum < CommitteeSize {
		redoQuorum = CommitteeSize
	}
	if len(fresh) < redoQuorum && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "fresh committee below quorum (re-commits < 2/3 of the anchored seats and verdicts < participation floor) -> the audit timeout will decide")
	}
	if totalRedoStake == 0 && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "fresh committee carries no stake")
	}

	// PARTICIPATION IS MEASURED ON SUCCESS TOO, OR THE SAMPLE IS CENSORED.
	//
	// The timeout emits `redo_participation` only for disputes that did NOT reach quorum — that is the very
	// definition of a timeout. Measuring there alone observes only the left tail: every measurement would
	// show `responders < quorum`, not because participation is poor but because that is the sample being
	// collected. Calibration would then be done on censored data. Here the gate has been cleared: the
	// adjudication IS going to commit, so the event survives (no rollback). The two emission points together
	// give the full distribution. `resolved_by` separates the two populations at analysis time.
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

	// STAKE-WEIGHTED VERDICT: a slashed miner is VINDICATED if its ORIGINAL commit gathers a STRICT MAJORITY
	// of the fresh stake (totalRedoStake <= the 1e13 udndr supply, so agree*2 cannot overflow uint64).
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
			continue // no strict majority of the fresh stake -> not vindicated
		}
		// vindicated -> RESTITUTION (accounting only: the coins have been in the module since the slash), bounded.
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

	// ADR-025 (M4) — OPTIMISTIC AUDIT RESOLUTION. The k=1 settlement PAID a primary WITHOUT slashing it (no
	// SlashRecord). If that optimistic job is challenged (VRF audit M3, or a human dispute), the primary's
	// ORIGINAL commit is compared to the STAKE MAJORITY of the fresh committee. Divergence -> the primary was
	// paid for a false result -> HARD SLASH (SlashLeakBps) + SlashRecord (refundable if it later re-challenges
	// successfully) + VALID dispute. Agreement -> the primary is vindicated and nothing moves. This is what
	// makes cheating -EV (Nash, ADR-025 §2.5).
	// fee-hold v2: the optimistic verdict is memorised so that the retention is CONSUMED at the end of the
	// resolution; otherwise HeldFee/HeldBurn stay frozen on this permissionless path.
	optimisticCheated := false
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		cheated := false
		if verdictCanSlash {
			// The decision rule in one sentence: slash IFF participation >= the effective floor AND
			// "invalid" >= ceil(2/3 of the anchored seats) AND a STAKE majority is invalid. The bar is
			// relative to the ANCHORED SEATS in BOTH branches. Detail and cost: `auditRelativeBar`.
			cheated = auditSlashDecision(p, verdictCheated, verdictVoters, verdictInvalid, verdictSeats)
			// The twin of the slash bar is the vindication bar, and it must be just as relative. This path
			// used to vindicate at the ABSOLUTE floor: `verdictCanSlash` (4 voters) was enough to enter
			// here, and "not cheating" ended in `releaseHeld` below — that is, a vindication pronounced by
			// roughly 27% of the stake, the exact hole the timeout path closes. Now: neither slash NOR
			// vindication at the relative bar => the adjudication DOES NOT CONCLUDE (error, no funds move,
			// the audit stays open, the timeout decides). A permissionless tx that refuses is the exact
			// equivalent of the EndBlocker deferring.
			if !cheated && !auditVindicateDecision(p, verdictValid, verdictVoters, verdictVoters-verdictInvalid, verdictSeats) {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
					"verdicts below the relative bar (neither slash nor vindication at 2/3 of the anchored seats) -> the adjudication does not conclude; the audit timeout will defer")
			}
		} else {
			// Fallback (M4): cosine comparison of the primary's ORIGINAL commit against the fresh stake
			// majority (reachable only with >= CommitteeSize re-commits, guaranteed by the gate above).
			if oc, e := k.Commit.Get(ctx, msg.JobId+"__"+job.MinerId); e == nil {
				if ov := parseIntVec(oc.ResultCommit); ov != nil {
					var agree uint64
					for _, fv := range fresh {
						if cosineGE(ov, fv.vec, semanticThresholdBps) {
							agree += fv.stake
						}
					}
					// ADR-033 — the `totalRedoStake > 0` guard is NOT cosmetic: without it `0*2 <= 0`
					// is TRUE, so an EMPTY electorate (committee not anchored, or drawn but silent)
					// would pronounce "the primary cheated". Silence does not convict; the audit
					// timeout is what handles the absence of an answer.
					//
					// A TIE NEITHER CONVICTS NOR VINDICATES. Written as the non-strict
					// `agree*2 <= totalRedoStake`, this test sends an EXACT 50/50 to a HARD SLASH
					// (SlashLeakBps), while the rule on this line is "disagrees with the MAJORITY" —
					// and a tie is not a majority. The twin of this computation, `auditVerdictTally`,
					// decides STRICTLY on BOTH sides (`invalidStake*2 > totalStake` for cheated, `<`
					// for valid) and leaves a tie UNDECIDED: two tables of the same shape must not
					// carry two different rules. This is not a theoretical edge: the redo quorum is
					// satisfied by responders of EQUAL stake (min_stake), so 5 against 5 over 10 seats
					// is an exact split. An adversary holding half the fresh stake — not a majority,
					// HALF — could get an honest miner slashed by 80%.
					// The rule is therefore STRICT on both sides, and a tie REFUSES the adjudication,
					// like the verdict branch above. No funds move, the audit stays open, the timeout
					// decides — a permissionless tx that refuses is the exact equivalent of the
					// EndBlocker deferring.
					//
					// THE PRICE OF THIS RULE, STATED HERE BECAUSE THIS IS WHERE IT WILL BE READ.
					// It MOVES the harm, it does not remove it. An adversary holding EXACTLY half the
					// fresh stake can FORCE the tie and turn the adjudication into a deferral — that
					// is, buy TIME, at will, for as long as it holds parity. This is the trade-off
					// assumed since ADR-025 (protect the honest party, accept some evasion), and it
					// remains the right one: a deferral pays no one and erases nothing, whereas a
					// false slash is irreversible in reputation. But a stalling lever handed to a
					// capitalised adversary MUST BE DECLARED — otherwise the next reader will believe
					// they found a bug and will "fix" it in the direction that reopens the false
					// slash. If stalling is ever observed, the answer is to BOUND the number of
					// deferrals (ADR-034), not to make a tie convicting.
					if totalRedoStake > 0 {
						switch {
						case agree*2 < totalRedoStake:
							cheated = true // STRICT majority disagreement in fresh stake
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
			// `upheld` FOLLOWS THE VERDICT, NOT THE SURVIVAL OF THE MINER. The primary may be absent:
			// a miner that exited or was pruned before the exit guard existed can outlive its job.
			// Setting `upheld = true` inside the successful `Miner.Get` block meant that a cheat
			// CONFIRMED at the bar with a vanished primary left `upheld=false`, so the bond of a
			// disputer who was RIGHT went to the Treasury as "anti-grief". Two decisions, one basis
			// each: the bond is disposed of according to what the committee SAID, the slash according
			// to what is left to slash. On this path, upheld == optimisticCheated BY CONSTRUCTION.
			upheld = true
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				amt := m.Stake * p.SlashLeakBps / 10000
				m.Stake -= amt
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
				pools.Treasury += amt
				if amt > 0 {
					job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
				}
			}
		}
	}

	if upheld {
		// VALID dispute: refund the bond and reward the disputer (whistleblower), bounded by the remaining Treasury.
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
		// dispute REJECTED by the fresh committee: bond -> Treasury (anti-grief; the coins stay in the module).
		pools.Treasury += job.DisputeBond
	}
	// The bond is DISPOSED OF in both branches (returned or confiscated), so it is cleared on the job, as the
	// timeout path does ("consumed, not replayable"). The real anti-replay is the `+resolved` guard at the
	// top; a non-zero field that survives its own disposal is state that LIES.
	job.DisputeBond = 0
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// fee-hold v2 — CONSUME the retention on THIS (permissionless) path too, otherwise HeldFee/HeldBurn stay
	// FROZEN after `+resolved`. Confirmed cheater -> the client is refunded from the retention (held+cut+burn)
	// and Demand is reversed (as in slashCheatedPrimary); vindicated primary -> the retention is released to
	// the miner and the burn is burned at finality (as in the timeout). No-op when nothing is retained
	// (hold_bps=0).
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		if optimisticCheated {
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				k.refundRetainedToClient(ctx, &job, p, &m)
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
			} else {
				// Primary VANISHED: refunding the CLIENT does not depend on the miner's survival
				// (nil = no Demand to reverse). Skipping the whole block left the retention FROZEN
				// on a `+resolved+adjudicated` job — an orphan debt.
				k.refundRetainedToClient(ctx, &job, p, nil)
			}
		} else {
			k.releaseHeld(ctx, &job) // nil-safe: miner gone -> the debt is KEPT and stays replayable
		}
	}
	// ADR-034 partition — `+adjudicated`: this path used to mark a bare `+resolved`, so an adjudicated audit
	// was indistinguishable, for The Proof, from a legacy `+resolved`: the `resolved_by_adjudication` class
	// of the partition was invisible and the sum could not add up. The PLURALITY of the decision base is
	// recorded too (distinct + voters + max per operator): LLM verdicts when the floor is reached (the branch
	// the code prefers), otherwise the re-commits.
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
	k.clearAuditCommittee(ctx, job.JobId) // audit adjudicated -> purge the anchored seats
	return &types.MsgAdjudicateDisputeResponse{}, nil
}
