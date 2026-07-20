package types_test

import (
	"testing"

	"dendra/x/jobs/types"

	"github.com/stretchr/testify/require"
)

func TestGenesisState_Validate(t *testing.T) {
	tests := []struct {
		desc     string
		genState *types.GenesisState
		valid    bool
	}{
		{
			desc:     "default is valid",
			genState: types.DefaultGenesis(),
			valid:    true,
		},
		{
			desc: "valid genesis state",
			genState: &types.GenesisState{MinerMap: []types.Miner{{MinerId: "0"}, {MinerId: "1"}}, JobMap: []types.Job{{JobId: "0"}, {JobId: "1"}}, Pools: &types.Pools{MinerPaid: 67,
				Validators: 69,
				Team:       8,
				Treasury:   85,
			}, CommitMap: []types.Commit{{JobId: "0"}, {JobId: "1"}}, BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}},
			valid: true,
		}, {
			desc: "duplicated miner",
			genState: &types.GenesisState{
				MinerMap: []types.Miner{
					{
						MinerId: "0",
					},
					{
						MinerId: "0",
					},
				},
				JobMap: []types.Job{{JobId: "0"}, {JobId: "1"}}, Pools: &types.Pools{MinerPaid: 67,
					Validators: 69,
					Team:       8,
					Treasury:   85,
				}, CommitMap: []types.Commit{{JobId: "0"}, {JobId: "1"}}, BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}},
			valid: false,
		}, {
			desc: "duplicated job",
			genState: &types.GenesisState{
				JobMap: []types.Job{
					{
						JobId: "0",
					},
					{
						JobId: "0",
					},
				},
				Pools: &types.Pools{MinerPaid: 67,
					Validators: 69,
					Team:       8,
					Treasury:   85,
				}, CommitMap: []types.Commit{{JobId: "0"}, {JobId: "1"}}, BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}},
			valid: false,
		}, {
			desc: "duplicated commit",
			genState: &types.GenesisState{
				CommitMap: []types.Commit{
					{
						JobId: "0",
					},
					{
						JobId: "0",
					},
				},
				BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}},
			valid: false,
		}, {
			desc: "duplicated beacon",
			genState: &types.GenesisState{
				BeaconMap: []types.Beacon{
					{
						JobId: "0",
					},
					{
						JobId: "0",
					},
				},
			},
			valid: false,
		},
	}
	for _, tc := range tests {
		t.Run(tc.desc, func(t *testing.T) {
			err := tc.genState.Validate()
			if tc.valid {
				require.NoError(t, err)
			} else {
				require.Error(t, err)
			}
		})
	}
}
