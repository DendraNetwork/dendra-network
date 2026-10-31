package jobs

import (
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	sdkmodule "github.com/cosmos/cosmos-sdk/types/module"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"testing"
)

type countingRegistrar struct{ names []string }

func (r *countingRegistrar) RegisterService(desc *grpc.ServiceDesc, _ interface{}) {
	r.names = append(r.names, desc.ServiceName)
}

func TestRegisterServicesAddsQueriesMessagesAndMigration(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	m := NewAppModule(cdc, keeper.Keeper{}, nil, nil)
	r := &countingRegistrar{}
	require.Empty(t, r.names)
	require.NoError(t, m.RegisterServices(r))
	require.Len(t, r.names, 2)
	messages, queries := &countingRegistrar{}, &countingRegistrar{}
	cfg := sdkmodule.NewConfigurator(cdc, messages, queries)
	require.NoError(t, m.RegisterServices(cfg))
	require.Len(t, messages.names, 1)
	require.Len(t, queries.names, 1)
	err := cfg.RegisterMigration(types.ModuleName, 1, keeper.NewMigrator(keeper.Keeper{}).Migrate1to2)
	require.Error(t, err)
}
