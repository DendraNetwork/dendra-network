package keeper_test

import (
	"testing"

	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
)

// PayWork verse le flux TRAVAIL (WorkPool) en vrais coins, borné par le pool ET le solde du module,
// en préservant l'invariant (le pool baisse exactement du montant versé).
func TestPayWorkFromPool(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 1000))
	f.bank.spendable = 5000 // le compte de module couvre largement

	recip := authtypes.NewModuleAddress("recip-test")

	// (a) on demande 1500 mais le pool ne contient que 1000 -> versé 1000 (plafonné au pool)
	paid, err := f.keeper.PayWork(f.ctx, recip, 1500)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), paid)

	wp, err := f.keeper.WorkPool.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), wp) // WorkPool débité d'autant

	// les coins ont RÉELLEMENT été envoyés au destinataire
	require.Equal(t, int64(1000), f.bank.paid[recip.String()].AmountOf("udndr").Int64())

	// (b) pool vide -> plus rien à verser, pas d'erreur
	paid2, err := f.keeper.PayWork(f.ctx, recip, 100)
	require.NoError(t, err)
	require.Equal(t, uint64(0), paid2)
}

// Le versement est plafonné par le solde dépensable du module (borne défensive), même si le compteur
// WorkPool est plus élevé que ce que le compte détient réellement.
func TestPayWorkBoundedBySpendable(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 1000))
	f.bank.spendable = 300 // le module ne détient que 300 dépensables

	recip := authtypes.NewModuleAddress("recip-test")
	paid, err := f.keeper.PayWork(f.ctx, recip, 1000)
	require.NoError(t, err)
	require.Equal(t, uint64(300), paid) // borné au solde réel

	wp, err := f.keeper.WorkPool.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(700), wp) // pool débité seulement de ce qui a été versé
}
