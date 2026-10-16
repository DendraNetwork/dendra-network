package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// The generated CRUD scaffold is neutralised here.
// Only the protocol may create, modify or delete a job: `OpenJob` is the only creation path (it
// escrows the fee for real) and settlement owns the state afterwards. The scaffolded CRUD handlers
// wrote the same `Job{Fee,State}` with no escrow and no guard, so `CreateJob` reopened the cross-job
// drain, `UpdateJob` reset the anti-replay markers, and `DeleteJob` stranded the escrow. All three
// are rejected.
func (k msgServer) CreateJob(ctx context.Context, msg *types.MsgCreateJob) (*types.MsgCreateJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "a job is created only through open-job (which escrows the fee)")
}

func (k msgServer) UpdateJob(ctx context.Context, msg *types.MsgUpdateJob) (*types.MsgUpdateJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "a job is immutable from the user side (state is owned by settlement)")
}

func (k msgServer) DeleteJob(ctx context.Context, msg *types.MsgDeleteJob) (*types.MsgDeleteJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "job deletion is forbidden (escrow and anti-replay depend on the record)")
}
