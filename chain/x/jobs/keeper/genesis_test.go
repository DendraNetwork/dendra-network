package keeper_test

import (
	"testing"

	"dendra/x/jobs/types"

	"github.com/stretchr/testify/require"
)

func TestGenesis(t *testing.T) {
	genesisState := types.GenesisState{
		Params:   types.DefaultParams(),
		MinerMap: []types.Miner{{MinerId: "0"}, {MinerId: "1"}}, JobMap: []types.Job{{JobId: "0"}, {JobId: "1"}}, Pools: &types.Pools{MinerPaid: 94,
			Validators: 41,
			Team:       46,
			Treasury:   80,
		}, CommitMap: []types.Commit{{JobId: "0"}, {JobId: "1"}}, BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}}

	f := initFixture(t)
	err := f.keeper.InitGenesis(f.ctx, genesisState)
	require.NoError(t, err)
	got, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.NotNil(t, got)

	require.EqualExportedValues(t, genesisState.Params, got.Params)
	require.EqualExportedValues(t, genesisState.MinerMap, got.MinerMap)
	require.EqualExportedValues(t, genesisState.JobMap, got.JobMap)
	require.EqualExportedValues(t, genesisState.Pools, got.Pools)
	require.EqualExportedValues(t, genesisState.CommitMap, got.CommitMap)
	require.EqualExportedValues(t, genesisState.BeaconMap, got.BeaconMap)

}
