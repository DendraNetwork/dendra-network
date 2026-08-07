package types_test

import (
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

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
			// ADR-039: a miner entry is valid only if its identifier is the one its creator derives.
			genState: &types.GenesisState{MinerMap: []types.Miner{minerEntry(t, 0x61), minerEntry(t, 0x71)}, JobMap: []types.Job{{JobId: "0"}, {JobId: "1"}}, Pools: &types.Pools{MinerPaid: 67,
				Validators: 69,
				Team:       8,
				Treasury:   85,
			}, CommitMap: []types.Commit{{JobId: "0"}, {JobId: "1"}}, BeaconMap: []types.Beacon{{JobId: "0"}, {JobId: "1"}}},
			valid: true,
		}, {
			desc: "duplicated miner",
			// BOTH ENTRIES ARE INDIVIDUALLY VALID, so DUPLICATION is what refuses them. With the
			// former `{MinerId: "0"}` fixtures the ADR-039 identity check would fire first and this
			// case would stay green while no longer testing duplication at all — a red for the wrong
			// reason reads exactly like a red for the right one.
			genState: &types.GenesisState{
				MinerMap: []types.Miner{
					minerEntry(t, 0x81),
					minerEntry(t, 0x81),
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
