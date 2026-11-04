package simulation

import (
	"context"
	coreaddress "cosmossdk.io/core/address"
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

type simAuth struct{ ac coreaddress.Codec }

func (a simAuth) AddressCodec() coreaddress.Codec                         { return a.ac }
func (a simAuth) GetAccount(context.Context, sdk.AccAddress) sdk.AccountI { return nil }

func TestUpdateSimulationHandlesEmptyAndUnreadableCreator(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	key := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, key, storetypes.NewTransientStoreKey("transient_sim_update")).Ctx
	ac := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	k := keeper.NewKeeper(runtime.NewKVStoreService(key), cdc, ac, authtypes.NewModuleAddress(types.GovModuleName), nil, nil, nil)
	run := SimulateMsgUpdateMiner(simAuth{ac}, nil, k, nil)
	out, future, err := run(rand.New(rand.NewSource(1)), nil, ctx, nil, "")
	require.NoError(t, err)
	require.Empty(t, future)
	require.Contains(t, out.Comment, "creator not found")
	require.NoError(t, k.Miner.Set(ctx, "bad", types.Miner{MinerId: "bad", Creator: "invalid"}))
	_, _, err = run(rand.New(rand.NewSource(1)), nil, ctx, nil, "")
	require.Error(t, err)
}
