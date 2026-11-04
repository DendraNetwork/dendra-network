package keeper_test

import (
	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestAvailabilityBoundaryRequiresNewEpochChallenge(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 100
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	op := ssAddr20(t, f, "boundary-review")
	require.NoError(t, gvSetMiner(f, f.ctx, "mBoundary", types.Miner{MinerId: "mBoundary", Operator: op, Stake: 1000}))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "old-challenge"))
	srv := keeper.NewMsgServerImpl(f.keeper)
	req := &types.MsgProveAvailability{Creator: op, MinerId: "mBoundary", Challenge: "old-challenge"}
	old := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(199)
	_, err := srv.ProveAvailability(old, req)
	require.NoError(t, err)
	boundary := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(200).WithHeaderInfo(header.Info{Height: 200, AppHash: []byte("new-challenge")})
	_, err = srv.ProveAvailability(boundary, req)
	require.NoError(t, err, "the last block can still answer the old epoch")
	present, err := f.keeper.Available.Has(boundary, collections.Join(int64(2), "mBoundary"))
	require.NoError(t, err)
	require.False(t, present)
	require.NoError(t, f.keeper.EndBlock(boundary))
	challenge, err := f.keeper.AvailChallenge.Get(boundary)
	require.NoError(t, err)
	require.NotEqual(t, "old-challenge", challenge)
	next := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(201)
	req.Challenge = challenge
	_, err = srv.ProveAvailability(next, req)
	require.NoError(t, err)
	present, err = f.keeper.Available.Has(next, collections.Join(int64(2), "mBoundary"))
	require.NoError(t, err)
	require.True(t, present)
}

func TestBoundaryProofUsesPreviousEpochDeadline(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 100
	p.AvailDeadlineBlocks = 2
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	op := ssAddr20(t, f, "boundary-deadline")
	require.NoError(t, gvSetMiner(f, f.ctx, "mDeadline", types.Miner{MinerId: "mDeadline", Operator: op, Stake: 1000}))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "old-challenge"))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(200)
	_, err := keeper.NewMsgServerImpl(f.keeper).ProveAvailability(ctx, &types.MsgProveAvailability{Creator: op, MinerId: "mDeadline", Challenge: "old-challenge"})
	require.Error(t, err, "the previous challenge's deadline has passed")
	for _, epoch := range []int64{1, 2} {
		present, e := f.keeper.Available.Has(ctx, collections.Join(epoch, "mDeadline"))
		require.NoError(t, e)
		require.False(t, present)
	}
}

func TestOneBlockEpochCanProveItsOwnChallenge(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	op := ssAddr20(t, f, "one-block-epoch")
	require.NoError(t, gvSetMiner(f, f.ctx, "mOneBlock", types.Miner{MinerId: "mOneBlock", Operator: op, Stake: 1000}))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "epoch-one"))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(2)
	_, err := keeper.NewMsgServerImpl(f.keeper).ProveAvailability(ctx, &types.MsgProveAvailability{Creator: op, MinerId: "mOneBlock", Challenge: "epoch-one"})
	require.NoError(t, err)
	present, err := f.keeper.Available.Has(ctx, collections.Join(int64(1), "mOneBlock"))
	require.NoError(t, err)
	require.True(t, present)
	present, err = f.keeper.Available.Has(ctx, collections.Join(int64(2), "mOneBlock"))
	require.NoError(t, err)
	require.False(t, present)
}
