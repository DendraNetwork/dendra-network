package keeper_test

import (
	"errors"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Consensus epoch 15, decision 2, read through both shipped draws: the audit jury (drawAuditCommittee)
// and the re-adjudication jury (anchorRedoCommittee). The predicate has its own cases in
// adr059_judges_test.go; these ask what each draw hands it.

// d25CompiledWindow is the compiled juror freshness. The judge window selects it when
// judge_verdict_window_blocks and juror_freshness_blocks are both 0.
const d25CompiledWindow = uint64(259200)

// d25Judge registers an undeclared miner at `reg`, which puts it in a pool frozen at `reg` and inside
// the vitality window of a draw there, then declares it a judge at `declared`.
func d25Judge(t *testing.T, f *fixture, id string, reg, declared uint64) {
	t.Helper()
	a59Miner(t, f, id, 1000)
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, reg))
	a59Declare(t, f, declared, id)
}

// d25DrawBoth runs both draws of `jobId` at height `h` and returns the audit jury and the redo jury.
// The work committee is anchored on three ids that are not registered, so every registered judge is
// also a redo candidate and the two juries can be compared.
func d25DrawBoth(t *testing.T, f *fixture, jobId string, h uint64) ([]string, []string) {
	t.Helper()
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, jobId, h))
	require.NoError(t, f.keeper.WorkCommittee.Set(f.ctx, jobId, "d25-w0,d25-w1,d25-w2"))
	require.NoError(t, f.keeper.BlockHash.Set(f.ctx, int64(h), []byte("d25-block-hash-"+jobId)))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(h))
	audit, err := f.keeper.DrawAuditCommitteeForTest(ctx, "d25-seed", jobId, "", "")
	require.NoError(t, err)
	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(ctx, jobId, "", ""))
	raw, err := f.keeper.AuditCommittee.Get(ctx, keeper.RedoAnchorKeyForTest(jobId))
	if errors.Is(err, collections.ErrNotFound) {
		return audit, nil
	}
	require.NoError(t, err)
	return audit, splitNonEmpty(raw)
}

// Decision 2: the window is counted back from the draw height. At the last block of the window a
// judge is seated on both juries; one block earlier it is seated on neither.
func TestADR059_BothDrawsReadTheJudgeWindowAtTheDrawHeight(t *testing.T) {
	const h = uint64(1_000_000)
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	d25Judge(t, f, "d25-stale", h, h-d25CompiledWindow-1)
	d25Judge(t, f, "d25-edge", h, h-d25CompiledWindow)
	d25Judge(t, f, "d25-fresh", h, h)

	audit, redo := d25DrawBoth(t, f, "d25-jHeight", h)
	want := []string{"d25-edge", "d25-fresh"}
	require.ElementsMatch(t, want, audit, "the audit jury reads the window at the draw height")
	require.ElementsMatch(t, want, redo, "the redo jury reads the window at the draw height")
}

// Decision 2: judge_verdict_window_blocks and judge_missed_draws_max are governed, and both draws read
// the posted values. A wider posted window seats a judge the compiled window would not, and a posted
// cap of 1 excludes a judge the compiled cap would keep.
func TestADR059_BothDrawsReadTheGovernedJudgeWindowAndCap(t *testing.T) {
	const h = uint64(1_000_000)
	f := initFixture(t)
	p := types.DefaultParams()
	p.JudgeVerdictWindowBlocks = 400000
	p.JudgeMissedDrawsMax = 1
	require.NoError(t, p.Validate(), "premise: a parameter set governance can post")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	d25Judge(t, f, "d25-wide", h, h-300000) // inside the posted window, outside the compiled one
	d25Judge(t, f, "d25-past", h, h-400001) // one block past the posted window
	d25Judge(t, f, "d25-missed", h, h)
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "d25-missed", 1)) // at the posted cap
	d25Judge(t, f, "d25-fresh", h, h)

	audit, redo := d25DrawBoth(t, f, "d25-jGov", h)
	want := []string{"d25-wide", "d25-fresh"}
	require.ElementsMatch(t, want, audit, "the audit jury follows the posted window and cap")
	require.ElementsMatch(t, want, redo, "the redo jury follows the posted window and cap")
}

// Decision 2 under the zero rule: params that cannot be read select the compiled values, the behaviour
// of a chain that never governed the fields. The compiled window is the juror freshness and the
// compiled cap is three missed draws.
func TestADR059_UnreadableParamsDrawWithTheCompiledJudgeWindowAndCap(t *testing.T) {
	const h = uint64(1_000_000)
	f := initFixture(t)
	d25Judge(t, f, "d25-edge", h, h-d25CompiledWindow)
	d25Judge(t, f, "d25-out", h, h-d25CompiledWindow-1)
	d25Judge(t, f, "d25-two", h, h)
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "d25-two", 2))
	d25Judge(t, f, "d25-three", h, h)
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "d25-three", 3))
	require.NoError(t, f.keeper.Params.Remove(f.ctx))
	_, err := f.keeper.Params.Get(f.ctx)
	require.Error(t, err, "premise: the params cannot be read")

	audit, redo := d25DrawBoth(t, f, "d25-jNoParams", h)
	want := []string{"d25-edge", "d25-two"}
	require.ElementsMatch(t, want, audit, "the audit jury falls back to the compiled window and cap")
	require.ElementsMatch(t, want, redo, "the redo jury falls back to the compiled window and cap")
}

// Decision 4 gives a declared judge the window the vitality rule gives a new registration, and the
// dates are read the same way: a date at or after the height is recent, never stale.
func TestADR059_AJudgeDateAheadOfTheHeightIsRecent(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	a59Miner(t, f, "d25-decl", 1000)
	a59Declare(t, f, 5000, "d25-decl")
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "d25-decl", 4999, p), "a declaration dated after the height")
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "d25-decl", 5000, p), "a declaration dated at the height")

	a59Miner(t, f, "d25-verdict", 1000)
	a59Declare(t, f, 1, "d25-verdict")
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "d25-verdict", 900000))
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "d25-verdict", 600000, p),
		"a last verdict dated after the height, with a declaration far outside the window")
}

// Decision 2: the judge filter is added to the filters the audit draw already had. A judge inside its
// trial window that is not vital at the draw height (registered long ago, no commit since) is not
// seated; its vital twin is.
func TestADR059_TheAuditDrawKeepsTheVitalityFilter(t *testing.T) {
	const h = uint64(1_000_000)
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	d25Judge(t, f, "d25-idle", 0, h)
	d25Judge(t, f, "d25-vital", h, h)
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "d25-jVital", h))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(h))
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(ctx, "d25-idle", h, types.DefaultParams()), "premise: the idle miner passes the judge filter")

	got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "d25-seed", "d25-jVital", "", "")
	require.NoError(t, err)
	require.Equal(t, []string{"d25-vital"}, got, "a declared judge that is not vital holds no audit seat")
}
