package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"dendra/x/emission/types"
)

// TestRunEpochSecurityFluxEM02 — EM-02 : `RunEpoch` déplace RÉELLEMENT la sécurité vers fee_collector,
// décrémente la Réserve, RETIENT avail/work, et l'identité comptable
// `reserve_left + WorkPool + AvailPool == reserve_initial − sécurité_envoyée` tient (= l'invariant
// observé LIVE, jusqu'ici couvert par AUCUN test puisque le fixture passait bank=nil).
func TestRunEpochSecurityFluxEM02(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000 // pas de burns -> demande 0 -> flux travail gaté à 0
	// epoch_blocks=20 EXPLICITE (le défaut est désormais ~1 an de blocs) -> l'époque se déclenche à h=100.
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(2200, 5000, 2000, 15000, 20)))

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100) // > epoch_blocks(20) -> époque déclenchée
	require.NoError(t, f.keeper.RunEpoch(ctx))

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(2_937_000_000000), r, "Réserve -11% (3,3M - 363M)")

	require.Equal(t, "217800000000udndr", f.bank.moved[authtypes.FeeCollectorName].String(),
		"SEULE la sécurité (6,6%) part vers fee_collector")

	av, err := f.keeper.AvailPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(145_200_000000), av, "avail (4,4%) RETENU au module")

	wp, err := f.keeper.WorkPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), wp, "work gaté à 0 sans demande")

	// INVARIANT comptable (= solde(module) == Reserve + WorkPool + AvailPool, seule la sécu sort).
	require.Equal(t, reserve-217_800_000000, r+wp+av)
}

// TestRunEpochDefersWhenUnfundedEM03 — EM-03 : compte de module NON financé -> `RunEpoch` DIFFÈRE
// (log + avance l'époque) SANS halt et SANS toucher la Réserve. Best-effort vérifié.
func TestRunEpochDefersWhenUnfundedEM03(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(2200, 5000, 2000, 15000, 20)))
	f.bank.fail = true // SendCoinsFromModuleToModule échoue -> libération différée

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100)
	require.NoError(t, f.keeper.RunEpoch(ctx), "best-effort : pas de halt même non financé")

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, reserve, r, "Réserve INCHANGÉE (libération différée, retry au prochain epoch)")
}
