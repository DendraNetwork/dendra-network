package jobs

import (
	storetypes "cosmossdk.io/store/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestProvideModuleSelectsDefaultAndConfiguredAuthority(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	ac := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	custom := authtypes.NewModuleAddress("other-authority")
	customString, err := ac.BytesToString(custom)
	require.NoError(t, err)
	for _, tc := range []struct {
		value string
		want  []byte
	}{{"", authtypes.NewModuleAddress(types.GovModuleName)}, {customString, custom}} {
		in := ModuleInputs{Config: &types.Module{Authority: tc.value}, StoreService: runtime.NewKVStoreService(storetypes.NewKVStoreKey(types.StoreKey)), Cdc: cdc, AddressCodec: ac}
		out := ProvideModule(in)
		require.Equal(t, tc.want, out.JobsKeeper.GetAuthority())
		require.NotNil(t, out.Module)
	}
}
