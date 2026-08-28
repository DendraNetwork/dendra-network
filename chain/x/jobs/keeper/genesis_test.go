package keeper_test

import (
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"github.com/stretchr/testify/require"
)

func TestGenesis(t *testing.T) {
	// Registry entries the chain could actually have produced: since ADR-039 the identifier is derived
	// from the creator on the genesis path too, so `{MinerId: "0"}` describes a state no code path
	// reaches.
	//
	// SORTED, because ExportGenesis walks the store in KEY order while a derived identifier is a hash:
	// insertion order and lexical order have no reason to agree, and the fixtures that used "0" and "1"
	// hid that by being ordered already. Without this the test would fail on the accident of a first
	// character rather than on anything it means to check.
	mA, mB := registeredMiner(t, 0x41, 0), registeredMiner(t, 0x51, 0)
	if mA.MinerId > mB.MinerId {
		mA, mB = mB, mA
	}
	genesisState := types.GenesisState{
		Params:   types.DefaultParams(),
		MinerMap: []types.Miner{mA, mB}, JobMap: []types.Job{{JobId: "0"}, {JobId: "1"}}, Pools: &types.Pools{MinerPaid: 94,
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
