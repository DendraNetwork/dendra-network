package emission

import (
	"context"
	"cosmossdk.io/math"
	storetypes "cosmossdk.io/store/types"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	"github.com/cosmos/cosmos-sdk/codec"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/testutil"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
	"testing"
)

type moduleTestBank struct{}

func (moduleTestBank) SpendableCoins(context.Context, sdk.AccAddress) sdk.Coins {
	return sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(keeper.GenesisReserveU)))
}
func (moduleTestBank) SendCoinsFromModuleToModule(context.Context, string, string, sdk.Coins) error {
	return nil
}
func (moduleTestBank) SendCoinsFromModuleToAccount(context.Context, string, sdk.AccAddress, sdk.Coins) error {
	return nil
}
func (moduleTestBank) GetSupply(context.Context, string) sdk.Coin {
	return sdk.NewCoin("udndr", math.ZeroInt())
}

func TestGenesisInitAndExportReserve(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	key := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, key, storetypes.NewTransientStoreKey("transient_emission_module")).Ctx
	k := keeper.NewKeeper(runtime.NewKVStoreService(key), cdc, addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix()), authtypes.NewModuleAddress(types.GovModuleName), moduleTestBank{})
	m := NewAppModule(cdc, k, nil, nil)
	require.Panics(t, func() { m.InitGenesis(ctx, cdc, []byte("{")) })
	raw, err := cdc.MarshalJSON(types.DefaultGenesis())
	require.NoError(t, err)
	m.InitGenesis(ctx, cdc, raw)
	out := m.ExportGenesis(ctx, cdc)
	var got types.GenesisState
	require.NoError(t, cdc.UnmarshalJSON(out, &got))
	require.NotNil(t, got.State)
	require.Equal(t, keeper.GenesisReserveU, got.State.Reserve)
	require.Zero(t, got.State.WorkPool)
}
