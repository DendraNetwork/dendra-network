package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// Phase 1a — ClaimSubsidy paie REELLEMENT l'operateur depuis le WorkPool de l'emission
// (avant : simple compteur). Le WorkPool est debite exactement du montant verse.
func TestClaimSubsidyPaysFromWorkPool(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	startPool := uint64(1_000_000)
	f.emission.pool = startPool

	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.NoError(t, err)

	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	paid := m.SubsidyClaimed
	require.Greater(t, paid, uint64(0))               // a REELLEMENT verse quelque chose
	require.Equal(t, paid, f.emission.paid[operator]) // verse en vrais coins a l'operateur
	require.Equal(t, startPool-paid, f.emission.pool) // WorkPool debite d'autant
}

// Pool insuffisant -> paiement PARTIEL ; SubsidyClaimed n'avance que du verse (reste reclamable).
func TestClaimSubsidyPartialWhenPoolShort(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	f.emission.pool = 50 // tres en-dessous du plafond -> paiement partiel
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.NoError(t, err)
	require.Equal(t, uint64(50), f.emission.paid[operator]) // paye seulement ce que le pool couvre
	require.Equal(t, uint64(0), f.emission.pool)            // pool vide

	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	require.Equal(t, uint64(50), m.SubsidyClaimed) // n'avance que du montant verse
}

// Pool vide -> rejet explicite (rien a verser).
func TestClaimSubsidyEmptyPoolRejects(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	f.emission.pool = 0
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}
