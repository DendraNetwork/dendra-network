package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Deflation: payout BURNS FeeBurnBps (5 %) of the escrow and distributes the REST.
// Conservation: burn + paid + surplus returned = job.Fee, and the burn is a REAL destruction of coins.
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
	// The job escrow is pre-funded in the module account, as if open-job had locked the fee.
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(fee)))

	// 3 miners (a full committee of size 3) with the SAME canonical commit -> all winners.
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}
	client := addr20("client-xyz")
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", Fee: fee, Client: client}))

	_, err := srv.Payout(f.ctx, &types.MsgPayout{Creator: addr20("trigger-addr"), JobId: "j1"})
	require.NoError(t, err)

	// burn = 5 % of 10000 = 500, a REAL destruction of coins, so the supply falls.
	require.Equal(t, "500udndr", f.bank.burned.String())
	// Conservation: the module escrow is fully consumed (burn 500 + paid 9498 + surplus 2 returned).
	require.True(t, f.bank.mod[types.ModuleName].Empty(), "escrow not cleared: %s", f.bank.mod[types.ModuleName])
}
