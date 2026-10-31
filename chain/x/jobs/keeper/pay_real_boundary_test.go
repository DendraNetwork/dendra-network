package keeper_test

import (
	"cosmossdk.io/math"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestPayRealRejectsBadAddressesAndMovesOnlyRequestedAmount(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	from := ssAddr20(t, f, "pay-real-from")
	to := ssAddr20(t, f, "pay-real-to")
	fromB, err := f.addressCodec.StringToBytes(from)
	require.NoError(t, err)
	toB, err := f.addressCodec.StringToBytes(to)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(fromB), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10))))
	f.bank.setBalance(sdk.AccAddress(toB), sdk.NewCoins())
	_, err = srv.PayReal(f.ctx, &types.MsgPayReal{Creator: "bad", Recipient: to, Amount: 1})
	require.Error(t, err)
	_, err = srv.PayReal(f.ctx, &types.MsgPayReal{Creator: from, Recipient: "bad", Amount: 1})
	require.Error(t, err)
	_, err = srv.PayReal(f.ctx, &types.MsgPayReal{Creator: from, Recipient: to, Amount: 0})
	require.NoError(t, err)
	require.True(t, f.bank.balOf(sdk.AccAddress(toB)).AmountOf("udndr").IsZero())
	_, err = srv.PayReal(f.ctx, &types.MsgPayReal{Creator: from, Recipient: to, Amount: 11})
	require.Error(t, err)
	_, err = srv.PayReal(f.ctx, &types.MsgPayReal{Creator: from, Recipient: to, Amount: 10})
	require.NoError(t, err)
	require.True(t, f.bank.balOf(sdk.AccAddress(fromB)).AmountOf("udndr").IsZero())
	require.Equal(t, int64(10), f.bank.balOf(sdk.AccAddress(toB)).AmountOf("udndr").Int64())
}
