package keeper_test

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// ═══ KEY PRESENCE IS A PROPERTY, NOT A DETAIL ════════════════════════════════════════════════════
//
// WHAT THESE TESTS GUARD. `workpool` / `availpool` / `securitypool` are written by nobody before the
// first epoch. As long as the bootstrap did not write them, an export/import produced a state
// carrying THREE KEYS THAT THE ORIGINAL DID NOT HAVE — a different app-hash — and the gap was masked
// by an exception declared in `app/sim_test.go`, at an explicit price: these pools carry money, so
// the round-trip could no longer see a REAL divergence concerning them. The bootstrap now writes
// them at zero, which makes presence uniform and makes the round-trip an identity in both
// directions — including for a pool that has genuinely been drained to 0.
//
// A test on VALUES would not see this: the six Items are 0 on both sides. It is the PRESENCE that
// differs, and presence is what makes the app-hash. Hence `Has`, not `Get`.

func TestEmissionAmorcagePoseLesSixItems(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))

	presents := map[string]bool{}
	for nom, has := range map[string]func() (bool, error){
		"reserve":      func() (bool, error) { return f.keeper.Reserve.Has(f.ctx) },
		"workpool":     func() (bool, error) { return f.keeper.WorkPool.Has(f.ctx) },
		"availpool":    func() (bool, error) { return f.keeper.AvailPool.Has(f.ctx) },
		"securitypool": func() (bool, error) { return f.keeper.SecurityPool.Has(f.ctx) },
		"lastepoch":    func() (bool, error) { return f.keeper.LastEpoch.Has(f.ctx) },
		"lastsupply":   func() (bool, error) { return f.keeper.LastSupply.Has(f.ctx) },
	} {
		ok, err := has()
		require.NoError(t, err)
		presents[nom] = ok
	}
	for nom, ok := range presents {
		require.True(t, ok,
			"`%s` is ABSENT after the bootstrap. An absent Item is indistinguishable from an Item at "+
				"zero for every reader, but NOT for the app-hash: the export carries it as 0 and the "+
				"import WRITES it, so the re-imported state carries a key the original did not have. "+
				"That is what forced `app/sim_test.go` to declare an exception on three pools that "+
				"carry money.", nom)
	}
}

// THE ROUND-TRIP IS AN IDENTITY — on a fresh chain AND on a chain whose pool has been drained.
//
// The second case is what ruled out the other candidate remedy ("do not write a zero at import"): a
// genuinely drained pool is legitimately 0, and would have seen it disappear at re-import. The same
// artefact, reversed — therefore rarer, therefore harder to see.
func TestEmissionRoundTripPreserveLaPresence(t *testing.T) {
	for _, cas := range []struct {
		nom   string
		prepa func(*fixture)
	}{
		{"fresh chain", func(*fixture) {}},
		{"work pool DRAINED to zero", func(f *fixture) {
			require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 0))
		}},
		{"credited pools", func(f *fixture) {
			require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 12345))
			require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 678))
		}},
	} {
		t.Run(cas.nom, func(t *testing.T) {
			a := initFixture(t)
			require.NoError(t, a.keeper.InitGenesis(a.ctx, *types.DefaultGenesis()))
			cas.prepa(a)

			exporte, err := a.keeper.ExportGenesis(a.ctx)
			require.NoError(t, err)

			b := initFixture(t)
			require.NoError(t, b.keeper.InitGenesis(b.ctx, *exporte))

			reexporte, err := b.keeper.ExportGenesis(b.ctx)
			require.NoError(t, err)
			require.EqualExportedValues(t, exporte.State, reexporte.State,
				"the state does not survive a round-trip")

			for nom, paire := range map[string][2]func() (bool, error){
				"reserve":      {func() (bool, error) { return a.keeper.Reserve.Has(a.ctx) }, func() (bool, error) { return b.keeper.Reserve.Has(b.ctx) }},
				"workpool":     {func() (bool, error) { return a.keeper.WorkPool.Has(a.ctx) }, func() (bool, error) { return b.keeper.WorkPool.Has(b.ctx) }},
				"availpool":    {func() (bool, error) { return a.keeper.AvailPool.Has(a.ctx) }, func() (bool, error) { return b.keeper.AvailPool.Has(b.ctx) }},
				"securitypool": {func() (bool, error) { return a.keeper.SecurityPool.Has(a.ctx) }, func() (bool, error) { return b.keeper.SecurityPool.Has(b.ctx) }},
				"lastepoch":    {func() (bool, error) { return a.keeper.LastEpoch.Has(a.ctx) }, func() (bool, error) { return b.keeper.LastEpoch.Has(b.ctx) }},
				"lastsupply":   {func() (bool, error) { return a.keeper.LastSupply.Has(a.ctx) }, func() (bool, error) { return b.keeper.LastSupply.Has(b.ctx) }},
			} {
				ha, err := paire[0]()
				require.NoError(t, err)
				hb, err := paire[1]()
				require.NoError(t, err)
				require.Equal(t, ha, hb,
					"`%s`: present=%v originally, present=%v after re-import. One key more or less "+
						"changes the app-hash even when the value is identical — that is exactly the "+
						"divergence `TestAppImportExport` measures.", nom, ha, hb)
			}
		})
	}
}

// NO PHANTOM PREFIX IN `x/emission/types` — the same check as for `x/jobs`, because a class closed on
// a single module is not a closed class.
func TestEmissionAucunPrefixeFantome(t *testing.T) {
	declares, err := simutil.PrefixesDeclares(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declares), 5,
		"only %d prefixes read: the scan no longer bites", len(declares))

	f := initFixture(t)
	require.Empty(t, simutil.PhantomPrefixes(f.keeper.Schema, declares),
		"prefix(es) declared in `x/emission/types` but absent from the schema: reachable only through "+
			"direct store access, therefore invisible to the export and to the round-trip")
}
