package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/emission/types"
)

// EM-04 — RunEpoch lit les paramètres GOUVERNABLES on-chain (taux d'émission + epoch_blocks) au lieu
// des constantes codées en dur. (Le repli sur les défauts v5 reste couvert par EM-02/EM-03.)
func TestRunEpochGovernableParams(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 1_000_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000 // demande 0 -> flux travail gaté à 0

	// PARAMS GOUVERNÉS : release 10% (au lieu de 22%), avail 30%, époque tous les 50 blocs.
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(1000, 5000, 3000, 15000, 50)))

	// hauteur 40 < epoch_blocks gouverné (50) -> rien encore
	ctx40 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(40)
	require.NoError(t, f.keeper.RunEpoch(ctx40))
	r0, err := f.keeper.Reserve.Get(ctx40)
	require.NoError(t, err)
	require.Equal(t, reserve, r0, "epoch_blocks gouverné (50) respecté : rien à 40")

	// hauteur 60 > 50 -> époque déclenchée au TAUX gouverné (10%)
	ctx60 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(60)
	require.NoError(t, f.keeper.RunEpoch(ctx60))
	r1, err := f.keeper.Reserve.Get(ctx60)
	require.NoError(t, err)
	// release 10% = 100k udndr ; work gaté 0 ; avail 30% = 30k ; sécu = reste = 20k ; released = 50k
	require.Equal(t, reserve-50_000_000000, r1, "taux d'émission gouverné (10%) appliqué")
}
