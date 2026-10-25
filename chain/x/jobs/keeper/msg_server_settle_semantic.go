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

// SettleSemantic performs free-form semantic settlement out of escrow plus slashing of the outliers,
// in a single handler.
//
// Invariants this handler holds:
//
//	The similarity threshold is a governed constant (semanticThresholdBps) and the reward is
//	derived from job.Fee. Neither is taken from the message: a caller-supplied threshold or reward
//	would let the payer decide who counts as an outlier and how much they are paid.
//	The committee must be complete and the majority strict; without a strict majority there is
//	neither payment nor slash, since an ambiguous vote must not move money in either direction.
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
	if jobEscrowReturned(job.State) { // the expiry pass already returned this escrow: nothing left to pay from
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"job escrow already returned to the client (expired): paying now would draw on other clients' escrows")
	}
	// Anti-double-slash: a job whose k=3 committee has already been judged, by any of the three
	// redundant paths, must not go through SettleSemantic. Anchored commits are immutable and so still
	// divergent, so it would slash the same outliers again; every one of these handlers is
	// permissionless, so the penalty compounds.
	//
	// Naming only the "finalized" marker here would leave an edge open. Narrowing the check to keep the
	// documented flow FinalizeJob -> Payout open buys nothing, since Payout is a different handler that
	// this check never touches, while "+verified", written by VerifySemantic, would match neither this
	// check nor jobIsPaid: verify then settle would take a bond 1000 -> 200 -> 40, about 96 % of an
	// honest miner's bond for one piece of work, on two calls anyone can make
	// (compound_slash_verify_then_settle_test.go). `jobK3Adjudicated` is
	// the predicate the other two paths already use (msg_server_finalize_job.go,
	// msg_server_verify_semantic.go), and its own doc comment describes this compounding.
	// The exit stays open: Payout guards only on jobIsPaid, so a judged but unpaid job is still paid
	// once, as after FinalizeJob (checked by reading its guards).
	// Placed before the verification_mode branch, so it covers both modes.
	if jobK3Adjudicated(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "k=3 committee already judged; pay through Payout")
	}

	// Optimistic k=1 settlement. Dormant: reached only if governance has switched
	// verification_mode to 1. In mode 0 (the default) execution continues into the redundant k=3
	// path below, unchanged.
	if params.VerificationMode == 1 {
		return k.settleOptimistic(ctx, job, params)
	}

	// The governed work committee size, read once so the derivation and the
	// completeness test below compare the same number.
	wcs := k.workCommitteeSize(ctx)
	committee, err := k.assignedCommittee(ctx, msg.JobId, wcs)
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
	if len(vecs) < wcs { // a complete committee is required
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "incomplete committee -> settlement refused")
	}

	mids := make([]string, 0, len(vecs))
	for mid := range vecs {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	required := len(vecs)/2 + 1 // strict majority

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
	// Real soft burn (deflation): burn FeeBurnBps of the escrow, then split the rest among the winners.
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	distributable := job.Fee - burnAmt
	per := distributable / uint64(nWin) // reward derived from the escrow, so the sum stays <= distributable
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
		if neighbors[mid]+1 >= required { // in the majority: paid out of the escrow
			if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
				if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
				}
			}
		} else { // outlier: slashed
			amt := miner.Stake * params.SlashLeakBps / 10000
			miner.Stake -= amt
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
			pools.Treasury += amt
			// Record the slash (job, miner, amount) so that restitution is possible if this verdict is
			// successfully disputed: ResolveDispute credits that stake back when the dispute is upheld.
			// Additive (it alters neither the payment nor the slash) and persisted by the final
			// k.Job.Set.
			if amt > 0 {
				job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: mid, Amount: amt})
			}
		}
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Real burn out of the module escrow: total supply decreases.
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

// settleOptimistic is the optimistic k=1 settlement. It pays the single primary miner
// (drawn by assignedCommittee(.,1), stake-weighted) against its one commit, applies the soft burn, and
// marks the job `+paid+optimistic`. No k=3 majority is required: correctness is guaranteed after the
// fact by VRF-sampled audit plus the hard slash of AdjudicateDispute, instead of by redundancy. The
// `optimistic` marker reads as `paid` to jobIsPaid (replay protection) while staying identifiable by
// the audit. Dormant: unreachable while verification_mode == 0 (the default).
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
	// Proof of work done: the primary must have anchored a valid commit, otherwise there is nothing to
	// settle.
	pc, cErr := k.Commit.Get(ctx, job.JobId+"__"+primId)
	if cErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "primary commit missing -> optimistic settlement impossible")
	}
	if parseIntVec(pc.ResultCommit) == nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "primary commit invalid")
	}
	// Inference takes the same protocol cut as SettleJob. Without it, inference funded neither
	// treasury, validators nor team, nor the `Demand` counter, so the emission subsidy (work and
	// availability alike) never rewarded inference work at all. Order: soft burn, protocol cut (split
	// validators/team/treasury as in SettleJob), the rest to the primary.
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
	// Windowed retention: when hold_bps > 0, `held` stays in the module until audit finality, released
	// to the primary if vindicated or never audited, refunded to the client otherwise. Dormant at 0,
	// which means payment in full.
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
	if held > 0 { // the retention stays in the module; released or refunded at resolution (releaseHeld / refundRetainedToClient)
		if err := k.HeldFee.Set(ctx, job.JobId, held); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		// Date the retention at its birth. HeldSince is the strict twin of HeldFee: written here,
		// removed wherever HeldFee is removed. "Retained is not lost" is only verifiable if the age of
		// the oldest retention is measurable (held_oldest_age, HeldSummary query).
		if err := k.HeldSince.Set(ctx, job.JobId, uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// The cut is retained per job until finality, then paid.
	// Crediting it straight into `Pools.Validators/Team/Treasury`, three counters nothing ever pays
	// out, would leave the validators' and the team's shares of every job in the module for good while
	// the public split announces them as paid. They are held per job instead, like the miner's retention
	// and the deferred burn, and `releaseHeld` pays them on the same branch that burns: the validators'
	// share to fee_collector, the team's to `team_address`, the treasury's into `Pools.Treasury`. A
	// refund returns them to the client instead, exactly this job's shares and nothing else.
	if err := k.retainCut(ctx, job.JobId, validators, team, treasury); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// The subsidy right is born when the miner is paid, not when the job is settled.
	// Crediting `Demand += treasury+team` here would let the subsidy it unlocks be claimed during the
	// audit window, before anyone knows whether the job will stand, and a refund would then reverse the
	// Demand and never the subsidy already paid out. The right is recorded here instead and credited by
	// `releaseHeld` at finality, so a refund has nothing to undo. Self-dealing (client ==
	// operator) still earns nothing.
	if job.Client != miner.Operator && treasury+team > 0 {
		if err := k.HeldDemand.Set(ctx, job.JobId, treasury+team); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// Burn deferred to finality: with retention enabled (hold_bps > 0) nothing is burned at settlement.
	// BurnCoins is irreversible, so a clawed-back job would either cost the honest client its burn share
	// or have to be taken out of the bond, and neither is acceptable. The burn is retained (HeldBurn)
	// and executed at finality (release or vindication, in releaseHeld), which makes it a tax on
	// successful work; on clawback or slash it is returned to the client. hold_bps = 0 means the burn
	// happens immediately.
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
	// Probation: increment the primary's optimistic-job counter, which forces 100 % audit on
	// its first N jobs; dormant when audit_probation_jobs == 0. A missing entry reads as 0.
	cnt, _ := k.MinerOptimisticCount.Get(ctx, primId)
	if err := k.MinerOptimisticCount.Set(ctx, primId, cnt+1); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Schedule the audit draw for the next block. The draw seed (the AppHash or the decentralized VRF
	// seed of the audit block) comes after the commit, so a miner cannot know while answering whether
	// it will be audited, and there is nothing to grind against. The EndBlocker decides (no-op when
	// audit_sample_bps == 0).
	auditH := sdk.UnwrapSDKContext(ctx).BlockHeight() + 1
	if err := k.PendingAudit.Set(ctx, collections.Join(auditH, job.JobId)); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgSettleSemanticResponse{}, nil
}
