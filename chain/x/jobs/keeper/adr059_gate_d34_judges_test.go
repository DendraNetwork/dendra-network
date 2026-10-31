package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The epoch 15 rules on missed draws and on the trial window that no test pinned before.
// Each case states one of those rules, or the parameter comment that names a compiled default,
// and drives the shipped draw, conclusion or genesis import.

// d34Resolve concludes a disputed job through the authority's ResolveDispute, one of the four points
// where decision 3 counts the missed draws.
func d34Resolve(t *testing.T, f *fixture, jobId string) {
	t.Helper()
	_, err := keeper.NewMsgServerImpl(f.keeper).ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: jobId, Upheld: false})
	require.NoError(t, err)
}

// d34Draw draws the audit jury of `jobId` at height `h` with the shipped draw.
func d34Draw(t *testing.T, f *fixture, jobId string, h uint64) []string {
	t.Helper()
	got, err := f.keeper.DrawAuditCommitteeForTest(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(h)), "s", jobId, "", "")
	require.NoError(t, err)
	return got
}

// d34Import starts a chain from `gs` with a zero-height import, after the check validate-genesis runs.
func d34Import(t *testing.T, gs *types.GenesisState) *fixture {
	t.Helper()
	require.NoError(t, gs.Validate(), "premise: validate-genesis accepts this state")
	f := initFixture(t)
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(0)
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *gs))
	return f
}

// Decision 3 with judge_missed_draws_max at 0: the compiled default is the count its parameter comment
// names (params.proto, judge_missed_draws_max: 3). Three concluded audits with no answer take a judge
// out of the next draw, two do not.
func TestADR059_AtZeroThreeConcludedAuditsWithoutAnAnswerExcludeAJudge(t *testing.T) {
	f := initFixture(t)
	require.Zero(t, types.DefaultParams().JudgeMissedDrawsMax, "premise: the shipped value selects the compiled default")
	a59Miner(t, f, "jm", 1000)
	a59Declare(t, f, 1, "jm")
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jmNext", 1000))
	for i, jobId := range []string{"jm1", "jm2", "jm3"} {
		require.Equal(t, []string{"jm"}, d34Draw(t, f, "jmNext", 1000), "%d concluded audits without an answer: still drawn", i)
		require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{JobId: jobId, State: "open+paid+optimistic+disputed"}))
		aeAnchor(f, t, jobId, "jm")
		d34Resolve(t, f, jobId)
		require.Equal(t, uint64(i+1), a59Missed(t, f, "jm"), "one missed draw per concluded audit")
	}
	require.Empty(t, d34Draw(t, f, "jmNext", 1000), "three concluded audits without an answer: not a candidate")
}

// Decision 4: a declared judge with no verdict yet is a candidate while its declaration is within
// judge_verdict_window_blocks of the draw height. The trial follows the governed window, and the
// governed juror freshness when the window is 0, exactly as the window on a verdict does.
func TestADR059_TheTrialLastsTheGovernedJudgeVerdictWindow(t *testing.T) {
	f := initFixture(t)
	a59Miner(t, f, "jt", 1000)
	a59Declare(t, f, 100, "jt") // no verdict: only the trial can seat it

	g := types.DefaultParams()
	g.JudgeVerdictWindowBlocks = 300000
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jt", 100+300000, g), "the last block of a governed trial")
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jt", 100+300001, g), "one block past it")
	fr := types.DefaultParams()
	fr.JurorFreshnessBlocks = 1000
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jt", 100+1000, fr), "window 0: the trial is the governed freshness")
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jt", 100+1001, fr), "window 0: one block past the governed freshness")

	// The same edge through the draw, with the window posted on chain and the vitality kept by a commit.
	require.NoError(t, f.keeper.Params.Set(f.ctx, g))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "jt", 300000))
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jTrial", 1000))
	require.Equal(t, []string{"jt"}, d34Draw(t, f, "jTrial", 100+300000), "drawn on the last block of the governed trial")
	require.Empty(t, d34Draw(t, f, "jTrial", 100+300001), "not drawn one block past it")
}

// Decisions 3 and 4 read the window and the cap from the params, 0 selecting the compiled values. A
// draw that cannot read its params applies that ungoverned rule, so it seats the judges the shipped
// defaults seat: neither a shorter trial nor a lower cap.
func TestADR059_ADrawWithUnreadableParamsJudgesByTheUngovernedRule(t *testing.T) {
	f := initFixture(t)
	const window = uint64(259200) // the compiled juror freshness, which a judge window of 0 selects
	const decl = uint64(1000)
	h := decl + window
	maxMissed := keeper.JudgeMissedDrawsMaxForTest
	for _, id := range []string{"uTrial", "uLate", "uBelow", "uAtCap"} {
		a59Miner(t, f, id, 1000)
		require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, id, h)) // vital at the draw height
	}
	a59Declare(t, f, decl, "uTrial", "uBelow", "uAtCap") // the trial of uTrial ends at h
	a59Declare(t, f, decl-1, "uLate")                    // its trial ended one block before h
	for _, id := range []string{"uBelow", "uAtCap"} {
		require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, id, h))
	}
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "uBelow", maxMissed-1))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "uAtCap", maxMissed))
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jU", h))
	want := []string{"uTrial", "uBelow"}
	require.ElementsMatch(t, want, d34Draw(t, f, "jU", h), "premise: the shipped defaults")

	require.NoError(t, f.keeper.Params.Remove(f.ctx))
	_, err := f.keeper.Params.Get(f.ctx)
	require.Error(t, err, "premise: the params cannot be read")
	require.ElementsMatch(t, want, d34Draw(t, f, "jU", h), "unreadable params: the same judges as the ungoverned rule")
}

// Decision 3 counts a seat once per audit. A genesis import copies an anchor string as written and
// nothing refuses an id listed twice in it, so the conclusion must still bill that judge one draw.
func TestADR059_AJudgeListedTwiceOnAnImportedJuryIsCountedOnce(t *testing.T) {
	a := registeredMiner(t, 0x51, 1000)
	b := registeredMiner(t, 0x52, 1000)
	gs := types.DefaultGenesis()
	gs.MinerMap = []types.Miner{a, b}
	gs.MinerJudge = []string{a.MinerId, b.MinerId}
	gs.JobMap = []types.Job{{JobId: "jDup", State: "open+paid+optimistic+disputed", Fee: 100000}}
	gs.AuditCommittee = []types.StringEntry{{Key: "jDup", Value: a.MinerId + "," + b.MinerId + "," + a.MinerId}}
	f := d34Import(t, gs)
	d34Resolve(t, f, "jDup")
	require.Equal(t, uint64(1), a59Missed(t, f, a.MinerId), "listed twice and mute: one missed draw for one audit")
	require.Equal(t, uint64(1), a59Missed(t, f, b.MinerId), "the twin listed once")
}

// Decision 3 skips a seat whose miner left the registry. Exit and eviction forget the judge state, but
// a genesis import keeps a declaration with no miner record, and such a seat must leave no counter.
func TestADR059_ASeatWhoseMinerLeftTheRegistryLeavesNoCounter(t *testing.T) {
	judge := registeredMiner(t, 0x53, 1000)
	gone := registeredMiner(t, 0x54, 1000) // declared and seated, with no record in the registry
	gs := types.DefaultGenesis()
	gs.MinerMap = []types.Miner{judge}
	gs.MinerJudge = []string{judge.MinerId, gone.MinerId}
	gs.JobMap = []types.Job{{JobId: "jGone", State: "open+paid+optimistic+disputed", Fee: 100000}}
	gs.AuditCommittee = []types.StringEntry{{Key: "jGone", Value: gone.MinerId + "," + judge.MinerId}}
	f := d34Import(t, gs)
	declared, err := f.keeper.MinerJudge.Has(f.ctx, gone.MinerId)
	require.NoError(t, err)
	require.True(t, declared, "premise: the import kept the declaration of a miner it has no record of")

	d34Resolve(t, f, "jGone")
	require.Equal(t, uint64(1), a59Missed(t, f, judge.MinerId), "premise: the conclusion billed its registered, mute judge")
	has, err := f.keeper.MinerMissedDraws.Has(f.ctx, gone.MinerId)
	require.NoError(t, err)
	require.False(t, has, "no counter on an id that is not in the registry")
}
