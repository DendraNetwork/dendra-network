package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// SettlePay: when `Amount` is NOT divisible by the number of winners, the remainder (at most N-1
// udndr) goes to the first winner, so the client pays EXACTLY `Amount`. Paying `per` to each of the N
// winners would silently keep the remainder in escrow.
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

	// Client planted: settle-pay is reserved to the job's own client. Without it this fixture measured
	// the remainder arithmetic on a job that had no owner -- and the handler agreed, because it read no
	// such field.
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", State: "open", Client: client}))
	for _, id := range []string{"a", "b", "c"} {
		op, _ := addr20("rop-" + id)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Operator: op, Stake: 1000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	// Amount=3001 with 3 winners: per=1000, remainder=1 -> the first winner gets 1001; total debited 3001.
	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "j1", Amount: 3001})
	require.NoError(t, err)
	require.Equal(t, "6999udndr", f.bank.SpendableCoins(f.ctx, clientAddr).String()) // 10000 - 3001, NOT - 3000
}
