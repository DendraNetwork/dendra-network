package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE S2 VERIFICATION HANDLER: IT SLASHES, AND ITS JUDGING BODY HAD NEVER RUN.
//
// Measured with `go tool cover`: 58 of the 95 statements of `msg_server_verify_semantic.go` were at
// count 0. Being precise about WHICH 58, because the loose version of this sentence would be wrong:
// the two early refusals WERE exercised (`double_settlement_guard_test.go` drives the optimistic-mode
// refusal and the already-settled one). What had never run is the body that JUDGES — the committee
// walk, the strict-majority computation, the outlier slash and its trace — plus the error branches.
// So the handler was known to say NO in two situations, and had never once said YES.
//
// Its own header lists five guards "and why each one exists". Four of them live in that unexecuted
// body: full committee, strict majority, fixed threshold, anti-replay after a verdict. A documented
// guard that no case crosses is a claim, not a protection.
//
// What it decides: a committee of three, a strict cosine majority, and every outlier loses
// `slash_leak_bps` of its stake. There is no payment here — only punishment — so the failure mode of
// a wrong guard is an honest miner losing 80 % of its bond.

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
	attendu := uint64(1000) * p.SlashLeakBps / 10000

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
	require.Equal(t, uint64(1000)-attendu, vc.Stake, "the outlier loses slash_leak_bps of its stake")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, attendu, pools.Treasury)

	job, err := f.keeper.Job.Get(f.ctx, "jVS")
	require.NoError(t, err)
	require.Contains(t, job.State, "+verified")
	// THE TRACE IS NOT DECORATION. `ResolveDispute` and `AdjudicateDispute` only iterate
	// `job.SlashRecords`: without this entry an honest miner slashed here and later cleared would never
	// be refunded — the dispute would succeed and give back nothing.
	// ⚠️ AND THE RESTITUTION IT ENABLES IS BOUNDED, WHICH THIS COMMENT USED TO OMIT: `ResolveDispute`
	// clamps the re-credit to the Treasury balance, so a miner cleared after the Treasury has been
	// drained gets back LESS than it lost, on a call that still answers success. Pinned by
	// `dispute_restitution_bound_test.go`. The trace is necessary for restitution; it is not sufficient.
	require.Len(t, job.SlashRecords, 1, "the slash must be traced or a successful dispute restores nothing")
	require.Equal(t, "vc", job.SlashRecords[0].MinerId)
	require.Equal(t, attendu, job.SlashRecords[0].Amount)
}

// NO STRICT MAJORITY -> NOBODY IS SLASHED. This is the guard its header calls "rules out an arbitrary
// slash": three mutually orthogonal answers must punish no one, rather than punish everyone.
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

// AN INCOMPLETE COMMITTEE IS REFUSED. With two answers instead of three, `required` would fall to 2
// and a single pair could condemn the third — the guard exists so a partial committee cannot judge.
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

// ANTI-REPLAY: the header says it plainly — without it "anyone could replay VerifySemantic to grind an
// honest miner's stake down to zero". The second call must refuse, and the stake is what proves it.
func TestVerifySemanticCannotBeReplayedToGrindAStake(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jVSRep", map[string]string{"ra": "1,2,3", "rb": "1,2,3", "rc": "-1,-2,-3"})
	msg := &types.MsgVerifySemantic{Creator: ssAddr20(t, f, "anyone-vs"), JobId: "jVSRep"}

	_, err := srv.VerifySemantic(f.ctx, msg)
	require.NoError(t, err)
	apres, err := f.keeper.Miner.Get(f.ctx, "rc")
	require.NoError(t, err)

	_, err = srv.VerifySemantic(f.ctx, msg)
	require.Error(t, err, "a judged committee must not be judged again")

	rejoue, err := f.keeper.Miner.Get(f.ctx, "rc")
	require.NoError(t, err)
	require.Equal(t, apres.Stake, rejoue.Stake,
		"the replay must not take a second bite: this is the grind the guard exists to stop")
	job, err := f.keeper.Job.Get(f.ctx, "jVSRep")
	require.NoError(t, err)
	require.Len(t, job.SlashRecords, 1, "and it must not record the same slash twice")
}

// DISABLED IN OPTIMISTIC MODE. The chain in production runs mode 1, where verification goes through the
// VRF audit; leaving this permissionless slasher reachable there would give a second, unaudited way to
// punish. The refusal must come BEFORE anything is read or moved.
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
