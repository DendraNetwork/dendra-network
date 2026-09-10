package keeper_test

import (
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"

	"github.com/stretchr/testify/require"
)

func TestGenesis(t *testing.T) {
	genesisState := types.GenesisState{
		Params: types.DefaultParams(),
		// canonical models pinned at genesis: one inference model, one embedder
		Models: []types.Model{
			{Id: "llama3.1:8b-instruct-q4_K_M", WeightsSha256: "sha-llama", Quant: "Q4_K_M", Engine: "ollama", HwClass: "consumer-gpu", Active: true},
			{Id: "all-MiniLM-L6-v2", WeightsSha256: "sha-embed", Engine: "sentence-transformers", Active: true},
		},
	}
	require.NoError(t, genesisState.Validate())

	f := initFixture(t)
	err := f.keeper.InitGenesis(f.ctx, genesisState)
	require.NoError(t, err)

	// seeded models are present and active through IsActive — the exact guard x/jobs CreateCommit uses
	require.True(t, f.keeper.IsActive(f.ctx, "llama3.1:8b-instruct-q4_K_M"))
	require.True(t, f.keeper.IsActive(f.ctx, "all-MiniLM-L6-v2"))
	require.False(t, f.keeper.IsActive(f.ctx, "modele-inconnu"))

	got, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.NotNil(t, got)
	require.EqualExportedValues(t, genesisState.Params, got.Params)
	require.Len(t, got.Models, 2) // the models survive the round-trip
}

// Validate refuses an inconsistent genesis: empty id, or a duplicate id.
func TestGenesisValidateModels(t *testing.T) {
	dup := types.GenesisState{Params: types.DefaultParams(), Models: []types.Model{{Id: "x"}, {Id: "x"}}}
	require.Error(t, dup.Validate())
	empty := types.GenesisState{Params: types.DefaultParams(), Models: []types.Model{{Id: ""}}}
	require.Error(t, empty.Validate())
}
