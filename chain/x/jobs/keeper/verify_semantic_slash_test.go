package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The S2 verification handler slashes, and its judging body had never run.
//
// Coverage showed 58 of the 95 statements of `msg_server_verify_semantic.go` at count 0. The two early
// refusals were exercised (`double_settlement_guard_test.go` drives the optimistic-mode refusal and the
// already-settled one). What had never run is the body that judges: the committee walk, the
// strict-majority computation, the outlier slash and its trace, plus the error branches.
//
// Four of the five guards the handler's header documents live in that body: full committee, strict
// majority, fixed threshold, and anti-replay after a verdict. The cases below cross each of them.
//
// What it decides: a committee of three, a strict cosine majority, and every outlier loses
// `slash_leak_bps` of its stake. There is no payment here, only punishment, so a wrong guard costs an
// honest miner 80 % of its bond.

// vsJob wires a job plus a full committee of vector commits, and returns the miner ids.
func vsJob(t *testing.T, f *fixture, jobID string, commits map[string]string) {
	t.Helper()
	for id, c := range commits {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: ssAddr20(t, f, "creator-"+id), Operator: ssAddr20(t, f, "operator-"+id),
			Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: c}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{JobId: jobID, Fee: 10000}))
}

func TestVerifySemanticSlashesTheOutlierAndTracesIt(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVS", map[string]string{"va": "1,2,3", "vb": "1,2,3", "vc": "-1,-2,-3"})

	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	expected := uint64(1000) * p.SlashLeakBps / 10000

	_, err = srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVS",
	})
	require.NoError(t, err)

	for _, id := range []string{"va", "vb"} {
		m, mErr := f.keeper.Miner.Get(f.ctx, id)
		require.NoError(t, mErr)
		require.Equal(t, uint64(1000), m.Stake, "a miner inside the majority is not slashed (%s)", id)
	}
	vc, err := f.keeper.Miner.Get(f.ctx, "vc")
	require.NoError(t, err)
	require.Equal(t, uint64(1000)-expected, vc.Stake, "the outlier loses slash_leak_bps of its stake")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, expected, pools.Treasury)

	job, err := f.keeper.Job.Get(f.ctx, "jVS")
	require.NoError(t, err)
	require.Contains(t, job.State, "+verified")
	// The trace matters: `ResolveDispute` and `AdjudicateDispute` only iterate `job.SlashRecords`, so
	// without this entry an honest miner slashed here and later cleared would never be refunded.
	// The restitution it enables is bounded: `ResolveDispute` clamps the re-credit to the Treasury balance,
	// so a miner cleared after the Treasury is drained gets back less than it lost, on a call that still
	// succeeds (`dispute_restitution_bound_test.go`). The trace is necessary for restitution; it does not
	// guarantee it.
	require.Len(t, job.SlashRecords, 1, "the slash must be traced or a successful dispute restores nothing")
	require.Equal(t, "vc", job.SlashRecords[0].MinerId)
	require.Equal(t, expected, job.SlashRecords[0].Amount)
}

// No strict majority, nobody slashed. This is the guard that, in the handler's header, "rules out an
// arbitrary slash": three mutually orthogonal answers must punish no one.
func TestVerifySemanticSlashesNobodyWithoutAStrictMajority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVSNo", map[string]string{"na": "1,0,0", "nb": "0,1,0", "nc": "0,0,1"})

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVSNo",
	})
	require.Error(t, err, "no strict majority must refuse, not slash")

	for _, id := range []string{"na", "nb", "nc"} {
		m, mErr := f.keeper.Miner.Get(f.ctx, id)
		require.NoError(t, mErr)
		require.Equal(t, uint64(1000), m.Stake, "nobody is slashed when no majority forms (%s)", id)
	}
	job, err := f.keeper.Job.Get(f.ctx, "jVSNo")
	require.NoError(t, err)
	require.NotContains(t, job.State, "+verified", "and the job is not marked verified on a refusal")
	require.Empty(t, job.SlashRecords)
}

// An incomplete committee is refused. With two answers instead of three, `required` would fall to 2 and
// a single pair could condemn the third.
func TestVerifySemanticRefusesAnIncompleteCommittee(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVSInc", map[string]string{"ia": "1,2,3", "ib": "1,2,3"})

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVSInc",
	})
	require.Error(t, err, "a committee smaller than CommitteeSize must not be able to judge")

	for _, id := range []string{"ia", "ib"} {
		m, mErr := f.keeper.Miner.Get(f.ctx, id)
		require.NoError(t, mErr)
		require.Equal(t, uint64(1000), m.Stake)
	}
}

// Anti-replay: without it "anyone could replay VerifySemantic to grind an honest miner's stake down to
// zero" (the handler's header). The second call must refuse, and the unchanged stake proves it.
func TestVerifySemanticCannotBeReplayedToGrindAStake(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVSRep", map[string]string{"ra": "1,2,3", "rb": "1,2,3", "rc": "-1,-2,-3"})
	msg := &types.MsgVerifySemantic{Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVSRep"}

	_, err := srv.VerifySemantic(f.ctx, msg)
	require.NoError(t, err)
	after, err := f.keeper.Miner.Get(f.ctx, "rc")
	require.NoError(t, err)

	_, err = srv.VerifySemantic(f.ctx, msg)
	require.Error(t, err, "a judged committee must not be judged again")

	replayed, err := f.keeper.Miner.Get(f.ctx, "rc")
	require.NoError(t, err)
	require.Equal(t, after.Stake, replayed.Stake,
		"the replay must not take a second bite: this is the grind the guard exists to stop")
	job, err := f.keeper.Job.Get(f.ctx, "jVSRep")
	require.NoError(t, err)
	require.Len(t, job.SlashRecords, 1, "and it must not record the same slash twice")
}

// Disabled in optimistic mode. Production runs mode 1, where verification goes through the VRF audit;
// leaving this permissionless slasher reachable there would give a second, unaudited way to punish. The
// refusal must come before anything is read or moved.
func TestVerifySemanticIsRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVSOpt", map[string]string{"oa": "1,2,3", "ob": "1,2,3", "oc": "-1,-2,-3"})

	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	p.VerificationMode = 1
	p.CommitteeRevealDelay = 1 // Validate() refuses mode 1 with a zero reveal delay
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	_, err = srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVSOpt",
	})
	require.Error(t, err, "verify-semantic must be closed in optimistic mode")

	oc, err := f.keeper.Miner.Get(f.ctx, "oc")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), oc.Stake, "and the refusal must happen before any stake moves")
}
