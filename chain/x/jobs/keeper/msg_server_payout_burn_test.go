package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// TestPayoutBurnsFeeV5 — déflation v5 : payout BRÛLE FeeBurnBps (5 %) de l'escrow et répartit le RESTE.
// Conservation : burn + payé + surplus rendu = job.Fee ; le burn est une VRAIE destruction (mock.burned).
func TestPayoutBurnsFeeV5(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	addr20 := func(s string) string {
		b := make([]byte, 20)
		copy(b, s)
		out, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return out
	}

	const fee = 10000
	// escrow du job pré-financé dans le compte de module (comme si open-job avait séquestré la fee).
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(fee)))

	// 3 mineurs (comité complet, size=3) avec le MÊME commit canonique -> tous gagnants.
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}
	client := addr20("client-xyz")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", Fee: fee, Client: client}))

	_, err := srv.Payout(f.ctx, &types.MsgPayout{Creator: addr20("trigger-addr"), JobId: "j1"})
	require.NoError(t, err)

	// burn = 5 % de 10000 = 500 (VRAIE destruction de coins -> supply ↓).
	require.Equal(t, "500udndr", f.bank.burned.String())
	// conservation : l'escrow du module est entièrement consommé (burn 500 + payé 9498 + surplus 2 rendu).
	require.True(t, f.bank.mod[types.ModuleName].Empty(), "escrow non soldé : %s", f.bank.mod[types.ModuleName])
}
