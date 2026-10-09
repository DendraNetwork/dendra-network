package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Deferred committee reveal (anti-grinding), driven by the committee_reveal_delay parameter.

// delay == 0 (the default): the seed is fixed AT open time, so a commit is accepted immediately.
func TestCommitteeRevealDelayOff(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "job0", Fee: 10})
	require.NoError(t, err)

	b, err := f.keeper.Beacon.Get(f.ctx, "job0")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "delay == 0 means the seed is fixed at open time")
}

// delay > 0: the seed is EMPTY at open time and commits are REFUSED; the EndBlocker at H+delay fixes
// the seed from the AppHash, and the commit then passes.
func TestCommitteeRevealDelayDeferred(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)

	// Enable the deferred reveal.
	p := types.DefaultParams()
	p.CommitteeRevealDelay = 2
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	// Open at block 10.
	ctx10 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(10)
	_, err = srv.OpenJob(ctx10, &types.MsgOpenJob{Creator: creator, JobId: "jobD", Fee: 10})
	require.NoError(t, err)

	// Empty seed, and the reveal is scheduled at H+delay = 12.
	b, err := f.keeper.Beacon.Get(ctx10, "jobD")
	require.NoError(t, err)
	require.Empty(t, b.Seed, "delay > 0 means the seed is not fixed at open time")
	has, err := f.keeper.PendingReveal.Has(ctx10, collections.Join(int64(12), "jobD"))
	require.NoError(t, err)
	require.True(t, has, "reveal scheduled at H+delay = 12")

	// A miner whose operator is the creator.
	idM1 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(ctx10, &types.MsgCreateMiner{Creator: creator, MinerId: idM1, Operator: creator, Stake: 1000})
	require.NoError(t, err)

	// A commit BEFORE the reveal is refused: the committee is not fixed yet.
	_, err = srv.CreateCommit(ctx10, &types.MsgCreateCommit{Creator: creator, JobId: "jobD__" + idM1, ResultCommit: "r"})
	require.Error(t, err, "commits are refused until the committee is revealed")

	// EndBlock at H=11: nothing is due, so the seed stays empty.
	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("apphash-11")})
	require.NoError(t, f.keeper.EndBlock(ctx11))
	b, _ = f.keeper.Beacon.Get(ctx11, "jobD")
	require.Empty(t, b.Seed, "not revealed yet at H=11")

	// EndBlock at H=12: the seed is fixed and the pending entry is consumed.
	ctx12 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(12).WithHeaderInfo(header.Info{Height: 12, AppHash: []byte("apphash-12")})
	require.NoError(t, f.keeper.EndBlock(ctx12))
	b, err = f.keeper.Beacon.Get(ctx12, "jobD")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "revealed at H=12")
	has, _ = f.keeper.PendingReveal.Has(ctx12, collections.Join(int64(12), "jobD"))
	require.False(t, has, "pending entry consumed")

	// The commit is now accepted.
	_, err = srv.CreateCommit(ctx12, &types.MsgCreateCommit{Creator: creator, JobId: "jobD__" + idM1, ResultCommit: "r"})
	require.NoError(t, err, "commit accepted after the reveal")
}
