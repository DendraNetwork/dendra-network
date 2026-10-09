package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// THE COUNTER IS NOT THE MONEY.
//
// `Reserve` counts coins that live in the module account, and `InitGenesis` does not put them there.
// A genesis that announces the Reserve while crediting an ordinary account produces a chain that
// looks healthy — both numbers exist, every balance query answers — until the first epoch, at which
// point `RunEpoch` moves the security tranche out of an empty account, fails, and DEFERS. It defers
// at every epoch after it: the reward budget is unreachable for good, `WorkPool` and `AvailPool` stay
// at zero, and nothing reports it, because the deferral is deliberately quiet.
//
// These cases drive the shipped `InitGenesis`, and each half matters: a refusal that also refused a
// correct genesis would stop every chain from starting.

// ── WHAT MUST BE REFUSED ───────────────────────────────────────────────────────────────────────
func TestGenesisRefuseUneReserveNonCouverte(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0 // the coins went to an ordinary account, as the deployed genesis did

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err, "a Reserve counter with no coins behind it must be REFUSED at load")
	require.Contains(t, err.Error(), "NOT backed",
		"the refusal must name what is wrong, not fail on something else by accident")

	// AND IT MUST REFUSE BEFORE WRITING: a chain that refuses and still installs the counter would
	// leave exactly the state the refusal exists to prevent.
	_, rErr := f.keeper.Reserve.Get(f.ctx)
	require.Error(t, rErr, "nothing may be written when the genesis is refused")
}

// PARTIAL BACKING IS STILL A LIE, and it is the shape a rounding or a units mistake produces.
func TestGenesisRefuseUneReservePartiellementCouverte(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU - 1

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err, "one udndr short is still short: the last epoch would defer for ever")
	require.Contains(t, err.Error(), "NOT backed")
}

// THE COUNTER TO BACK IS THE WHOLE IDENTITY, NOT THE RESERVE ALONE.
//
// `RunEpoch` states it: `balance(emission module) == Reserve + WorkPool + AvailPool`, because only
// SECURITY leaves the module. Work and availability STAY there as counters, and `pay_work.go` /
// `pay_avail.go` read the module's spendable balance before sending. A genesis whose pools are
// declared but not funded therefore loads cleanly, looks healthy, and fails at the first work or
// availability payment -- the same "the counter is not the money" defect the Reserve check refuses,
// one level down and later.
func TestGenesisRefuseDesPoolsNonCouverts(t *testing.T) {
	f := initFixture(t)
	// The Reserve alone IS backed: only the pools are not. A guard reading `Reserve` and nothing else
	// passes this genesis, which is precisely what this case exists to catch.
	f.bank.spendable = keeper.GenesisReserveU
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: keeper.GenesisReserveU, WorkPool: 12345, AvailPool: 678}

	err := f.keeper.InitGenesis(f.ctx, *g)
	require.Error(t, err, "pools declared without coins behind them must be REFUSED at load")
	require.Contains(t, err.Error(), "NOT backed")
	require.Contains(t, err.Error(), "WorkPool",
		"the refusal must name the counters it summed, otherwise the operator fixes the wrong one")
}

// AND SECURITYPOOL IS DELIBERATELY OUT OF THAT SUM. Those coins are sent to `fee_collector`, so
// requiring the module to still hold them would refuse a perfectly correct genesis -- the symmetric
// mistake, and the one that stops a chain from starting rather than letting a broken one run.
func TestGenesisAccepteUnSecurityPoolSorti(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: keeper.GenesisReserveU, SecurityPool: 999_999}

	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g),
		"SecurityPool has left the module by design: counting it would refuse a correct genesis")
}

// A RESUME IS CHECKED TOO. The exported counter is as much a claim about money as the bootstrap one,
// and an export/import is the ordinary gesture of a migration.
func TestGenesisRefuseUneRepriseNonCouverte(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 10
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 5000}

	err := f.keeper.InitGenesis(f.ctx, *g)
	require.Error(t, err, "a resumed Reserve must be backed like a fresh one")
	require.Contains(t, err.Error(), "NOT backed")
}

// ── WHAT MUST STILL LOAD ───────────────────────────────────────────────────────────────────────

// The funded chain — the only one that should exist — loads and writes its counter.
func TestGenesisAccepteUneReserveCouverte(t *testing.T) {
	f := initFixture(t) // the fixture backs the Reserve by default
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))

	r, err := f.keeper.Reserve.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, keeper.GenesisReserveU, r, "a backed bootstrap must install its counter")
}

// EXACT COVER IS COVER. The bound is inclusive: asking for one udndr more than the counter would
// refuse a genesis whose accounting is exactly right.
func TestGenesisAcceptsAnExactCoverage(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = keeper.GenesisReserveU
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))
}

// AN EXHAUSTED RESERVE NEEDS NO BACKING, and refusing it would make a spent chain unable to re-import
// its own export — turning a legitimate end state into a chain that cannot restart.
func TestGenesisAccepteUneReserveEpuisee(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0
	g := types.DefaultGenesis()
	g.State = &types.EmissionState{Reserve: 0, WorkPool: 0, AvailPool: 0}

	require.NoError(t, f.keeper.InitGenesis(f.ctx, *g),
		"a Reserve of zero claims nothing, so it needs nothing behind it")
}

// ── THE ESCAPE HATCH, AND ITS TWO HALVES ───────────────────────────────────────────────────────
//
// The refusal made the LIVE network unjoinable on the day it shipped: the served genesis credits an
// ordinary account, so a node built from the published source exited 2 at InitChain while the chain
// itself carried on — it never replays InitGenesis. Refusing to CREATE protects; refusing to JOIN
// repaired nothing and kept miners away. Hence an explicit way past it, closed by default.
//
// An escape hatch is not tested only by what it OPENS: if any value opens it, a variable inherited by
// accident disarms the guard in silence.
func TestGenesisEscapeHatchOpensOnlyOnOne(t *testing.T) {
	for _, tc := range []struct {
		value string
		opens bool
	}{
		{"1", true},
		{"0", false},
		{"true", false}, // the most common reflex, and it must NOT open
		{"", false},
		{" 1", false}, // no tolerance on whitespace: a value close to the value is not the value
	} {
		t.Run("value="+tc.value, func(t *testing.T) {
			t.Setenv("DENDRA_ALLOW_UNBACKED_RESERVE", tc.value)
			f := initFixture(t)
			f.bank.spendable = 0 // exactly the state of the genesis served in production

			err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
			if tc.opens {
				require.NoError(t, err, "an operator JOINING an already-unbacked chain must be able to start")
				r, rErr := f.keeper.Reserve.Get(f.ctx)
				require.NoError(t, rErr, "and the state must be installed, or it joins nothing")
				require.Equal(t, keeper.GenesisReserveU, r,
					"a node that skips the guard must compute EXACTLY the state of one that never had it -- "+
						"that is what makes this hatch harmless to consensus")
				return
			}
			require.Error(t, err, "any value other than \"1\" leaves the refusal in place")
			require.Contains(t, err.Error(), "NOT backed")
		})
	}
}

// THE MESSAGE HAS TO SAY WHAT TO DO. A joining operator reads only what the refusal writes: if it does
// not name the way past, the way past does not exist for them.
func TestGenesisTheRefusalNamesTheEscapeHatch(t *testing.T) {
	f := initFixture(t)
	f.bank.spendable = 0

	err := f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis())
	require.Error(t, err)
	require.Contains(t, err.Error(), "DENDRA_ALLOW_UNBACKED_RESERVE",
		"the refusal must name the variable, or it blocks without pointing at a way out")
}

// AND THE ROUND TRIP STILL HOLDS: export then re-import on a funded chain, which is the gesture a
// migration performs and the one the guard must not stand in the way of.
func TestGenesisAllerRetourSurUneChaineCouverte(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))
	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	g := initFixture(t)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported),
		"a genesis exported from a funded chain must re-import into one")
}
