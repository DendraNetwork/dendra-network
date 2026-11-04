package jobs

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
	"testing"
)

func TestGenesisInitAndExportJobs(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	key := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, key, storetypes.NewTransientStoreKey("transient_jobs_module")).Ctx
	k := keeper.NewKeeper(runtime.NewKVStoreService(key), cdc, addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix()), authtypes.NewModuleAddress(types.GovModuleName), nil, nil, nil)
	m := NewAppModule(cdc, k, nil, nil)
	require.Panics(t, func() { m.InitGenesis(ctx, cdc, []byte("{")) })
	g := types.DefaultGenesis()
	g.JobMap = []types.Job{{JobId: "job", State: "open"}}
	raw, err := cdc.MarshalJSON(g)
	require.NoError(t, err)
	m.InitGenesis(ctx, cdc, raw)
	out := m.ExportGenesis(ctx, cdc)
	var got types.GenesisState
	require.NoError(t, cdc.UnmarshalJSON(out, &got))
	require.Len(t, got.JobMap, 1)
	require.Equal(t, "job", got.JobMap[0].JobId)
}
