package keeper_test

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// Key presence is a property of the state.
//
// `workpool` / `availpool` / the security total are written by nobody before the first epoch. While
// the bootstrap did not write them, an export/import produced a state carrying three keys the original
// did not have, so a different app hash, and the gap was hidden by an exception in `app/sim_test.go`.
// Since these pools carry money, the round trip could then no longer see a real divergence in them.
// The bootstrap now writes them at zero, which makes presence uniform and the round trip an identity
// in both directions, including for a pool that has really been drained to 0. The two paid totals of
// epoch 15 follow the same rule, so the set is eight items.
//
// A test on values would not see this, since the Items are 0 on both sides. Presence is what
// differs, and presence is what makes the app hash. Hence `Has`, not `Get`.

func TestEmissionBootstrapSetsTheEightItems(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))

	presents := map[string]bool{}
	for name, has := range map[string]func() (bool, error){
		"reserve":                    func() (bool, error) { return f.keeper.Reserve.Has(f.ctx) },
		"workpool":                   func() (bool, error) { return f.keeper.WorkPool.Has(f.ctx) },
		"availpool":                  func() (bool, error) { return f.keeper.AvailPool.Has(f.ctx) },
		"security_distributed_total": func() (bool, error) { return f.keeper.SecurityDistributedTotal.Has(f.ctx) },
		"lastepoch":                  func() (bool, error) { return f.keeper.LastEpoch.Has(f.ctx) },
		"lastsupply":                 func() (bool, error) { return f.keeper.LastSupply.Has(f.ctx) },
		"work_paid_total":            func() (bool, error) { return f.keeper.WorkPaidTotal.Has(f.ctx) },
		"avail_paid_total":           func() (bool, error) { return f.keeper.AvailPaidTotal.Has(f.ctx) },
	} {
		ok, err := has()
		require.NoError(t, err)
		presents[name] = ok
	}
	for name, ok := range presents {
		require.True(t, ok,
			"`%s` is absent after the bootstrap. An absent Item is indistinguishable from an Item at "+
				"zero for every reader, but not for the app-hash: the export carries it as 0 and the "+
				"import writes it, so the re-imported state carries a key the original did not have. "+
				"That is what forced `app/sim_test.go` to declare an exception on three pools that "+
				"carry money.", name)
	}
}

// The round trip is an identity, on a fresh chain and on a chain whose pool has been drained.
//
// The second case is what ruled out the other candidate remedy ("do not write a zero at import"): a
// pool that was really drained is legitimately 0, and it would disappear at re-import. The same
// artifact, reversed, rarer, and so harder to see.
func TestEmissionRoundTripPreservesPresence(t *testing.T) {
	for _, tc := range []struct {
		name    string
		prepare func(*fixture)
	}{
		{"fresh chain", func(*fixture) {}},
		{"work pool drained to zero", func(f *fixture) {
			require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 0))
		}},
		{"credited pools", func(f *fixture) {
			require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 12345))
			require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 678))
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := initFixture(t)
			require.NoError(t, a.keeper.InitGenesis(a.ctx, *types.DefaultGenesis()))
			tc.prepare(a)

			exported, err := a.keeper.ExportGenesis(a.ctx)
			require.NoError(t, err)

			b := initFixture(t)
			// The bank follows the counters. `balance(emission module) == Reserve + WorkPool + AvailPool` is
			// the invariant RunEpoch states and the genesis guard enforces, so a fixture that imports
			// credited pools without holding the coins models a chain that cannot exist, and the guard
			// rightly refuses it. The balance is derived from the state being imported rather than retyped,
			// so a case that credits different amounts stays covered.
			if st := exported.State; st != nil {
				b.bank.spendable = st.Reserve + st.WorkPool + st.AvailPool
			}
			require.NoError(t, b.keeper.InitGenesis(b.ctx, *exported))

			reexported, err := b.keeper.ExportGenesis(b.ctx)
			require.NoError(t, err)
			require.EqualExportedValues(t, exported.State, reexported.State,
				"the state does not survive a round-trip")

			for name, pair := range map[string][2]func() (bool, error){
				"reserve":                    {func() (bool, error) { return a.keeper.Reserve.Has(a.ctx) }, func() (bool, error) { return b.keeper.Reserve.Has(b.ctx) }},
				"workpool":                   {func() (bool, error) { return a.keeper.WorkPool.Has(a.ctx) }, func() (bool, error) { return b.keeper.WorkPool.Has(b.ctx) }},
				"availpool":                  {func() (bool, error) { return a.keeper.AvailPool.Has(a.ctx) }, func() (bool, error) { return b.keeper.AvailPool.Has(b.ctx) }},
				"security_distributed_total": {func() (bool, error) { return a.keeper.SecurityDistributedTotal.Has(a.ctx) }, func() (bool, error) { return b.keeper.SecurityDistributedTotal.Has(b.ctx) }},
				"lastepoch":                  {func() (bool, error) { return a.keeper.LastEpoch.Has(a.ctx) }, func() (bool, error) { return b.keeper.LastEpoch.Has(b.ctx) }},
				"lastsupply":                 {func() (bool, error) { return a.keeper.LastSupply.Has(a.ctx) }, func() (bool, error) { return b.keeper.LastSupply.Has(b.ctx) }},
				"work_paid_total":            {func() (bool, error) { return a.keeper.WorkPaidTotal.Has(a.ctx) }, func() (bool, error) { return b.keeper.WorkPaidTotal.Has(b.ctx) }},
				"avail_paid_total":           {func() (bool, error) { return a.keeper.AvailPaidTotal.Has(a.ctx) }, func() (bool, error) { return b.keeper.AvailPaidTotal.Has(b.ctx) }},
			} {
				ha, err := pair[0]()
				require.NoError(t, err)
				hb, err := pair[1]()
				require.NoError(t, err)
				require.Equal(t, ha, hb,
					"`%s`: present=%v originally, present=%v after re-import. One key more or less "+
						"changes the app-hash even when the value is identical — that is exactly the "+
						"divergence `TestAppImportExport` measures.", name, ha, hb)
			}
		})
	}
}

// No phantom prefix in `x/emission/types`: the same check as for `x/jobs`, since a defect class
// closed on a single module is not closed.
func TestEmissionHasNoGhostPrefix(t *testing.T) {
	declared, err := simutil.DeclaredPrefixes(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declared), 5,
		"only %d prefixes read: the scan no longer bites", len(declared))

	f := initFixture(t)
	require.Empty(t, simutil.PhantomPrefixes(f.keeper.Schema, declared),
		"prefix(es) declared in `x/emission/types` but absent from the schema: reachable only through "+
			"direct store access, therefore invisible to the export and to the round-trip")
}
