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

// availBlock is the context of block `h` whose header carries `appHash`, the bytes the EndBlock of a
// boundary block rolls the next challenge from.
func availBlock(f *fixture, h int64, appHash string) sdk.Context {
	return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte(appHash)})
}

func availPresent(t *testing.T, f *fixture, ctx sdk.Context, epoch int64, mid string) bool {
	t.Helper()
	present, err := f.keeper.Available.Has(ctx, collections.Join(epoch, mid))
	require.NoError(t, err)
	return present
}

// availSetup arms availability windows of 100 blocks with the given answer deadline and registers one
// bonded miner whose operator signs the proofs.
func availSetup(t *testing.T, f *fixture, deadline uint64, mid string) (string, types.MsgServer, types.QueryServer) {
	t.Helper()
	p := types.DefaultParams()
	p.AvailEpochBlocks = 100
	p.AvailDeadlineBlocks = deadline
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	op := ssAddr20(t, f, "op-"+mid)
	require.NoError(t, gvSetMiner(f, f.ctx, mid, types.Miner{MinerId: mid, Operator: op, Stake: p.MinStake}))
	return op, keeper.NewMsgServerImpl(f.keeper), keeper.NewQueryServerImpl(f.keeper)
}

// After the Commit of a boundary block, baseapp builds the CheckTx state on that block's header
// (abci.go, Commit: `setState(execModeCheck, header)`), so the simulation behind `--gas auto` runs
// ProveAvailability at the boundary height on a state that already holds the next challenge. A
// definition that subtracts one epoch at every boundary height counted that proof for the closing
// epoch and refused it as too late once avail_deadline_blocks was armed, and the miner daemon then
// settles the challenge as closed for the whole window. availChallengeEpoch reads the roll height.
func TestAvailabilitySimulationOnACommittedBoundaryAnswersTheNewEpoch(t *testing.T) {
	f := initFixture(t)
	op, srv, _ := availSetup(t, f, 2, "mSimB")
	require.NoError(t, f.keeper.EndBlock(availBlock(f, 100, "epoch-one")))
	require.NoError(t, f.keeper.EndBlock(availBlock(f, 200, "epoch-two")))
	two, err := f.keeper.AvailChallenge.Get(f.ctx)
	require.NoError(t, err)

	// The committed state of block 200, at height 200: what CheckTx and the simulation read.
	committed := availBlock(f, 200, "epoch-two")
	sim, _ := committed.CacheContext()
	_, err = srv.ProveAvailability(sim, &types.MsgProveAvailability{Creator: op, MinerId: "mSimB", Challenge: two})
	require.NoError(t, err, "a proof of the new challenge simulated on the committed boundary block is on time")
	require.True(t, availPresent(t, f, sim, 2, "mSimB"))
	require.False(t, availPresent(t, f, sim, 1, "mSimB"))
	require.False(t, availPresent(t, f, committed, 2, "mSimB"), "the simulation writes nothing")
	rolled, err := f.keeper.AvailChallengeHeight.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, int64(200), rolled)

	// Delivered in the next block, the same proof counts for the same epoch.
	_, err = srv.ProveAvailability(availBlock(f, 201, "after-two"), &types.MsgProveAvailability{Creator: op, MinerId: "mSimB", Challenge: two})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, committed, 2, "mSimB"))
}

// The query and the handler read one definition: on both states that share a boundary height, the
// query reports the epoch the handler records a proof under, next to the challenge it returns.
func TestAvailabilityQueryAndProofAgreeOnTheBoundaryHeight(t *testing.T) {
	f := initFixture(t)
	op, srv, qs := availSetup(t, f, 0, "mAgree")
	require.NoError(t, f.keeper.EndBlock(availBlock(f, 100, "epoch-one")))
	one, err := f.keeper.AvailChallenge.Get(f.ctx)
	require.NoError(t, err)

	// Block 200 before its EndBlock: its transactions answer epoch 1, the epoch the block closes.
	inBlock := availBlock(f, 200, "epoch-two")
	q, err := qs.GetAvailChallenge(inBlock, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, one, q.Challenge)
	require.Equal(t, int64(1), q.Epoch, "inside the boundary block the challenge in force is epoch 1's")
	tx, _ := inBlock.CacheContext()
	_, err = srv.ProveAvailability(tx, &types.MsgProveAvailability{Creator: op, MinerId: "mAgree", Challenge: one})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, tx, q.Epoch, "mAgree"))

	// The committed state of block 200: the next challenge, and the next epoch.
	require.NoError(t, f.keeper.EndBlock(availBlock(f, 200, "epoch-two")))
	committed := availBlock(f, 200, "epoch-two")
	q, err = qs.GetAvailChallenge(committed, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.NotEqual(t, one, q.Challenge)
	require.Equal(t, int64(2), q.Epoch, "the query reports the epoch of the challenge it returns")
	sim, _ := committed.CacheContext()
	_, err = srv.ProveAvailability(sim, &types.MsgProveAvailability{Creator: op, MinerId: "mAgree", Challenge: q.Challenge})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, sim, q.Epoch, "mAgree"))
}

// The upgrade height falls on an availability boundary. The state is the one the epoch 14 binary
// leaves: the challenge it rolled one window earlier and no roll height. The block's PreBlock runs the
// epoch 15 migration, which writes none, and the block's transactions answer that challenge for the
// epoch the block closes, which its own EndBlock slashes and pays. The slash is armed at one absence in a
// one-epoch window, so a proof counted for the wrong epoch shows as a slash, and a miner without a proof
// shows that it bites. After the first roll, the committed block accepts the new challenge on time.
func TestAvailabilityUpgradeHeightOnAnEpochBoundary(t *testing.T) {
	f := initFixture(t)
	aeFundModule(f)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 100
	p.AvailSlashBps = 1000
	p.AvailFailK = 1
	p.AvailFailWindow = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	proverOp := ssAddr20(t, f, "avail-upgrade-prover")
	silentOp := ssAddr20(t, f, "avail-upgrade-silent")
	require.NoError(t, gvSetMiner(f, f.ctx, "mUpProver", types.Miner{MinerId: "mUpProver", Operator: proverOp, Stake: p.MinStake}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mUpSilent", types.Miner{MinerId: "mUpSilent", Operator: silentOp, Stake: p.MinStake}))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "rolled-by-epoch-14"))
	srv := keeper.NewMsgServerImpl(f.keeper)

	upgrade := availBlock(f, 300, "first-epoch-15-roll")
	require.NoError(t, keeper.NewMigrator(f.keeper).Migrate1to2(upgrade))
	_, err := f.keeper.AvailChallengeHeight.Get(upgrade)
	require.ErrorIs(t, err, collections.ErrNotFound, "the migration writes no roll height")

	_, err = srv.ProveAvailability(upgrade, &types.MsgProveAvailability{Creator: proverOp, MinerId: "mUpProver", Challenge: "rolled-by-epoch-14"})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, upgrade, 2, "mUpProver"), "the upgrade block closes epoch 2")
	require.False(t, availPresent(t, f, upgrade, 3, "mUpProver"))

	require.NoError(t, f.keeper.EndBlock(upgrade))
	prover, err := f.keeper.Miner.Get(f.ctx, "mUpProver")
	require.NoError(t, err)
	require.Equal(t, p.MinStake, prover.Stake, "proven for epoch 2, not slashed")
	_, err = f.keeper.AvailFailCount.Get(f.ctx, "mUpProver")
	require.ErrorIs(t, err, collections.ErrNotFound)
	silent, err := f.keeper.Miner.Get(f.ctx, "mUpSilent")
	require.NoError(t, err)
	require.Less(t, silent.Stake, p.MinStake, "the slash is armed: an absence for epoch 2 costs stake")
	rolled, err := f.keeper.AvailChallengeHeight.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, int64(300), rolled)
	next, err := f.keeper.AvailChallenge.Get(f.ctx)
	require.NoError(t, err)
	require.NotEqual(t, "rolled-by-epoch-14", next)

	p.AvailDeadlineBlocks = 2
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	committed := availBlock(f, 300, "first-epoch-15-roll")
	sim, _ := committed.CacheContext()
	_, err = srv.ProveAvailability(sim, &types.MsgProveAvailability{Creator: proverOp, MinerId: "mUpProver", Challenge: next})
	require.NoError(t, err, "simulated on the committed upgrade block, the new challenge is on time")
	require.True(t, availPresent(t, f, sim, 3, "mUpProver"))
	_, err = srv.ProveAvailability(availBlock(f, 301, "after-upgrade"), &types.MsgProveAvailability{Creator: proverOp, MinerId: "mUpProver", Challenge: next})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, committed, 3, "mUpProver"))
}
