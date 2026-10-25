package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// The counter is not the money.
//
// `Reserve` counts coins that live in the module account, and `InitGenesis` does not put them there.
// A genesis that announces the Reserve while crediting an ordinary account produces a chain that looks
// healthy (both numbers exist, every balance query answers) until the first epoch, when `RunEpoch`
// moves the security tranche out of an empty account, fails, and defers. It defers at every epoch
// after that: the reward budget is unreachable, `WorkPool` and `AvailPool` stay at zero, and nothing
// reports it, because the deferral is quiet on purpose.
//
// These cases drive the shipped `InitGenesis`, and each half matters: a refusal that also refused a
// correct genesis would stop every chain from starting.

// What must be refused.
func TestGenesisRefusesAnUnbackedReserve(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0 // the coins went to an ordinary account, as in the deployed genesis

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err, "a Reserve counter with no coins behind it must be refused at load")
	require.Contains(t, err.Error(), "not backed",
		"the refusal must name what is wrong, not fail on something else by accident")

	// It must refuse before writing: a chain that refuses and still installs the counter would leave
	// the state the refusal exists to prevent.
	_, rErr := f.keeper.Reserve.Get(f.ctx)
	require.Error(t, rErr, "nothing may be written when the genesis is refused")
}

// Partial backing is still wrong, and it is the shape a rounding or units mistake produces.
func TestGenesisRefusesAPartiallyBackedReserve(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU - 1

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err, "one udndr short is still short: the last epoch would defer for ever")
	require.Contains(t, err.Error(), "not backed")
}

// The counter to back is the whole identity, not the Reserve alone.
//
// `RunEpoch` states it: `balance(emission module) == Reserve + WorkPool + AvailPool`, because only
// security leaves the module. Work and availability stay there as counters, and `pay_work.go` /
// `pay_avail.go` read the module's spendable balance before sending. A genesis whose pools are
// declared but not funded therefore loads cleanly, looks healthy, and fails at the first work or
// availability payment: the same defect the Reserve check refuses, one level down and later.
func TestGenesisRefusesUnbackedPools(t *testing.T) {
	f := initFixture(t)
	// The Reserve alone is backed and only the pools are not. A guard that reads `Reserve` and nothing
	// else passes this genesis, which is what this case exists to catch.
	f.bank.spendable = keeper.GenesisReserveU
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: keeper.GenesisReserveU, WorkPool: 12345, AvailPool: 678}

	err := f.keeper.InitGenesis(f.ctx, *g)
	require.Error(t, err, "pools declared without coins behind them must be refused at load")
	require.Contains(t, err.Error(), "not backed")
	require.Contains(t, err.Error(), "WorkPool",
		"the refusal must name the counters it summed, otherwise the operator fixes the wrong one")
}

// SecurityPool is left out of that sum on purpose. Those coins are sent to `fee_collector`, so
// requiring the module to still hold them would refuse a correct genesis: the symmetric mistake, and
// the one that stops a chain from starting rather than letting a broken one run.
func TestGenesisAcceptsAWithdrawnSecurityPool(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: keeper.GenesisReserveU, SecurityPool: 999_999}

	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g),
		"SecurityPool has left the module by design: counting it would refuse a correct genesis")
}

// A resume is checked too. The exported counter is as much a claim about money as the bootstrap one,
// and an export/import is the usual step of a migration.
func TestGenesisRefusesAnUnbackedResume(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 10
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 5000}

	err := f.keeper.InitGenesis(f.ctx, *g)
	require.Error(t, err, "a resumed Reserve must be backed like a fresh one")
	require.Contains(t, err.Error(), "not backed")
}

// What must still load.

// The funded chain, the only one that should exist, loads and writes its counter.
func TestGenesisAcceptsABackedReserve(t *testing.T) {
	f := initFixture(t) // the fixture backs the Reserve by default
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))

	r, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, keeper.GenesisReserveU, r, "a backed bootstrap must install its counter")
}

// Exact cover is cover. The bound is inclusive: asking for one udndr more than the counter would
// refuse a genesis whose accounting is exactly right.
func TestGenesisAcceptsAnExactCoverage(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))
}

// An exhausted Reserve needs no backing. Refusing it would keep a spent chain from re-importing its
// own export, turning a legitimate end state into a chain that cannot restart.
func TestGenesisAcceptsAnExhaustedReserve(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 0, WorkPool: 0, AvailPool: 0}

	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g),
		"a Reserve of zero claims nothing, so it needs nothing behind it")
}

// The escape hatch, and its two halves.
//
// A network whose served genesis credits an ordinary account keeps running, since it never replays
// InitGenesis, while a node built from the published source would exit 2 at InitChain. Refusing to
// create a chain protects it; refusing to join one repairs nothing. Hence an explicit way past the
// refusal, closed by default.
//
// An escape hatch is tested on what it keeps closed as well as on what it opens: if any value opens
// it, a variable inherited by accident disarms the guard silently.
func TestGenesisEscapeHatchOpensOnlyOnOne(t *testing.T) {
	for _, tc := range []struct {
		value string
		opens bool
	}{
		{"1", true},
		{"0", false},
		{"true", false}, // the most common reflex, and it must not open
		{"", false},
		{" 1", false}, // no tolerance on whitespace: a value close to the value is not the value
	} {
		t.Run("value="+tc.value, func(t *testing.T) {
			t.Setenv("DENDRA_ALLOW_UNBACKED_RESERVE", tc.value)
			f := initFixture(t)
			f.bank.spendable = 0 // the state of a genesis that credits an ordinary account

			err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
			if tc.opens {
				require.NoError(t, err, "an operator joining an already-unbacked chain must be able to start")
				r, rErr := f.keeper.Reserve.Get(f.ctx)
				require.NoError(t, rErr, "and the state must be installed, or it joins nothing")
				require.Equal(t, keeper.GenesisReserveU, r,
					"a node that skips the guard must compute exactly the state of one that never had it -- "+
						"that is what makes this hatch harmless to consensus")
				return
			}
			require.Error(t, err, "any value other than \"1\" leaves the refusal in place")
			require.Contains(t, err.Error(), "not backed")
		})
	}
}

// The message has to say what to do. A joining operator reads only what the refusal writes, so it
// has to name the way past.
func TestGenesisTheRefusalNamesTheEscapeHatch(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err)
	require.Contains(t, err.Error(), "DENDRA_ALLOW_UNBACKED_RESERVE",
		"the refusal must name the variable, or it blocks without pointing at a way out")
}

// The round trip still holds: export then re-import on a funded chain, which is what a migration does
// and what the guard must not block.
func TestGenesisRoundTripOnABackedChain(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))
	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	g := initFixture(t)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported),
		"a genesis exported from a funded chain must re-import into one")
}
