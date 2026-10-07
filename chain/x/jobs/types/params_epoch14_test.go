package types_test

import (
	"testing"

	"github.com/cosmos/cosmos-sdk/types/bech32"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-048 — the five fields epoch 14 added. Each bound is pinned on BOTH sides of its edge, and the zero
// value of each must always validate: a genesis that omits them is a genesis from before epoch 14.

func e14Mode1() types.Params {
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.DisputeBond = p.MinStake
	p.CommitteeRevealDelay = 1
	return p
}

func TestEpoch14Params_ZeroIsAlwaysValid(t *testing.T) {
	require.NoError(t, types.DefaultParams().Validate())
	require.NoError(t, e14Mode1().Validate())
}

func TestEpoch14Params_TeamAddress(t *testing.T) {
	enc := func(n int) string {
		s, err := bech32.ConvertAndEncode("dm", make([]byte, n))
		require.NoError(t, err)
		return s
	}
	p := types.DefaultParams()
	for _, ok := range []string{enc(20), enc(32)} {
		p.TeamAddress = ok
		require.NoError(t, p.Validate(), ok)
	}
	for _, bad := range []string{"team", enc(10), enc(20) + "x"} {
		p.TeamAddress = bad
		require.Error(t, p.Validate(), "%q must be refused: the team share would have nowhere to go", bad)
	}
}

func TestEpoch14Params_JobExpiry(t *testing.T) {
	p := e14Mode1()
	for v, ok := range map[uint64]bool{99: false, 100: true, 10_000_000: true, 10_000_001: false} {
		p.JobExpiryBlocks = v
		require.Equal(t, ok, p.Validate() == nil, "job_expiry_blocks=%d", v)
	}
	p.CommitteeRevealDelay = 500
	p.JobExpiryBlocks = 500
	require.Error(t, p.Validate(), "a job must not expire before its committee is revealed")
	p.JobExpiryBlocks = 501
	require.NoError(t, p.Validate())
}

func TestEpoch14Params_WorkCommitteeSize(t *testing.T) {
	p := e14Mode1()
	p.WorkCommitteeSize = 1
	require.NoError(t, p.Validate(), "optimistic mode pays one primary: 1 is legitimate")
	p.WorkCommitteeSize = 16
	require.Error(t, p.Validate())
	p.WorkCommitteeSize = 15
	require.NoError(t, p.Validate())

	r := types.DefaultParams() // redundant mode
	r.WorkCommitteeSize = 2
	require.Error(t, r.Validate(), "a strict majority of two answers is one answer")
	r.WorkCommitteeSize = 3
	require.NoError(t, r.Validate())
}

func TestEpoch14Params_StakeCapMultiple(t *testing.T) {
	p := types.DefaultParams()
	p.AssignmentStakeCapMultiple = 100
	require.NoError(t, p.Validate())
	p.AssignmentStakeCapMultiple = 101
	require.Error(t, p.Validate())
	p.AssignmentStakeCapMultiple = 1
	require.NoError(t, p.Validate(), "1 removes stake weighting: a decision ADR-042 leaves open, not an invalid set")
}

func TestEpoch14Params_AuditDrawSize(t *testing.T) {
	p := e14Mode1()
	p.AuditCommitteeDrawSize = types.AuditFallbackFloor - 1
	require.Error(t, p.Validate(), "fewer seats than the fallback floor: no audit could ever conclude")
	p.AuditCommitteeDrawSize = types.AuditFallbackFloor
	require.NoError(t, p.Validate())
	p.AuditCommitteeDrawSize = 65
	require.Error(t, p.Validate())
	p.AuditCommitteeDrawSize = 64
	require.NoError(t, p.Validate())

	p.AuditMinQuorum = 6
	p.AuditCommitteeDrawSize = 5
	require.Error(t, p.Validate(), "a governed quorum above the seats drawn makes the bar unreachable")
	p.AuditCommitteeDrawSize = 6
	require.NoError(t, p.Validate())
}
