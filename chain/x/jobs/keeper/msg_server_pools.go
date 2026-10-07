package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// The scaffolded CRUD on `Pools` is NEUTRALIZED.
// The `Pools` singleton (Treasury/Team/Validators/MinerPaid accounting) is managed by the PROTOCOL:
// created lazily at settlement (`settle_semantic`/`finalize_job` call `Pools.Get` with a
// `types.Pools{}` fallback when absent), and ultimately written at genesis. A public CRUD would let
// the FIRST caller create, own and falsify the accounting (arbitrary counters) and deny service to it
// (`DeletePools` breaks `SettleJob` and `ClaimSubsidy`). It is therefore rejected, as in
// `msg_server_beacon.go`.
func (k msgServer) CreatePools(ctx context.Context, msg *types.MsgCreatePools) (*types.MsgCreatePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "pools are managed by the protocol (settlement / genesis)")
}

func (k msgServer) UpdatePools(ctx context.Context, msg *types.MsgUpdatePools) (*types.MsgUpdatePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "pools cannot be modified outside the protocol")
}

func (k msgServer) DeletePools(ctx context.Context, msg *types.MsgDeletePools) (*types.MsgDeletePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "deleting pools is forbidden (would deny settlement)")
}
