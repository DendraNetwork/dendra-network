package keeper

import (
	"context"
	"errors"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// SettleSemantic — free-form SEMANTIC settlement out of ESCROW plus slashing of the outliers, in a
// single handler.
//
// Invariants this handler holds:
//
//	The similarity threshold is a governed constant (semanticThresholdBps) and the reward is DERIVED
//	from job.Fee. Neither is taken from the message: a caller-supplied threshold or reward would let
//	the payer decide who counts as an outlier and how much they are paid.
//	The committee must be COMPLETE and the majority STRICT; without a strict majority there is neither
//	payment nor slash — an ambiguous vote must not move money in either direction.
//	Every Set error is checked, and any escrow surplus is refunded to the client.
//	cosineGE/parseIntVec are shared with verify_semantic (big.Int arithmetic, bounded inputs).
//	Replay protection: the job must be open and not already paid.
func (k msgServer) SettleSemantic(ctx context.Context, msg *types.MsgSettleSemantic) (*types.MsgSettleSemanticResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not open (no escrow)")
	}
	if jobIsPaid(job.State) { // unified settlement replay guard (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already settled")
	}
	// ANTI-DOUBLE-SLASH: a job that is already FINALIZED — FinalizeJob has returned the k=3 verdict and
	// slashed the divergent miners — must not go through SettleSemantic. Anchored commits are IMMUTABLE
	// and therefore still divergent, so it would re-slash the SAME outliers; both handlers are
	// permissionless, so the penalty would compound. The guard is TARGETED at this handler alone: the
	// documented flow FinalizeJob -> Payout (verdict, then payment; Payout does not slash) stays open.
	// Placed before the verification_mode branch, so it covers both modes.
	if strings.Contains(job.State, "finalized") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "verdict already returned (FinalizeJob); pay through Payout")
	}

	// OPTIMISTIC k=1 SETTLEMENT (ADR-025). DORMANT: reached ONLY if governance has switched
	// verification_mode to 1. In mode 0 (the default) execution continues straight into the redundant
	// k=3 path below, strictly unchanged.
	if params.VerificationMode == 1 {
		return k.settleOptimistic(ctx, job, params)
	}

	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	vecs := map[string][]int64{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				if v := parseIntVec(c.ResultCommit); v != nil {
					vecs[mid] = v
				}
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(vecs) < CommitteeSize { // a complete committee is required
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "incomplete committee -> settlement refused")
	}

	mids := make([]string, 0, len(vecs))
	for mid := range vecs {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	required := len(vecs)/2 + 1 // STRICT majority

	neighbors := map[string]int{}
	nWin := 0
	for _, mid := range mids {
		c := 0
		for _, other := range mids {
			if other != mid && cosineGE(vecs[mid], vecs[other], semanticThresholdBps) {
				c++
			}
		}
		neighbors[mid] = c
		if c+1 >= required {
			nWin++
		}
	}
	if nWin == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no strict semantic majority")
	}
	// Real soft BURN (deflation): burn FeeBurnBps of the escrow, then split the REST among the winners.
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	distributable := job.Fee - burnAmt
	per := distributable / uint64(nWin) // reward DERIVED from the escrow, so Σ <= distributable
	if per == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow too small for the number of winners")
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(per)))

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	for _, mid := range mids {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		if neighbors[mid]+1 >= required { // in the majority -> PAID out of the escrow
			if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
				if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
				}
			}
		} else { // OUTLIER -> slashed
			amt := miner.Stake * params.SlashLeakBps / 10000
			miner.Stake -= amt
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
			pools.Treasury += amt
			// RECORD the slash (job, miner, amount) so that RESTITUTION is possible if this verdict is
			// successfully disputed: ResolveDispute re-credits that stake when the dispute is upheld.
			// Additive — it alters neither the payment nor the slash — and persisted by the final
			// k.Job.Set.
			if amt > 0 {
				job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: mid, Amount: amt})
			}
		}
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Real BURN out of the module escrow: total supply decreases.
	if burnAmt > 0 {
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}
	// Any escrow surplus goes back to the client.
	if surplus := distributable - per*uint64(nWin); surplus > 0 {
		if cliBz, e := k.addressCodec.StringToBytes(job.Client); e == nil {
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(surplus)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), refund); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund surplus: "+err.Error())
			}
		}
	}
	// Mark as settled; the Set error is checked.
	job.State = job.State + "+paid+finalized"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark the job: "+err.Error())
	}
	return &types.MsgSettleSemanticResponse{}, nil
}

// settleOptimistic (ADR-025) — OPTIMISTIC k=1 SETTLEMENT. Pays the SINGLE PRIMARY miner (drawn by
// assignedCommittee(.,1), stake-weighted) against its one commit, applies the soft burn, and marks the
// job `+paid+optimistic`. No k=3 majority is required: correctness is no longer guaranteed by
// REDUNDANCY but AFTER THE FACT, by VRF-sampled audit plus the hard slash of AdjudicateDispute. The
// `optimistic` marker reads as `paid` to jobIsPaid (replay protection) while staying identifiable by
// the audit. DORMANT: unreachable while verification_mode == 0 (the default).
func (k msgServer) settleOptimistic(ctx context.Context, job types.Job, params types.Params) (*types.MsgSettleSemanticResponse, error) {
	prim, err := k.assignedCommittee(ctx, job.JobId, 1)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	primId := ""
	for id := range prim {
		primId = id // assignedCommittee(.,1) yields exactly one element
	}
	if primId == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no primary assigned")
	}
	// Proof of work done: the primary MUST have anchored a valid commit, otherwise there is nothing to
	// settle.
	pc, cErr := k.Commit.Get(ctx, job.JobId+"__"+primId)
	if cErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "primary commit missing -> optimistic settlement impossible")
	}
	if parseIntVec(pc.ResultCommit) == nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "primary commit invalid")
	}
	// ADR-017: inference takes the SAME protocol cut as SettleJob. Without it, inference funded neither
	// treasury/validators/team nor the `Demand` counter, so the emission subsidy — work AND availability
	// alike — never rewarded inference work at all. Order: soft burn, protocol cut (split
	// validators/team/treasury as in SettleJob), the REST to the primary.
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	cut := job.Fee * params.ProtocolFeeBps / 10000
	if job.Fee < burnAmt+cut {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow too small (burn+cut)")
	}
	validators := cut * params.ValidatorRewardBps / 10000
	team := cut * params.TeamFeeBps / 10000
	treasury := cut - validators - team
	minerNet := job.Fee - burnAmt - cut
	if minerNet == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow too small")
	}
	// Windowed retention: when hold_bps > 0, `held` is RETAINED in the module until audit finality —
	// released to the primary if vindicated or never audited, refunded to the client otherwise. Dormant
	// at 0, which means payment in full.
	held := minerNet * params.HoldBps / 10000
	immediate := minerNet - held
	miner, mErr := k.Miner.Get(ctx, primId)
	if mErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "primary not registered")
	}
	if immediate > 0 {
		if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
			coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(immediate)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
			}
		}
	}
	if held > 0 { // the retention STAYS in the module; released or refunded at resolution (releaseHeld / refundRetainedToClient)
		if err := k.HeldFee.Set(ctx, job.JobId, held); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		// DATE the retention at its birth. HeldSince is the strict twin of HeldFee: written here,
		// removed wherever HeldFee is removed. "Retained is not lost" is only verifiable if the AGE of
		// the oldest retention is measurable (held_oldest_age, HeldSummary query).
		if err := k.HeldSince.Set(ctx, job.JobId, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// The cut STAYS in the module escrow and is accounted in Pools (validators/team/treasury), as in
	// SettleJob.
	pools, pErr := k.Pools.Get(ctx)
	if pErr != nil {
		if errors.Is(pErr, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, pErr.Error())
		}
	}
	pools.Validators += validators
	pools.Team += team
	pools.Treasury += treasury
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// `Demand += treasury+team` is the signal of REAL demand served, credited at settlement, EXCEPT on
	// self-dealing (client == operator), as in SettleJob. A primary that is audited and then slashed has
	// this Demand REVERSED by slashCheatedPrimary, so farming does not pay: the pre-resolution window is
	// negative expected value, since an 80 % slash dwarfs the reachable subsidy. Strict finality —
	// crediting only on non-audit or vindication — is a later refinement.
	if job.Client != miner.Operator {
		miner.Demand += treasury + team
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// Burn DEFERRED to finality: with retention enabled (hold_bps > 0) nothing is burned at settlement.
	// BurnCoins is IRREVERSIBLE, so a clawed-back job would either cost the honest client its burn share
	// or have to be taken out of the bond, and neither is acceptable. The burn is RETAINED (HeldBurn)
	// and burned at finality (release or vindication, in releaseHeld), which makes it a tax on
	// SUCCESSFUL work; on clawback or slash it is RETURNED to the client. hold_bps = 0 -> burn
	// immediately.
	if burnAmt > 0 {
		if params.HoldBps > 0 {
			if err := k.HeldBurn.Set(ctx, job.JobId, burnAmt); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "held_burn: "+err.Error())
			}
		} else if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}
	job.MinerId = primId
	job.State = job.State + "+paid+optimistic"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark the job: "+err.Error())
	}
	// Probation (ADR-025): increment the primary's optimistic-job counter, which forces 100 % audit on
	// its first N jobs; dormant when audit_probation_jobs == 0. A missing entry reads as 0.
	cnt, _ := k.MinerOptimisticCount.Get(ctx, primId)
	if err := k.MinerOptimisticCount.Set(ctx, primId, cnt+1); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Schedule the AUDIT DRAW for the NEXT block. The draw seed — the AppHash or the decentralized VRF
	// seed of the audit block — is POSTERIOR to the commit, so a miner cannot know while answering
	// whether it will be audited: there is nothing to grind against. The EndBlocker decides (no-op when
	// audit_sample_bps == 0).
	auditH := sdk.UnwrapSDKContext(ctx).BlockHeight() + 1
	if err := k.PendingAudit.Set(ctx, collections.Join(auditH, job.JobId)); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgSettleSemanticResponse{}, nil
}
