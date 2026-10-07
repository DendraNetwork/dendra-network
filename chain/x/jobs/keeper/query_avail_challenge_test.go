package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// GetAvailChallenge: OFF by default (avail_epoch_blocks=0, empty challenge); ON it returns the
// challenge and the epoch.
func TestQueryGetAvailChallenge(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	// OFF by default
	resp, err := qs.GetAvailChallenge(f.ctx, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(0), resp.AvailEpochBlocks)
	require.Empty(t, resp.Challenge)

	// ON: period 4, challenge written, height 20 -> epoch 5
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "deadbeef"))

	ctx20 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	resp, err = qs.GetAvailChallenge(ctx20, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(4), resp.AvailEpochBlocks)
	require.Equal(t, "deadbeef", resp.Challenge)
	require.Equal(t, int64(5), resp.Epoch)
}
