package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// One check stands between a forged claim and a slashed miner.
//
// `ReportDivergence` slashes the job's miner on a divergence "proof" that the caller supplies
// (`miner_commit` vs `correct_commit`). Nothing on-chain verifies that proof, so the handler is
// restricted to the governance authority; its own comment says why: left permissionless it is a
// griefing vector, since a false report destroys an honest miner's bond.
//
// That restriction had no test behind it: coverage put this reachable Msg RPC at 0.0 %. A one-line
// guard on the path that destroys stake, which no case exercises, can disappear in a refactor with
// nothing going red.
//
// These two cases are about who may call the handler, and leave aside what it computes.

func TestReportDivergence_AStrangerCannotSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mTarget", "creator_divergence_unknown_")

	require.NoError(t, gvSetJob(f, f.ctx, "jDiv", types.Job{JobId: "jDiv", MinerId: "mTarget"}))
	before, err := f.keeper.Miner.Get(f.ctx, "mTarget")
	require.NoError(t, err)

	// `creator` is the miner's own operator: a valid account, and not the authority.
	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: creator, JobId: "jDiv", MinerCommit: "a", CorrectCommit: "b",
	})
	require.Error(t, err,
		"a caller who is not the governance authority must not be able to slash on a claim nobody verified")
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	after, err := f.keeper.Miner.Get(f.ctx, "mTarget")
	require.NoError(t, err)
	require.Equal(t, before.Stake, after.Stake,
		"and the refusal must leave the stake untouched: a guard that refuses after moving value has not refused")
}

func TestReportDivergence_TheAuthoritySlashes(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, ctx := rgSetup(t, f, "mTarget2", "creator_divergence_authority")

	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, "jDiv2", types.Job{JobId: "jDiv2", MinerId: "mTarget2"}))

	before, err := f.keeper.Miner.Get(f.ctx, "mTarget2")
	require.NoError(t, err)
	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	expected := before.Stake * p.SlashLeakBps / 10000
	// The other half: a test that only proves a refusal would stay green on a handler that refuses
	// everyone. The authority must still be able to act, and the amount is derived from the parameter the
	// handler reads, never retyped here, so the case pins a rule and not a number.
	require.Positive(t, expected, "with slash_leak_bps at 0 this case would prove nothing")

	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: authority, JobId: "jDiv2", MinerCommit: "a", CorrectCommit: "b",
	})
	require.NoError(t, err, "the governance authority is the one caller this handler serves")

	after, err := f.keeper.Miner.Get(f.ctx, "mTarget2")
	require.NoError(t, err)
	require.Equal(t, before.Stake-expected, after.Stake, "the slash is applied at slash_leak_bps")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, expected, pools.Treasury, "and the slashed amount funds the treasury, which is what compensates the client")
}

// The degenerate claim is refused before anything is read: identical commits are not a divergence, and
// a handler that slashed on them would make every honest job punishable.
func TestReportDivergence_IdenticalCommitsAreNotADivergence(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, ctx := rgSetup(t, f, "mTarget3", "creator_divergence_identical_")

	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, "jDiv3", types.Job{JobId: "jDiv3", MinerId: "mTarget3"}))
	before, err := f.keeper.Miner.Get(f.ctx, "mTarget3")
	require.NoError(t, err)

	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: authority, JobId: "jDiv3", MinerCommit: "same", CorrectCommit: "same",
	})
	require.Error(t, err, "identical commits are not a divergence")

	after, err := f.keeper.Miner.Get(f.ctx, "mTarget3")
	require.NoError(t, err)
	require.Equal(t, before.Stake, after.Stake, "and nothing is slashed on a non-divergence")
}
