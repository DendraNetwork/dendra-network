package app

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/log"
	upgradetypes "cosmossdk.io/x/upgrade/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	dbm "github.com/cosmos/cosmos-db"
	"github.com/cosmos/cosmos-sdk/client/flags"
	simtestutil "github.com/cosmos/cosmos-sdk/testutil/sims"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	jobskeeper "github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The epoch 15 plan is registered in the real application and its handler runs,
// through the same `ApplyUpgrade` the upgrade module calls at the planned height. A plan that exists
// only as a name halts the chain on "unknown upgrade"; a handler that errors halts it too, with the one
// validator this network has. So the handler is run here, on the state an epoch-14 node carries: the
// stored parameters without the new fields, and a version map where x/jobs is still at 1.
func TestUpgradeEpoch15PlanIsRegisteredAndItsHandlerRuns(t *testing.T) {
	require.Contains(t, upgradePlans, UpgradeEpoch15, "the plan must be in the list the handler loop and the store loader read")

	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})
	require.True(t, app.UpgradeKeeper.HasHandler(UpgradeEpoch15),
		"a passed proposal naming this plan must find a handler, or the node refuses to restart")

	// The upgrade keeper records the done height from the header info, which baseapp sets per block;
	// a hand-made context carries it only if set here.
	ctx := app.NewUncachedContext(false, cmtproto.Header{Height: 10, ChainID: "epoch15-rehearsal"}).
		WithHeaderInfo(header.Info{Height: 10, ChainID: "epoch15-rehearsal"})
	stored := jobstypes.DefaultParams()
	stored.MinStake = 4242
	stored.AuditResolveTimeout = 999
	require.NoError(t, app.JobsKeeper.Params.Set(ctx, stored))
	vm := app.ModuleManager.GetVersionMap()
	require.Equal(t, uint64(2), vm[jobstypes.ModuleName], "premise: this binary's x/jobs is at version 2")
	vm[jobstypes.ModuleName] = 1 // what the epoch-14 node recorded at its genesis
	require.NoError(t, app.UpgradeKeeper.SetModuleVersionMap(ctx, vm))

	require.NoError(t, app.UpgradeKeeper.ApplyUpgrade(ctx, upgradetypes.Plan{Name: UpgradeEpoch15, Height: 10}))

	after, err := app.UpgradeKeeper.GetModuleVersionMap(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(2), after[jobstypes.ModuleName], "the migration ran and the version map records the epoch")
	for name, v := range vm {
		if name != jobstypes.ModuleName {
			require.Equal(t, v, after[name], "no other module moved: %s", name)
		}
	}
	got, err := app.JobsKeeper.Params.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, stored, got, "the stored set is unchanged except for the five new fields, which read 0")
	done, err := app.UpgradeKeeper.GetDoneHeight(ctx, UpgradeEpoch15)
	require.NoError(t, err)
	require.Equal(t, int64(10), done, "what `dendrad query upgrade applied` answers on a node")
}

// The plan height falls on an availability boundary. The handler runs in that block's PreBlock, on
// the state the epoch 14 binary left: the challenge it rolled one window earlier and no roll height,
// which the handler does not write. A proof in that block answers the epoch the block closes, the one
// its EndBlock slashes and pays (x/jobs/keeper/availability.go, availChallengeEpoch).
func TestUpgradeEpoch15AtAnAvailabilityBoundary(t *testing.T) {
	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})
	const height = 300
	ctx := app.NewUncachedContext(false, cmtproto.Header{Height: height, ChainID: "epoch15-rehearsal"}).
		WithHeaderInfo(header.Info{Height: height, ChainID: "epoch15-rehearsal"})
	stored := jobstypes.DefaultParams()
	stored.AvailEpochBlocks = 100
	require.NoError(t, app.JobsKeeper.Params.Set(ctx, stored))
	require.NoError(t, app.JobsKeeper.AvailChallenge.Set(ctx, "rolled-by-epoch-14"))
	op := sdk.AccAddress([]byte("upgrade-boundary-op_")).String()
	require.NoError(t, app.JobsKeeper.Miner.Set(ctx, "mUp", jobstypes.Miner{MinerId: "mUp", Operator: op, Stake: stored.MinStake}))
	vm := app.ModuleManager.GetVersionMap()
	vm[jobstypes.ModuleName] = 1
	require.NoError(t, app.UpgradeKeeper.SetModuleVersionMap(ctx, vm))

	require.NoError(t, app.UpgradeKeeper.ApplyUpgrade(ctx, upgradetypes.Plan{Name: UpgradeEpoch15, Height: height}))
	_, err := app.JobsKeeper.AvailChallengeHeight.Get(ctx)
	require.ErrorIs(t, err, collections.ErrNotFound, "the handler writes no roll height")

	_, err = jobskeeper.NewMsgServerImpl(app.JobsKeeper).ProveAvailability(ctx,
		&jobstypes.MsgProveAvailability{Creator: op, MinerId: "mUp", Challenge: "rolled-by-epoch-14"})
	require.NoError(t, err)
	closing, err := app.JobsKeeper.Available.Has(ctx, collections.Join(int64(2), "mUp"))
	require.NoError(t, err)
	require.True(t, closing, "the upgrade block closes epoch 2")
	next, err := app.JobsKeeper.Available.Has(ctx, collections.Join(int64(3), "mUp"))
	require.NoError(t, err)
	require.False(t, next)
}
