package jobs

import (
	"encoding/json"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestSimulationGenesisRequiresAccountsAndReusesSeededIdentities(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	m := AppModule{}
	zero := module.SimulationState{Cdc: cdc, GenState: map[string]json.RawMessage{}}
	require.Panics(t, func() { m.GenerateGenesisState(&zero) })
	accounts := []simtypes.Account{{Address: sdk.AccAddress([]byte("simulation-address01"))}, {Address: sdk.AccAddress([]byte("simulation-address02"))}}
	state := module.SimulationState{Cdc: cdc, Accounts: accounts, GenState: map[string]json.RawMessage{}}
	m.GenerateGenesisState(&state)
	var got types.GenesisState
	require.NoError(t, cdc.UnmarshalJSON(state.GenState[types.ModuleName], &got))
	require.NoError(t, got.Validate())
	require.Len(t, got.MinerMap, 2)
	require.NotEqual(t, got.MinerMap[0].MinerId, got.MinerMap[1].MinerId)
	other := module.SimulationState{Cdc: cdc, Accounts: accounts, GenState: map[string]json.RawMessage{}}
	m.GenerateGenesisState(&other)
	require.Equal(t, state.GenState[types.ModuleName], other.GenState[types.ModuleName])
}
