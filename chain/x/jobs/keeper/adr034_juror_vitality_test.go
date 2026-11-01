package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Juror vitality is not mining: the exit guard, pinned by three cases.
//
// Clause (b) of `hasOpenObligation` (`miner_vitality.go`) must not hold a juror the protocol has itself
// declared undrawable, that is, beyond `jurorFreshnessBlocks`, where `eligibleAsJuror` already refuses
// to summon it. Holding someone in an old committee while refusing to draw them into a fresh one is
// incoherent.
//
// "Release once the audit is past its deadline" was rejected, and these tests keep it out: an audit
// past its deadline without quorum is the case of a primary whose audit could not conclude. A cheater
// able to stall its own audit would leave with its stake, and the slash would become avoidable by
// waiting. Hence case (3) below, which shows the audited party does not move.
//
// The contract of these tests:
//   (1) the live juror stays held: the fix must not overreach;
//   (2) the expired juror may leave: the behaviour being introduced;
//   (3) the audited party never leaves, expired or not: the slash must stay unavoidable.
// Without (1), one hole would simply be traded for another.

const rgStake = uint64(1_000_000)

// rgSetup creates a miner, its creator, and enough funds to refund its bond on exit.
func rgSetup(t *testing.T, f *fixture, id, seed string) (string, sdk.Context) {
	t.Helper()
	aeFundModule(f)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(seed)))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
		MinerId: id, Stake: rgStake, Operator: creator, Creator: creator,
	}))
	return creator, sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
}

// (1) The live juror stays held. A miner that anchored a commit recently is summonable
// (`eligibleAsJuror` draws it), so its seat in an ongoing audit means something, and letting it
// desert would make the quorum unreachable, the harm clause (b) exists to prevent.
func TestVitality_LiveJuror_SittingOnAnOpenAudit_StaysHeld(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mAliveJuror", "creator_juror_alive_")

	// Live: commit anchored 1,000 blocks ago, far inside jurorFreshnessBlocks (259,200).
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mAliveJuror", uint64(vitH-1000)))
	// Sits on the committee of an unresolved audit, and is not the primary of the job.
	require.NoError(t, gvSetJob(f, f.ctx, "jAlive", types.Job{
		JobId: "jAlive", MinerId: "anotherPrimary", State: "open+paid+optimistic+disputed",
	}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jAlive", "mAliveJuror,mOther"))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mAliveJuror"})
	require.Error(t, err,
		"a summonable juror sitting on an ongoing audit must stay held: letting it leave is the "+
			"desertion clause (b) exists to prevent")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mAliveJuror")
	require.NoError(t, hErr)
	require.True(t, has, "it must still be in the registry")
}

// (2) The expired juror must be able to leave.
//
// Identities whose operator keys are lost still sit on unresolved audits and will never vote. Clause
// (b) held them, the audit could not conclude for want of their votes, and pruning goes through the
// same guard, so neither voluntary exit nor automatic eviction was possible. The seat would live
// forever because it was dead, keeping the quorum bar high for the whole network.
func TestVitality_ExpiredJuror_SittingOnAnOpenAudit_MustBeAbleToExit(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mStaleJuror", "creator_juror_stale_")

	// Expired: last commit at block 1, current height 700,000, so 699,999 > 259,200. `eligibleAsJuror`
	// refuses to summon it onto a fresh committee, the incoherence being removed.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStaleJuror", vitAncient))
	require.NoError(t, gvSetJob(f, f.ctx, "jExpired", types.Job{
		JobId: "jExpired", MinerId: "anotherPrimary", State: "open+paid+optimistic+disputed",
	}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jExpired", "mStaleJuror,mOther"))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mStaleJuror"})
	require.NoError(t, err,
		"a juror the protocol already refuses to summon must no longer be held: keeping it serves "+
			"nothing and keeps the quorum bar high for the whole network")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mStaleJuror")
	require.NoError(t, hErr)
	require.False(t, has, "it must have left the registry")
}

// (3) The audited party never leaves, even expired. Clause (c) holds the primary of an unresolved
// disputed job, and the clause (b) behaviour must not touch it. This miner is expired like the one in
// case (2); only its role differs. If this case ever stopped failing the exit, the slash would become
// avoidable by waiting.
func TestVitality_ClauseC_ExpiredPrimaryOfADisputedJobStaysHeld(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mStalePrimary", "creator_primary_stale__")

	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStalePrimary", vitAncient))
	// It is the primary of the unresolved disputed job, the party being judged, not a juror.
	require.NoError(t, gvSetJob(f, f.ctx, "jAudited", types.Job{
		JobId: "jAudited", MinerId: "mStalePrimary", State: "open+paid+optimistic+disputed",
	}))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mStalePrimary"})
	require.Error(t, err,
		"the primary of an unresolved audit never leaves, expired or not: otherwise lying goes back to "+
			"0-EV instead of -EV, and the slash is avoided by waiting")
	has, hErr := f.keeper.Miner.Has(f.ctx, "mStalePrimary")
	require.NoError(t, hErr)
	require.True(t, has)
}

// (4) The release applies to both exit paths, voluntary exit and automatic pruning.
//
// An identity whose operator key is lost can never call `DeleteMiner`, so restricting the release to
// voluntary exit would only serve miners that still hold their key, the ones without the problem.
// Only pruning can remove an identity whose key is gone. `TestC1bis_ExpiredJurorSeatIsPrunable` pins
// that path; `TestC1_ActiveCommitteeSeatStillProtects` pins the complement with a fresh juror.
//
// The form of the justification matters as much as its content. "Every prune candidate is expired by
// construction (604,800 > 259,200)" holds for the compiled defaults and fails in general, since both
// windows are governable. The rule is therefore written on the predicate (`eligibleAsJuror`
// evaluated), never on a presumed order, and `Params.Validate` bounds
// `miner_prune_blocks >= juror_freshness_blocks` so the reverse incoherence cannot be configured.
