package keeper

import (
	"context"
	"errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

func (k msgServer) ClaimSubsidy(ctx context.Context, msg *types.MsgClaimSubsidy) (*types.MsgClaimSubsidyResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}

	miner, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Only the miner's OPERATOR may claim its subsidy. Otherwise a third party can exhaust a competitor's
	// quota (driving SubsidyClaimed to the cap) while that competitor receives nothing: pure griefing.
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "only the miner operator may claim its subsidy")
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// The subsidy is GATED by non-recoverable demand (ADR-017): cap = work_gate * demand.
	subsidyCap := miner.Demand * params.WorkGateBps / 10000
	if subsidyCap <= miner.SubsidyClaimed {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "nothing to claim (cap reached or demand is zero)")
	}
	pay := subsidyCap - miner.SubsidyClaimed

	// Pay in REAL coins from the emission WorkPool rather than incrementing a counter. PayWork returns the
	// amount ACTUALLY paid (less than `pay` when the pool is short), so SubsidyClaimed and MinerPaid are
	// credited only with what was paid; the remainder stays claimable in a later epoch.
	opBz, err := k.addressCodec.StringToBytes(miner.Operator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid operator address")
	}
	paid, err := k.emissionKeeper.PayWork(ctx, sdk.AccAddress(opBz), pay)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if paid == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "emission work pool is empty -- retry after the next epoch")
	}
	miner.SubsidyClaimed += paid
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.MinerPaid += paid
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgClaimSubsidyResponse{}, nil
}
