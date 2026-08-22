package keeper

import (
	"bytes"
	"context"
	"errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// RewardTraining — miners are paid for TRAINING (validated compute), not only for inference: that is
// the second pillar of Dendra. The reward is validated compute times a rate, credited to the
// `training_paid` pool. The rate is a fixed 10 per unit here and is meant to become governed. Proving
// that the compute happened (spot checks, redundancy) is out of scope for this handler, which only
// records the on-chain payout.
func (k msgServer) RewardTraining(ctx context.Context, msg *types.MsgRewardTraining) (*types.MsgRewardTrainingResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// A training reward is a PROTOCOL action, therefore reserved to the module AUTHORITY (governance).
	// Without this check, anyone could inflate `TrainingPaid` for any miner.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "reward-training is reserved to the authority (gov)")
	}

	// the miner must exist (a bond is required to contribute to training)
	if _, err := k.Miner.Get(ctx, msg.MinerId); err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// `Units` is bounded so that `reward = Units * ratePerUnit` cannot overflow uint64.
	const ratePerUnit = 10
	const maxUnits = 1_000_000_000_000_000 // 1e15 -> reward <= 1e16, far below MaxUint64
	if msg.Units > maxUnits {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "units out of bounds")
	}
	reward := msg.Units * ratePerUnit

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.TrainingPaid += reward
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgRewardTrainingResponse{}, nil
}
