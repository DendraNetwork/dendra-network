package keeper

import (
	"context"
	"fmt"
	"math/big"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// OpenJob escrows the fee and fixes the job's beacon. The client deposits `fee` into the "jobs"
// module account, so payment can no longer be withdrawn once miners start working, and the chain
// fixes an unpredictable beacon (block height and block time, both decided by consensus) for this
// job. The assigned committee derives from that beacon: the creator cannot shop for a jobId that
// yields a friendly committee.
func (k msgServer) OpenJob(ctx context.Context, msg *types.MsgOpenJob) (*types.MsgOpenJobResponse, error) {
	clientBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// "__" separates the two halves of a Commit key (jobId__minerId), so it is forbidden inside a
	// jobId: otherwise extracting the minerId by LastIndex splits at the wrong place and a vote or a
	// slash gets attributed to the wrong miner.
	if msg.JobId == "" || strings.Contains(msg.JobId, "__") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid jobId (must be non-empty and contain no '__')")
	}

	// Params are read ONCE and reused by the Nash guard and by the reveal delay below.
	//
	// Fail closed, and both consumers depend on it. Treating a failed read as a working mode skipped
	// the Nash guard — so an arbitrarily large fee passed in optimistic mode — and made
	// `committee_reveal_delay` fall back to 0, that is, back to the PREDICTABLE seed that deferred
	// reveal exists to close. Two anti-grinding guards disarmed by the same absence, without a single
	// error being raised. A params read that fails authorises nothing.
	params, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic,
			"module parameters unreadable: neither the Nash guard nor the reveal delay can be evaluated (fail-closed refusal)")
	}
	// Nash guard (ADR-025), optimistic mode only. Cheating must stay -EV: with an audit rate
	// s = audit_sample_bps and a slash P = slash_leak_bps * stake, the Nash inequality s*P > (1-s)*g
	// (with g approximately the fee) requires
	//     audit_sample_bps * slash_leak_bps * min_stake > (10000-audit_sample_bps) * 10000 * fee.
	// The reference is min_stake, the floor every primary is guaranteed to hold, which makes the
	// guard CONSERVATIVE. big.Int keeps the products (around 1e21) overflow-free. Fail-safe: an armed
	// optimistic mode with no audit at all (sample 0) yields lhs = 0 and every job is refused.
	// Dormant in redundant mode (the default), where peer comparison carries verification.
	if params.VerificationMode == 1 {
		// (10000 - s) computed underflow-safe: if s >= 10000 — Validate forbids it, but this is the
		// consensus path and deserves defence in depth — then 1-s = 0, so rhs = 0 and the guard is
		// always satisfied, which is correct: auditing everything makes Nash trivially hold.
		oneMinusS := uint64(0)
		if params.AuditSampleBps < 10000 {
			oneMinusS = 10000 - params.AuditSampleBps
		}
		lhs := new(big.Int).Mul(new(big.Int).SetUint64(params.AuditSampleBps), new(big.Int).SetUint64(params.SlashLeakBps))
		lhs.Mul(lhs, new(big.Int).SetUint64(params.MinStake))
		rhs := new(big.Int).Mul(new(big.Int).SetUint64(oneMinusS), big.NewInt(10000))
		rhs.Mul(rhs, new(big.Int).SetUint64(msg.Fee))
		if lhs.Cmp(rhs) <= 0 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
				"fee too high for the optimistic Nash equilibrium (ADR-025 §2.5): raise min_stake or lower the fee")
		}
	}

	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Fee)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(clientBz), types.ModuleName, coins); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
	}

	// Anti-grinding beacon. The committee derives from the beacon, so the creator must not be able to
	// pick a jobId that yields a friendly committee. Two regimes, selected by committee_reveal_delay:
	//   - delay == 0: the seed is fixed at open time from `height:time`. Both quantities are visible
	//     to the job creator, so the seed is PREDICTABLE. Devnet only.
	//   - delay  > 0: DEFERRED REVEAL. The seed stays EMPTY; the EndBlocker fixes it at H+delay from
	//     the AppHash of that future block, which is unknown when the job is opened, so grinding the
	//     jobId buys nothing. Commits are refused until the seed is revealed (see CreateCommit and
	//     assignedCommittee).
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	delay := params.CommitteeRevealDelay
	if delay == 0 {
		seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%d:%d", sdkCtx.BlockHeight(), sdkCtx.BlockTime().UnixNano()))
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: seed}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	} else {
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: ""}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		revealH := sdkCtx.BlockHeight() + int64(delay)
		if err := k.PendingReveal.Set(ctx, collections.Join(revealH, msg.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}

	// ADR-037 — the juror pool freeze is anchored HERE and nowhere else. By construction the anchor
	// predates every seed, whatever the seed's source (decentralised VRF, AppHash fallback, or any
	// future scheme), which is exactly what makes it safe without re-proving freshness at each draw.
	// An anchor taken later — at each draw — would admit more jurors and would be WRONG under the
	// fallback regime, where the seed is derivable one block ahead.
	// The accepted price: a job's eligible pool SHRINKS as the job ages (ADR-037 §4), which is why
	// Validate() enforces `miner_prune_blocks >= dispute_window`.
	if err := k.JobPoolFreezeHeight.Set(ctx, msg.JobId, uint64(sdkCtx.BlockHeight())); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	job := types.Job{JobId: msg.JobId, Client: msg.Creator, MinerId: "", Fee: msg.Fee, State: "open"}
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgOpenJobResponse{}, nil
}
