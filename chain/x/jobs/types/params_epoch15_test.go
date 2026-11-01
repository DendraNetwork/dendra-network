package types_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The five fields epoch 15 added. Each bound is pinned on both sides of its edge, and the
// zero value of each must always validate: a genesis that omits them is a genesis from before epoch 15.

func TestEpoch15Params_ZeroIsAlwaysValid(t *testing.T) {
	require.NoError(t, types.DefaultParams().Validate())
	require.NoError(t, e14Mode1().Validate())
	p := types.DefaultParams()
	require.Zero(t, p.MinKitVersion)
	require.Zero(t, p.JudgeVerdictWindowBlocks)
	require.Zero(t, p.JudgeMissedDrawsMax)
	require.Zero(t, p.AvailSampleBps)
	require.Zero(t, p.CommitteeMinVrfPowerBps)
}

func TestEpoch15Params_JudgeWindowIsNotBelowAPostedFreshness(t *testing.T) {
	p := types.DefaultParams()
	p.JurorFreshnessBlocks = 1000
	p.JudgeVerdictWindowBlocks = 999
	require.Error(t, p.Validate(), "a judge would lose its seat by the window while vitality still summons it")
	p.JudgeVerdictWindowBlocks = 1000
	require.NoError(t, p.Validate(), "equal is the boundary and it is allowed")
	p.JudgeVerdictWindowBlocks = 0
	require.NoError(t, p.Validate(), "0 selects the freshness window itself")
	p.JurorFreshnessBlocks = 0
	p.JudgeVerdictWindowBlocks = 1
	require.NoError(t, p.Validate(), "a pair with a zero compares nothing: the dormant pattern")
}

func TestEpoch15Params_AvailSampleBpsIsAShare(t *testing.T) {
	p := types.DefaultParams()
	p.AvailSampleBps = 10000
	require.NoError(t, p.Validate())
	p.AvailSampleBps = 10001
	require.Error(t, p.Validate())
}

func TestEpoch15Params_ThePowerBarMovesUpOnly(t *testing.T) {
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfPowerBps = types.VrfContributorPowerFloorBps - 1
	require.Error(t, p.Validate(), "below two thirds the BFT bar would be lowered by a vote")
	p.CommitteeMinVrfPowerBps = types.VrfContributorPowerFloorBps
	require.NoError(t, p.Validate())
	p.CommitteeMinVrfPowerBps = 10000
	require.NoError(t, p.Validate())
	p.CommitteeMinVrfPowerBps = 10001
	require.Error(t, p.Validate())

	p.CommitteeMinVrfPowerBps = 7000
	p.CommitteeSeedSource = 0
	require.Error(t, p.Validate(), "posted without the decentralised seed the bar is never evaluated: advertised and inert")
}

func TestEpoch15Params_TheCountAndTheKitNumberHaveNoUpperBound(t *testing.T) {
	p := types.DefaultParams()
	p.JudgeMissedDrawsMax = 1
	require.NoError(t, p.Validate(), "1 is the strictest posted count")
	p.JudgeMissedDrawsMax = 1_000_000_000
	require.NoError(t, p.Validate(), "a high count only weakens the exclusion by count; the window still applies")
	p.MinKitVersion = 1_000_000_000
	require.NoError(t, p.Validate(), "the chain cannot know the kit numbers, so it bounds none")
}
