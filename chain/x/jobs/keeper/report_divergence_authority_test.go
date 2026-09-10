package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE ONE LINE BETWEEN A FORGED CLAIM AND A SLASHED MINER.
//
// `ReportDivergence` slashes the job's miner on a divergence "proof" that the CALLER supplies
// (`miner_commit` vs `correct_commit`). Nothing on-chain verifies that proof, so the handler is
// restricted to the governance authority — its own comment says why: left permissionless it is a
// griefing vector, since a false report destroys an honest miner's bond.
//
// ⛔ AND THAT RESTRICTION WAS THE ONLY THING STANDING THERE, WITH NO TEST BEHIND IT. Measured with
// `go tool cover`: this handler was at **0.0 %** coverage while being a reachable Msg RPC. A guard
// that is one `if` long, on the path that destroys stake, and that no case exercises, is the exact
// shape of a guard that disappears in a refactor without anything going red.
//
// These two cases are not about what the handler computes. They are about who may call it.

func TestReportDivergence_UnInconnuNePeutPasSlasher(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mCible", "creator_divergence_inconnu_")

	require.NoError(t, gvSetJob(f, f.ctx, "jDiv", types.Job{JobId: "jDiv", MinerId: "mCible"}))
	avant, err := f.keeper.Miner.Get(f.ctx, "mCible")
	require.NoError(t, err)

	// `creator` is the miner's own operator: a perfectly valid account, and NOT the authority.
	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: creator, JobId: "jDiv", MinerCommit: "a", CorrectCommit: "b",
	})
	require.Error(t, err,
		"a caller who is not the governance authority must not be able to slash on a claim nobody verified")
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	apres, err := f.keeper.Miner.Get(f.ctx, "mCible")
	require.NoError(t, err)
	require.Equal(t, avant.Stake, apres.Stake,
		"and the refusal must leave the stake untouched: a guard that refuses AFTER moving value has not refused")
}

func TestReportDivergence_LAutoriteSlashe(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, ctx := rgSetup(t, f, "mCible2", "creator_divergence_autorite_")

	autorite, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, "jDiv2", types.Job{JobId: "jDiv2", MinerId: "mCible2"}))

	avant, err := f.keeper.Miner.Get(f.ctx, "mCible2")
	require.NoError(t, err)
	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	attendu := avant.Stake * p.SlashLeakBps / 10000
	// The other half: a test that only proves a refusal would stay green on a handler that refuses
	// EVERYONE. The authority must still be able to act, and the amount is DERIVED from the parameter
	// the handler reads — never retyped here, or the case would pin a number instead of a rule.
	require.Positive(t, attendu, "with slash_leak_bps at 0 this case would prove nothing")

	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: autorite, JobId: "jDiv2", MinerCommit: "a", CorrectCommit: "b",
	})
	require.NoError(t, err, "the governance authority is the one caller this handler serves")

	apres, err := f.keeper.Miner.Get(f.ctx, "mCible2")
	require.NoError(t, err)
	require.Equal(t, avant.Stake-attendu, apres.Stake, "the slash is applied at slash_leak_bps")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, attendu, pools.Treasury, "and the slashed amount funds the treasury, which is what compensates the client")
}

// ... and the degenerate claim is refused BEFORE anything is read: identical commits are not a
// divergence, and a handler that slashed on them would turn every honest job into a punishable one.
func TestReportDivergence_CommitsIdentiquesNeSontPasUneDivergence(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, ctx := rgSetup(t, f, "mCible3", "creator_divergence_identique_")

	autorite, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, "jDiv3", types.Job{JobId: "jDiv3", MinerId: "mCible3"}))
	avant, err := f.keeper.Miner.Get(f.ctx, "mCible3")
	require.NoError(t, err)

	_, err = srv.ReportDivergence(ctx, &types.MsgReportDivergence{
		Creator: autorite, JobId: "jDiv3", MinerCommit: "meme", CorrectCommit: "meme",
	})
	require.Error(t, err, "identical commits are not a divergence")

	apres, err := f.keeper.Miner.Get(f.ctx, "mCible3")
	require.NoError(t, err)
	require.Equal(t, avant.Stake, apres.Stake, "and nothing is slashed on a non-divergence")
}
