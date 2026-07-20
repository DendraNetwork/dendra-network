package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// NEW-GO-42 (audit v3) — SettlePay : quand `Amount` n'est PAS divisible par le nombre de gagnants, le
// reliquat (≤ N-1 udndr) est versé au 1er gagnant → le client paie EXACTEMENT `Amount` (avant : per×N,
// le reliquat n'était jamais versé).
func TestSettlePayRemainderGO42(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	addr20 := func(s string) (string, sdk.AccAddress) {
		b := make([]byte, 20)
		copy(b, s)
		o, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return o, sdk.AccAddress(b)
	}
	client, clientAddr := addr20("rem-client")
	f.bank.setBalance(clientAddr, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000))))

	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", State: "open"}))
	for _, id := range []string{"a", "b", "c"} {
		op, _ := addr20("rop-" + id)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Operator: op, Stake: 1000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	// Amount=3001, 3 gagnants : per=1000, reliquat=1 → 1er gagnant 1001 ; total débité = 3001.
	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "j1", Amount: 3001})
	require.NoError(t, err)
	require.Equal(t, "6999udndr", f.bank.SpendableCoins(f.ctx, clientAddr).String()) // 10000 - 3001 (et NON - 3000)
}
