package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE SAME MINER WAS SLASHED TWICE FOR ONE PIECE OF WORK, WHILE THE CODE SAID THAT WAS IMPOSSIBLE.
//
// `jobK3Adjudicated` (job_state.go) states the rule and even names this failure: "settle -> verify then
// verify -> finalize re-slash the same outlier (80 % of the REMAINDER on every pass, up to ~96 %), and
// these entry points are permissionless. One rule instead: as soon as any of these markers is present
// the committee has been judged and no other path may slash again."
//
// Two of the three paths asked that predicate. `SettleSemantic` did not: it carried a targeted test on
// the single word "finalized", justified by keeping "the documented flow FinalizeJob -> Payout" open —
// but Payout is a different handler that this test never touched, so the narrowing bought nothing.
// "+verified", written by `VerifySemantic`, matched neither that test nor `jobIsPaid`.
//
// MEASURED BEFORE THE FIX, by running the sequence rather than reading it: 1000 -> 200 -> 40. An honest
// miner lost ~96 % of its bond for one piece of work, on two calls anyone can make, for the price of gas.
// The fix asks `jobK3Adjudicated`, like the other two paths.
//
// ⛔ WHY NO EXISTING TEST SAW IT. The square has four edges and three were covered: verify->finalize and
// settle->verify (double_settlement_guard_test.go), finalize->settle (f1_finalize_settle_guard_test.go).
// The fourth — verify->settle_semantic — had no case at all. A reader finds "UNIFIED ANTI-DOUBLE-SLASH"
// in two files out of three and concludes the rule is unified.
//
// ⚠️ AND THE ASSERTION HAS TO BE THE BOND, NOT THE ERROR. `SettleSemantic` refuses for several reasons
// that have nothing to do with this guard ("incomplete committee", an escrow it cannot draw on), and
// they carry the same error type. Only the outlier's remaining stake separates "the guard stopped it"
// from "something else did". So the escrow is funded, and every conclusion below is read off the stake.
//
// MODE: 0, the Go default (`DefaultParams` never sets `VerificationMode`). The public network runs mode
// 1, where `SettleSemantic` dispatches to `settleOptimistic` and this sequence cannot arise — and where
// `VerifySemantic` refuses outright, so no job is ever marked "+verified". Claiming the live chain was
// exposed would be the easy overclaim. What this pins is the path an operator gets from the defaults.

func csFund(t *testing.T, f *fixture, jobID string, fee uint64) {
	t.Helper()
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(fee)))
	job, err := f.keeper.Job.Get(f.ctx, jobID)
	require.NoError(t, err)
	job.Client = ssAddr20(t, f, "cs-client-"+jobID)
	require.NoError(t, gvSetJob(f, f.ctx, jobID, job))
}

// (a) THE EDGE THAT WAS OPEN: verify judges and slashes, then settle must NOT judge the same committee.
func TestVerifyThenSettleSemanticCannotSlashTheSameMinerTwice(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jCS", map[string]string{"ca": "1,2,3", "cb": "1,2,3", "cc": "-1,-2,-3"})
	csFund(t, f, "jCS", 10000)

	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	premier := uint64(1000) - uint64(1000)*p.SlashLeakBps/10000 // what one judgement leaves
	compose := premier - premier*p.SlashLeakBps/10000           // what a second one would leave

	_, err = srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-cs1"), JobId: "jCS",
	})
	require.NoError(t, err)
	apres1, err := f.keeper.Miner.Get(f.ctx, "cc")
	require.NoError(t, err)
	require.Equal(t, premier, apres1.Stake, "premise: the first judgement took slash_leak_bps of the bond")

	job, err := f.keeper.Job.Get(f.ctx, "jCS")
	require.NoError(t, err)
	require.Contains(t, job.State, "+verified")
	require.NotContains(t, job.State, "finalized", "the marker the old guard named is absent...")
	require.False(t, jobIsPaidProbe(job.State), "...and so is the one jobIsPaid names")

	// The second entry point, by a completely unrelated signer.
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone-cs2"), JobId: "jCS",
	})
	require.Error(t, err, "a judged committee must not be judged again through a sibling handler")

	apres2, err := f.keeper.Miner.Get(f.ctx, "cc")
	require.NoError(t, err)
	require.Equal(t, premier, apres2.Stake,
		"the bond is what proves it: a second judgement would leave %d, i.e. slash_leak_bps of what "+
			"the first one left", compose)

	// One committee, one slash record. A second record IS the compound.
	job, err = f.keeper.Job.Get(f.ctx, "jCS")
	require.NoError(t, err)
	require.Len(t, job.SlashRecords, 1, "a second record would mean the same outlier was charged twice")
	require.Equal(t, "cc", job.SlashRecords[0].MinerId)
}

// (b) THE CONTROL, on an edge that was ALREADY guarded: settle first, then verify. Same fixture, same
// handlers, reversed order. Without this half, case (a) could not tell "the fix works" from "this pair
// never double-slashed in this fixture".
func TestSettleThenVerifySemanticDoesNotSlashTwice(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jCSb", map[string]string{"ba": "1,2,3", "bb": "1,2,3", "bc": "-1,-2,-3"})
	csFund(t, f, "jCSb", 10000)

	_, err := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone-csb1"), JobId: "jCSb",
	})
	require.NoError(t, err)
	apres1, err := f.keeper.Miner.Get(f.ctx, "bc")
	require.NoError(t, err)
	require.Less(t, apres1.Stake, uint64(1000), "premise: the first judgement really slashed")

	_, _ = srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-csb2"), JobId: "jCSb",
	})

	apres2, err := f.keeper.Miner.Get(f.ctx, "bc")
	require.NoError(t, err)
	require.Equal(t, apres1.Stake, apres2.Stake,
		"this edge already asked the unifying predicate, so the bond is untouched -- the fixture is not the cause")
}

// (c) THE EXIT MUST STAY OPEN, or the fix trades a double slash for a stranded escrow — the failure this
// repository met this same day in `SettlePay`. A judged-but-unpaid job is still payable ONCE, through
// Payout, which guards on `jobIsPaid` alone. Asserted on the module escrow, not on an error code.
func TestAVerifiedJobIsStillPayableThroughPayout(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jCSc", map[string]string{"pa": "1,2,3", "pb": "1,2,3", "pc": "-1,-2,-3"})
	csFund(t, f, "jCSc", 10000)

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-csc"), JobId: "jCSc",
	})
	require.NoError(t, err)

	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: ssAddr20(t, f, "anyone-csc2"), JobId: "jCSc"})
	require.NoError(t, err, "refusing SettleSemantic must not leave the escrow with no way out")

	require.True(t, f.bank.mod[types.ModuleName].IsZero(),
		"the escrow really left the module: the surplus is refunded, nothing is stranded")
	job, err := f.keeper.Job.Get(f.ctx, "jCSc")
	require.NoError(t, err)
	require.True(t, jobIsPaidProbe(job.State), "and the job is marked paid, so it cannot be paid twice")
}
