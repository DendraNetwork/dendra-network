package modelregistry

import (
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestValidateGenesisRejectsMissingAndDuplicateModelIDs(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	m := NewAppModule(cdc, keeper.Keeper{}, nil, nil)
	require.Error(t, m.ValidateGenesis(cdc, nil, []byte("{")))
	g := types.DefaultGenesis()
	raw, err := cdc.MarshalJSON(g)
	require.NoError(t, err)
	require.NoError(t, m.ValidateGenesis(cdc, nil, raw))
	g.Models = []types.Model{{Id: ""}}
	raw, err = cdc.MarshalJSON(g)
	require.NoError(t, err)
	require.Error(t, m.ValidateGenesis(cdc, nil, raw))
	g.Models = []types.Model{{Id: "x"}, {Id: "x"}}
	raw, err = cdc.MarshalJSON(g)
	require.NoError(t, err)
	require.Error(t, m.ValidateGenesis(cdc, nil, raw))
}
