package simulation

import (
	"context"
	storetypes "cosmossdk.io/store/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/testutil"
	sdk "github.com/cosmos/cosmos-sdk/types"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
	"math/rand"
	"testing"
)

type emptySimulationBank struct{}

func (emptySimulationBank) SpendableCoins(context.Context, sdk.AccAddress) sdk.Coins {
	return sdk.NewCoins()
}
func (emptySimulationBank) SendCoins(context.Context, sdk.AccAddress, sdk.AccAddress, sdk.Coins) error {
	return nil
}
func (emptySimulationBank) SendCoinsFromAccountToModule(context.Context, sdk.AccAddress, string, sdk.Coins) error {
	return nil
}
func (emptySimulationBank) SendCoinsFromModuleToAccount(context.Context, string, sdk.AccAddress, sdk.Coins) error {
	return nil
}
func (emptySimulationBank) SendCoinsFromModuleToModule(context.Context, string, string, sdk.Coins) error {
	return nil
}
func (emptySimulationBank) BurnCoins(context.Context, string, sdk.Coins) error { return nil }

func TestCreateSimulationChecksExistingAndUnfundedAccounts(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	key := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, key, storetypes.NewTransientStoreKey("transient_sim_create")).Ctx
	ac := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	k := keeper.NewKeeper(runtime.NewKVStoreService(key), cdc, ac, authtypes.NewModuleAddress(types.GovModuleName), nil, nil, nil)
	account := simtypes.Account{Address: sdk.AccAddress([]byte("simulation-test-addr"))}
	id, err := types.DeriveMinerID(account.Address.Bytes())
	require.NoError(t, err)
	run := func() simtypes.OperationMsg {
		out, _, e := SimulateMsgCreateMiner(nil, emptySimulationBank{}, k, nil)(rand.New(rand.NewSource(1)), nil, ctx, []simtypes.Account{account}, "")
		require.NoError(t, e)
		return out
	}
	require.NoError(t, k.Miner.Set(ctx, id, types.Miner{MinerId: id}))
	require.Contains(t, run().Comment, "already exist")
	require.NoError(t, k.Miner.Remove(ctx, id))
	require.Contains(t, run().Comment, "params unreadable")
	require.NoError(t, k.Params.Set(ctx, types.DefaultParams()))
	require.Contains(t, run().Comment, "bond not fundable")
	_, _, err = SimulateMsgCreateMiner(nil, emptySimulationBank{}, k, nil)(rand.New(rand.NewSource(1)), nil, ctx, []simtypes.Account{{}}, "")
	require.Error(t, err)
}
