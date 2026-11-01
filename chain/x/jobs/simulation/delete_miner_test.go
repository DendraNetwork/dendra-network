package simulation

import (
	storetypes "cosmossdk.io/store/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/testutil"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
	"math/rand"
	"testing"
)

func TestDeleteSimulationNamesDeleteMessageWhenRegistryEmpty(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	key := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, key, storetypes.NewTransientStoreKey("transient_sim_delete")).Ctx
	ac := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	k := keeper.NewKeeper(runtime.NewKVStoreService(key), cdc, ac, authtypes.NewModuleAddress(types.GovModuleName), nil, nil, nil)
	msg, future, err := SimulateMsgDeleteMiner(nil, nil, k, nil)(rand.New(rand.NewSource(1)), nil, ctx, nil, "")
	require.NoError(t, err)
	require.Empty(t, future)
	require.Equal(t, sdk.MsgTypeURL(&types.MsgDeleteMiner{}), msg.Name)
}
