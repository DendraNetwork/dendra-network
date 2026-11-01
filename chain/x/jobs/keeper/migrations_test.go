package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	module "github.com/DendraNetwork/dendra-network/chain/x/jobs/module"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The epoch 15 migration writes the five new parameters at 0 and nothing else.
//
// The stored set of an epoch-14 node has no bytes for the new fields, so it already reads 0; the case
// starts from a set that carries non-zero values there, which is the only way to see the migration
// write anything, and checks that every other field is untouched and that the result is byte for byte
// the epoch-14 encoding.
func TestEpoch15MigrationWritesTheNewParametersAtZeroAndNothingElse(t *testing.T) {
	f := initFixture(t)
	stored := types.DefaultParams()
	stored.MinStake = 4242
	stored.AuditResolveTimeout = 999
	stored.JurorFreshnessBlocks = 777
	stored.TeamAddress = ssAddr20(t, f, "team-e15")
	epoch14Bytes, err := stored.Marshal()
	require.NoError(t, err)

	tampered := stored
	tampered.MinKitVersion = 9
	tampered.JudgeVerdictWindowBlocks = 999999
	tampered.JudgeMissedDrawsMax = 9
	tampered.AvailSampleBps = 9
	tampered.CommitteeMinVrfPowerBps = 9999
	require.NoError(t, f.keeper.Params.Set(f.ctx, tampered))

	require.NoError(t, keeper.NewMigrator(f.keeper).Migrate1to2(sdk.UnwrapSDKContext(f.ctx)))

	after, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, stored, after, "the five fields are 0 and every other field is as stored")
	afterBytes, err := after.Marshal()
	require.NoError(t, err)
	require.Equal(t, epoch14Bytes, afterBytes, "dormant zeros cost no bytes: the migrated set is the epoch-14 encoding")
}

func TestEpoch15MovesTheConsensusVersionOfJobs(t *testing.T) {
	require.Equal(t, uint64(2), module.AppModule{}.ConsensusVersion(),
		"the version is what makes RunMigrations run Migrate1to2 at the upgrade height")
}
