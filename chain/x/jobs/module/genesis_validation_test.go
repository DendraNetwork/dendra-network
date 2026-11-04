package jobs

import (
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestValidateGenesisRejectsMalformedAndDuplicateJobs(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	m := NewAppModule(cdc, keeper.Keeper{}, nil, nil)
	require.Error(t, m.ValidateGenesis(cdc, nil, []byte("{")))
	good := types.DefaultGenesis()
	raw, err := cdc.MarshalJSON(good)
	require.NoError(t, err)
	require.NoError(t, m.ValidateGenesis(cdc, nil, raw))
	good.JobMap = []types.Job{{JobId: "x"}, {JobId: "x"}}
	raw, err = cdc.MarshalJSON(good)
	require.NoError(t, err)
	require.Error(t, m.ValidateGenesis(cdc, nil, raw))
}
