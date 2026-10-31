package emission

import (
	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestValidateGenesisReleaseBoundaryAndZero(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	m := NewAppModule(cdc, keeper.Keeper{}, nil, nil)
	require.Error(t, m.ValidateGenesis(cdc, nil, []byte("{")))
	g := types.DefaultGenesis()
	for _, n := range []uint64{0, 10000, 10001} {
		g.Params.ReserveReleaseBps = n
		raw, err := cdc.MarshalJSON(g)
		require.NoError(t, err)
		err = m.ValidateGenesis(cdc, nil, raw)
		if n <= 10000 {
			require.NoError(t, err)
		} else {
			require.Error(t, err)
		}
	}
}
