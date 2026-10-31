package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Double slash of the k=3 committee.
//
// VerifySemantic and FinalizeJob slash the cosine outliers of a k=3 committee. Each one recognised only
// its own anti-replay marker (`+verified` / `+finalized`), so `settle(k3) -> verify` and then
// `verify -> finalize` slashed the same outlier again (80% of the remainder on each pass). Nothing
// disabled them in optimistic mode either, where a permissionless attacker could slash honest
// non-primary miners (cosine is no judge there). Two locks: a mode-0 gate plus a unified anti-replay
// (jobK3Adjudicated).
//
// For months this file proved none of it. Every case planted a job with no registered miner and no
// commit, so `assignedCommittee` returned an empty set without error, the walk found nothing, and both
// handlers refused on "no commit from an assigned miner for this job", a fallback unrelated to either
// lock. Measured one guard at a time, by replacing its condition with `if false` (the mode gate of
// VerifySemantic, its unified anti-replay, the mode gate of FinalizeJob, its unified anti-replay): four
// mutations, and four times the whole file stayed green. A case that asserts "X is refused" measures
// the first refusal on its fixture, not the one it is named after.
//
// So each case now sets up a real committee (three miners, two agreeing commits and one divergent), a
// fixture on which the handler succeeds when the guard is absent. And the conclusion is read off the
// outlier's bond, never off the error: `no commit from an assigned miner`, `no strict majority` and the
// two locks all return the same `ErrInvalidRequest`, so an error assertion cannot tell them apart.
// Stake 1000 intact means the guard refused before touching anything; 200 means it did not.
//
// The last case is the control that makes the other four readable: the same fixture, in the one
// configuration where nothing forbids it, does slash. Without it, "the bond is intact" could not be
// told apart from "this fixture never slashes".

// dsCommittee plants a full k=3 committee on `jobID` (two agreeing commits and one divergent, the shape
// both handlers judge) and returns the outlier's id. It uses gvSetMiner/gvSetJob rather than the raw
// collections because those set `MinerRegisteredHeight` and `JobPoolFreezeHeight`; without them the
// frozen-pool and vitality predicates empty the committee and the fallback refusal returns.
func dsCommittee(t *testing.T, f *fixture, jobID, state string) string {
	t.Helper()
	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: ssAddr20(t, f, "ds-creator-"+id),
			Operator: ssAddr20(t, f, "ds-operator-"+id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, State: state, Fee: 10000, Client: ssAddr20(t, f, "ds-client-"+jobID),
	}))
	return "mc"
}

// dsIntact asserts the outlier was not touched, the only reading that separates this guard from the
// three other refusals the same handler can return.
func dsIntact(t *testing.T, f *fixture, id, what string) {
	t.Helper()
	m, err := f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake,
		"%s: the refusal must land before any bond moves (200 would mean the handler judged)", what)
}

func dsCreator(f *fixture, t *testing.T) string {
	t.Helper()
	c, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("trigger_ds_2107_____")))
	require.NoError(t, err)
	return c
}

// (1) Optimistic mode: verify-semantic is refused (cosine plays no role; the VRF audit does the job).
func TestVerifySemanticRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.CommitteeRevealDelay = 1 // Validate refuses mode 1 with a zero reveal delay
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsCommittee(t, f, "jVS", "open")

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jVS"})
	require.Error(t, err, "verify-semantic must be refused in optimistic mode (griefing of honest non-primary miners)")
	dsIntact(t, f, outlier, "verify in optimistic mode")
}

// (2) Anti-double-slash: a job that is already settled (paid+finalized) can no longer be slashed again
// by verify-semantic (mode 0). A guard that tests only `+verified` lets settle then verify slash twice.
func TestVerifySemanticRefusedOnAlreadySettledJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsCommittee(t, f, "jPaid", "open+paid+finalized")

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jPaid"})
	require.Error(t, err, "a k=3 committee already adjudicated (settled/verified/finalized) must not be re-slashed")
	dsIntact(t, f, outlier, "settle -> verify")
}

// (3) Symmetry: finalize-job shares both locks.
func TestFinalizeJobRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.CommitteeRevealDelay = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsCommittee(t, f, "jFJ", "open")

	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jFJ"})
	require.Error(t, err, "finalize-job must be refused in optimistic mode (same griefing as verify-semantic)")
	dsIntact(t, f, outlier, "finalize in optimistic mode")
}

func TestFinalizeJobRefusedOnAlreadyVerifiedJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	srv := keeper.NewMsgServerImpl(f.keeper)
	// Job already verified (verify applied the k=3 slash), so finalize must not slash again.
	outlier := dsCommittee(t, f, "jVerified", "open+verified")

	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jVerified"})
	require.Error(t, err, "verify -> finalize must not re-slash the same outlier (unified anti-replay)")
	dsIntact(t, f, outlier, "verify -> finalize")
}

// (5) The control, which gives the four cases above their meaning: the same fixture, mode 0, on a job
// no lock applies to. Both handlers judge it and the outlier loses slash_leak_bps of its bond. If this
// case ever goes red, the four refusals above stop proving anything: they would be measuring a
// committee that cannot be judged rather than a guard that refuses to judge it.
func TestTheSameFixtureIsReallyJudgedWhenNoLockApplies(t *testing.T) {
	p := types.DefaultParams()
	expected := uint64(1000) - uint64(1000)*p.SlashLeakBps/10000

	t.Run("verify", func(t *testing.T) {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		srv := keeper.NewMsgServerImpl(f.keeper)
		outlier := dsCommittee(t, f, "jOkV", "open")

		_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jOkV"})
		require.NoError(t, err, "mode 0 on a clean job: nothing forbids the judgement")
		m, err := f.keeper.Miner.Get(f.ctx, outlier)
		require.NoError(t, err)
		require.Equal(t, expected, m.Stake, "the outlier is slashed here, so the fixture is a success case")
	})

	t.Run("finalize", func(t *testing.T) {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		srv := keeper.NewMsgServerImpl(f.keeper)
		outlier := dsCommittee(t, f, "jOkF", "open")

		_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jOkF"})
		require.NoError(t, err, "mode 0 on a clean job: nothing forbids the verdict")
		m, err := f.keeper.Miner.Get(f.ctx, outlier)
		require.NoError(t, err)
		require.Equal(t, expected, m.Stake, "same for finalize -- both handlers really do slash this fixture")
	})
}
