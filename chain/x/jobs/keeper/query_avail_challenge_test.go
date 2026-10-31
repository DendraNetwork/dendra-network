package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// GetAvailChallenge: off by default (avail_epoch_blocks=0, empty challenge); when on, it returns the
// challenge and the epoch.
func TestQueryGetAvailChallenge(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	// off by default
	resp, err := qs.GetAvailChallenge(f.ctx, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(0), resp.AvailEpochBlocks)
	require.Empty(t, resp.Challenge)

	// on: period 4, challenge rolled by the EndBlock of block 20 (runAvailabilityEpoch writes the
	// challenge and its roll height), height 20 gives epoch 5
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "deadbeef"))
	require.NoError(t, f.keeper.AvailChallengeHeight.Set(f.ctx, 20))

	ctx20 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	resp, err = qs.GetAvailChallenge(ctx20, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(4), resp.AvailEpochBlocks)
	require.Equal(t, "deadbeef", resp.Challenge)
	require.Equal(t, int64(5), resp.Epoch)

	// the same height inside block 20, before its EndBlock: the challenge in force was rolled at 16,
	// and the epoch is 4, the one block 20 closes and its proofs count for
	require.NoError(t, f.keeper.AvailChallengeHeight.Set(f.ctx, 16))
	resp, err = qs.GetAvailChallenge(ctx20, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, int64(4), resp.Epoch)
}
