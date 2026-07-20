package keeper_test

import (
	"testing"

	"dendra/x/modelregistry/types"

	"github.com/stretchr/testify/require"
)

func TestGenesis(t *testing.T) {
	genesisState := types.GenesisState{
		Params: types.DefaultParams(),
		// modèles canoniques épinglés au genesis (inférence + embedder DOC-13)
		Models: []types.Model{
			{Id: "llama3.1:8b-instruct-q4_K_M", WeightsSha256: "sha-llama", Quant: "Q4_K_M", Engine: "ollama", HwClass: "consumer-gpu", Active: true},
			{Id: "all-MiniLM-L6-v2", WeightsSha256: "sha-embed", Engine: "sentence-transformers", Active: true},
		},
	}
	require.NoError(t, genesisState.Validate())

	f := initFixture(t)
	err := f.keeper.InitGenesis(f.ctx, genesisState)
	require.NoError(t, err)

	// modèles semés -> présents + actifs via IsActive (la garde exacte utilisée par x/jobs CreateCommit)
	require.True(t, f.keeper.IsActive(f.ctx, "llama3.1:8b-instruct-q4_K_M"))
	require.True(t, f.keeper.IsActive(f.ctx, "all-MiniLM-L6-v2"))
	require.False(t, f.keeper.IsActive(f.ctx, "modele-inconnu"))

	got, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.NotNil(t, got)
	require.EqualExportedValues(t, genesisState.Params, got.Params)
	require.Len(t, got.Models, 2) // round-trip des modèles
}

// Validate refuse un genesis incohérent (id vide / doublon).
func TestGenesisValidateModels(t *testing.T) {
	dup := types.GenesisState{Params: types.DefaultParams(), Models: []types.Model{{Id: "x"}, {Id: "x"}}}
	require.Error(t, dup.Validate())
	empty := types.GenesisState{Params: types.DefaultParams(), Models: []types.Model{{Id: ""}}}
	require.Error(t, empty.Validate())
}
