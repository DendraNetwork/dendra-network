package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// H6 — a job's beacon is managed ONLY by the protocol (open-job, from block randomness). The public
// create/update/delete-beacon messages are therefore NEUTRALISED: otherwise anyone could set or change
// a job's beacon to grind the committee. The beacon is thus immutable from the user side; only
// `open-job` writes it, through the keeper.
func (k msgServer) CreateBeacon(ctx context.Context, msg *types.MsgCreateBeacon) (*types.MsgCreateBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon managed by the protocol (open-job)")
}

func (k msgServer) UpdateBeacon(ctx context.Context, msg *types.MsgUpdateBeacon) (*types.MsgUpdateBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon immutable (managed by open-job)")
}

func (k msgServer) DeleteBeacon(ctx context.Context, msg *types.MsgDeleteBeacon) (*types.MsgDeleteBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon immutable (managed by open-job)")
}
