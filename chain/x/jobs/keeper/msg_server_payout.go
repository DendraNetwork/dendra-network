package keeper

import (
	"context"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// Payout pays the ASSIGNED honest majority out of the module escrow for THIS job.
//
// Three properties hold the payout together, and each closes a distinct way of draining funds:
//
//   - The amount is DERIVED from this job's escrow (job.Fee / number of winners), bounded by
//     Σ <= job.Fee. A caller-supplied amount would let one job drain the escrow of another.
//     msg.Amount is consequently unused and is scheduled for removal from the proto.
//   - The error from k.Job.Set is checked. An unwritten "+paid" flag leaves the job replayable.
//   - The escrow SURPLUS (job.Fee minus the total paid) is refunded to the client, so integer
//     division never strands the remainder in the module account.
//
// Replay protection: the job must be open and not already settled.
func (k msgServer) Payout(ctx context.Context, msg *types.MsgPayout) (*types.MsgPayoutResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not open (no escrow) -> open-job first")
	}
	if jobIsPaid(job.State) { // Unified settlement replay guard: paid or settled, both count.
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already paid")
	}

	prefix := msg.JobId + "__"
	commitByMiner := map[string]string{}
	tally := map[string]int{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				commitByMiner[mid] = c.ResultCommit
				tally[c.ResultCommit]++
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(commitByMiner) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no commit from an assigned miner for this job")
	}
	// The committee must be COMPLETE before any payout. Without this guard the first member to commit
	// and then relay this message collects 100 % of the escrow at the expense of its two peers,
	// before they have had a chance to submit at all. The same rule holds in `verify_semantic` and
	// `settle_semantic`; a rule enforced on some settlement paths and not the others is no rule.
	if len(commitByMiner) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "incomplete committee -> payout refused")
	}

	commits := make([]string, 0, len(tally))
	for c := range tally {
		commits = append(commits, c)
	}
	sort.Strings(commits)
	canonical, best := "", -1
	for _, c := range commits {
		if tally[c] > best {
			best, canonical = tally[c], c
		}
	}

	// Winners are the miners whose commit equals the canonical one; each share is DERIVED from the
	// escrow of this job, so the total can never exceed job.Fee.
	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	winners := make([]string, 0, len(mids))
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			winners = append(winners, mid)
		}
	}
	if len(winners) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no majority -> nothing to pay")
	}
	// The soft burn is a real burn. FeeBurnBps of the escrow is destroyed, then the REMAINDER is
	// split between the winners: the burn is an actual reduction of total supply, not a counter that
	// merely records one. Conservation: burn + paid + refunded surplus = job.Fee.
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	distributable := job.Fee - burnAmt
	per := distributable / uint64(len(winners))
	if per == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow too small for the number of winners")
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(per)))
	for _, mid := range winners {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
		if aErr != nil {
			continue
		}
		if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}

	// Real burn out of the module escrow: total supply goes down.
	if burnAmt > 0 {
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}

	// Refund the escrow surplus to the client; otherwise the integer-division remainder stays
	// trapped in the module account with no path out.
	if surplus := distributable - per*uint64(len(winners)); surplus > 0 {
		if cliBz, e := k.addressCodec.StringToBytes(job.Client); e == nil {
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(surplus)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), refund); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund surplus: "+err.Error())
			}
		}
	}

	// The Set error is checked: an unwritten flag would leave replay protection off.
	job.State = job.State + "+paid"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark +paid: "+err.Error())
	}
	return &types.MsgPayoutResponse{}, nil
}
