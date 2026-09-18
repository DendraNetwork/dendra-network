package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The scaffolded Pools CRUD is NEUTRALISED: the `Pools` accounting is owned by the settlement path (lazy
// creation) and by the genesis. Create/Update/DeletePools must ALWAYS reject; otherwise the first caller
// owns or forges the accounting and can deny service to the settlement.
func TestPoolsCRUDNeutralizedGO34(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.CreatePools(f.ctx, &types.MsgCreatePools{Creator: creator, Treasury: 10_000_000})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.UpdatePools(f.ctx, &types.MsgUpdatePools{Creator: creator, Treasury: 10_000_000})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.DeletePools(f.ctx, &types.MsgDeletePools{Creator: creator})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// No Pools singleton could be written by an arbitrary caller.
	has, err := f.keeper.Pools.Has(f.ctx)
	require.NoError(t, err)
	require.False(t, has)
}
