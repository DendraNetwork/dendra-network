package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// The emission engine must survive an export/import round trip.
//
// An `InitGenesis` that resets the Reserve to its genesis value and the epoch to 0 on every call,
// without reading the genesis it is given, breaks a migration in three silent ways: an already-spent
// Reserve comes back full, so the same release schedule plays twice; unclaimed pools are wiped, so
// subsidies owed to miners vanish while the coins backing them are counted again as Reserve; and
// `last_epoch = 0` against an already high block height fires an epoch on the first block.
//
// The fixture is chosen so that a round trip carrying nothing cannot pass: the exported Reserve (1000)
// is far from `GenesisReserveU`, and the pools are non-zero.
func TestEmissionGenesisRoundTripPreservesTheEngine(t *testing.T) {
	f := initFixture(t)

	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, 1000))
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 77))
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 33))
	require.NoError(t, f.keeper.SecurityDistributedTotal.Set(f.ctx, 11))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 4200))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 9_999_999))
	require.NoError(t, f.keeper.WorkPaidTotal.Set(f.ctx, 5))
	require.NoError(t, f.keeper.AvailPaidTotal.Set(f.ctx, 7))

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.NotNil(t, exported.State, "the engine state must be exported, not only the params")

	g := initFixture(t)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	res, err := g.keeper.Reserve.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), res,
		"an already-spent Reserve must not come back full: that would release the same schedule twice")

	wp, _ := g.keeper.WorkPool.Get(g.ctx)
	ap, _ := g.keeper.AvailPool.Get(g.ctx)
	sp, _ := g.keeper.SecurityDistributedTotal.Get(g.ctx)
	require.Equal(t, uint64(77), wp, "unclaimed work subsidies: wiping them makes them owed to nobody")
	require.Equal(t, uint64(33), ap)
	require.Equal(t, uint64(11), sp)
	wpt, _ := g.keeper.WorkPaidTotal.Get(g.ctx)
	apt, _ := g.keeper.AvailPaidTotal.Get(g.ctx)
	require.Equal(t, uint64(5), wpt, "the paid totals of epoch 15 travel like the counters beside them")
	require.Equal(t, uint64(7), apt)

	lastEp, _ := g.keeper.LastEpoch.Get(g.ctx)
	require.Equal(t, uint64(4200), lastEp,
		"last_epoch=0 against a high block height would fire an epoch on the very first block")
	ls, _ := g.keeper.LastSupply.Get(g.ctx)
	require.Equal(t, uint64(9_999_999), ls)
}

// A fresh chain still bootstraps. The guard must not break the nominal case: with no `state` block
// (a hand-written genesis, `config.yml`, an older binary), the engine must start on the genesis
// Reserve, otherwise a resurrection of spent funds would be traded for a chain that never emits.
func TestEmissionGenesisWithoutStateStillBootstraps(t *testing.T) {
	f := initFixture(t)
	legacy := types.DefaultGenesis() // State == nil, as an export produced before state was carried
	require.Nil(t, legacy.State)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *legacy))

	res, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.NotZero(t, res, "fresh chain: the engine must be bootstrapped on the genesis Reserve")
}

// An epoch ahead of the chain must not silence emission.
//
// `last_epoch` is an absolute height. Restored onto a chain that restarts lower (an import at height
// 1, which is what `forZeroHeight` produces), the "not time yet" condition stays true for the whole
// distance to catch up, and emission goes quiet with no error and no log line. `x/jobs` solves the
// twin case by carrying remaining time; here the value is a starting point rather than a deadline, so
// the remedy is to refuse the silence: realign on the current height and log it.
func TestEmissionFutureLastEpochDoesNotSilenceEmission(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.EpochBlocks = 300
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, 1_000_000))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 9000)) // state imported from a much higher chain

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5) // the chain itself restarts near zero
	require.NoError(t, f.keeper.RunEpoch(ctx))

	lastEp, err := f.keeper.LastEpoch.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(5), lastEp,
		"last_epoch must be realigned on the current height, otherwise emission waits 8995 blocks in silence")
}

// A legitimately exhausted Reserve stays at zero. This is the case proto3 alone cannot tell apart
// from "unset", and the reason `state` is an optional message: a block that is present with a Reserve
// of 0 means "nothing is left", not "start over".
func TestEmissionExhaustedReserveIsNotResurrected(t *testing.T) {
	f := initFixture(t)
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 0, LastEpoch: 12345}
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g))

	res, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), res, "an exhausted Reserve is zero and must stay zero")
	lastEp, _ := f.keeper.LastEpoch.Get(f.ctx)
	require.Equal(t, uint64(12345), lastEp)
}
