package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// An exit before the verdict must not make the slash pointless.
//
// Without an obligation guard on DeleteMiner (permissionless exit, stake returned in full), a challenged
// primary could leave before the verdict and reach the divergence `upheld` != `optimisticCheated`.
// The consequences each case below turns red on:
//   - AdjudicateDispute: cheating confirmed at the bar but the primary gone, so `upheld=false` and the
//     bond of a disputer who was right goes to the Treasury as "anti-grief";
//   - both resolution paths freeze the client's retention (adjudicate skipping the refund block,
//     slashCheatedPrimary doing a bare `return nil`), an orphan debt forever;
//   - the slash itself becomes moot: lying goes from -EV to 0-EV through the exit dodge.
// What holds: `upheld` follows the verdict; the client is refunded even when the primary is gone;
// hasOpenObligation has clause (c), "primary of an unresolved disputed job"; DeleteMiner goes through
// the same guard as the prune (fail closed).

// sedSeats anchors 15 seats (under the given prefix) and posts `votes` verdicts of "0" (invalid).
func sedSeats(f *fixture, t *testing.T, jobId, prefix string, votes int) {
	t.Helper()
	ids := make([]string, 0, 15)
	for i := 0; i < 15; i++ {
		id := prefix + string(rune('A'+i))
		aeReg(f, t, id, 1)
		ids = append(ids, id)
	}
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId, strings.Join(ids, ",")))
	for i := 0; i < votes; i++ {
		aeVerdict(f, t, jobId, ids[i], "0")
	}
}

// `upheld` follows the verdict, not the survival of the miner (red on both pockets).

// (takes effect, the core case) Cheating confirmed at the bar (10/15 "0"), primary gone. Previously
// `upheld = true` lived inside the successful `Miner.Get` block, so the bond of a disputer who was
// right was confiscated to the Treasury, and the client refund block was skipped, freezing the
// retention on a resolved job. Expected: the disputer gets its bond back and the client its retention;
// the verdict disposes of the funds, and the existence of the miner only of the slash.
func TestAdjudicateUpheldFollowsVerdictWhenPrimaryVanished(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_sedec_van__")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditMinQuorum = 4
	p.HoldBps = 10000
	p.ProtocolFeeBps = 0 // isolates pocket (1): the measured refund is exactly the retention
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispAcc := sdk.AccAddress([]byte("disputer_sedec_van__"))
	f.bank.setBalance(dispAcc, sdk.NewCoins())
	clientAcc := sdk.AccAddress([]byte("client_sedec_van____"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())

	sedSeats(f, t, "jVan", "sj", 10) // slash at the relative bar
	// No Miner "mGone": an exit or prune from before the guard existed (legacy state the chain must absorb).
	require.NoError(t, gvSetJob(f, f.ctx, "jVan", types.Job{
		JobId: "jVan", State: "open+paid+optimistic+disputed", MinerId: "mGone", Fee: 100000,
		Client: client, Disputer: disp, DisputeBond: 50000, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jVan", uint64(80000)))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jVan", uint64(1)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jVan"})
	require.NoError(t, err)

	require.Equal(t, int64(50000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(),
		"the disputer was right (cheating at the bar): its bond comes back even if the primary vanished — previously it went to the Treasury as anti-grief")
	require.Equal(t, int64(80000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(),
		"the client is refunded the retention even without a miner — previously it froze forever on a resolved job")
	_, hErr := f.keeper.HeldFee.Get(f.ctx, "jVan")
	require.Error(t, hErr, "the debt is settled, not orphaned")
	_, sErr := f.keeper.HeldSince.Get(f.ctx, "jVan")
	require.Error(t, sErr, "its twin: the timestamp leaves with the debt")
	job, _ := f.keeper.Job.Get(f.ctx, "jVan")
	require.Contains(t, job.State, "+resolved+adjudicated")
	require.Equal(t, uint64(0), job.DisputeBond, "bond consumed, not replayable")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury, "nothing to confiscate: no bond (returned), no slash (no stake left to slash)")
}

// (stays silent, the green side of the coupling) Same cheating at the bar, primary present: on this
// path `upheld == optimisticCheated` by construction. The disputer is refunded and rewarded, the client
// is refunded, the stake is slashed, the SlashRecord is written. If one moves without the other, one of
// these five assertions breaks.
func TestAdjudicateSlashCouplesBondAndRetentionWhenMinerPresent(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_sedec_ok___")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditMinQuorum = 4
	p.HoldBps = 10000
	p.ProtocolFeeBps = 0
	p.SlashLeakBps = 9000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispAcc := sdk.AccAddress([]byte("disputer_sedec_ok___"))
	f.bank.setBalance(dispAcc, sdk.NewCoins())
	clientAcc := sdk.AccAddress([]byte("client_sedec_ok_____"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())

	require.NoError(t, gvSetMiner(f, f.ctx, "mBad", types.Miner{MinerId: "mBad", Stake: aeStake}))
	sedSeats(f, t, "jCpl", "cj", 10)
	require.NoError(t, gvSetJob(f, f.ctx, "jCpl", types.Job{
		JobId: "jCpl", State: "open+paid+optimistic+disputed", MinerId: "mBad", Fee: 100000,
		Client: client, Disputer: disp, DisputeBond: 50000, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jCpl", uint64(80000)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jCpl"})
	require.NoError(t, err)

	slashAmt := aeStake * 9000 / 10000
	require.Equal(t, int64(100000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(),
		"bond returned plus a reward equal to the bond (bounded by the Treasury, funded by the slash)")
	// ADR-048: this fixture retains 80000 of a 100000 price, so 20000 had already left the module. The
	// timeout path owes that part to the client out of the slash, and so does this path, before
	// the whistleblower's reward, instead of keeping it in the Treasury.
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(),
		"client refunded the retention (80000) and, out of the slash, what had already left the module (20000)")
	m, _ := f.keeper.Miner.Get(f.ctx, "mBad")
	require.Equal(t, aeStake-slashAmt, m.Stake, "the hard slash bit")
	job, _ := f.keeper.Job.Get(f.ctx, "jCpl")
	require.Len(t, job.SlashRecords, 1, "SlashRecord written (refundable if it re-challenges)")
	require.Equal(t, uint64(0), job.DisputeBond)
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, slashAmt-20000-50000, pools.Treasury, "Treasury = slash minus the client's restitution minus the whistleblower reward")
}

// (takes effect, the timeout twin) Same vanished primary, EndBlocker path: slashCheatedPrimary did a
// bare `return nil`, so the job stayed in `+resolved+clawed+quorum` with its retention intact and no
// path left to settle it. Expected: client refunded, debt settled, disputer's bond returned (the bond
// table does not depend on the miner's survival).
func TestTimeoutSlashVanishedPrimaryStillRefundsClient(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	p.ProtocolFeeBps = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	dispAcc := sdk.AccAddress([]byte("disputer_sedec_tv___"))
	disp, err := f.addressCodec.BytesToString(dispAcc)
	require.NoError(t, err)
	f.bank.setBalance(dispAcc, sdk.NewCoins())
	clientAcc := sdk.AccAddress([]byte("client_sedec_tv_____"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())

	sedSeats(f, t, "jTv", "tj", 10)
	require.NoError(t, gvSetJob(f, f.ctx, "jTv", types.Job{
		JobId: "jTv", State: "open+paid+optimistic+disputed", MinerId: "mGoneT", Fee: 100000,
		Client: client, Disputer: disp, DisputeBond: 50000, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jTv", uint64(80000)))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jTv", uint64(1)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jTv")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jTv")
	require.Contains(t, job.State, "clawed+quorum", "cheating at quorum, even with no stake left to slash: the verdict is still written")
	require.Equal(t, int64(80000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(),
		"the client is refunded even when the primary vanished — previously the bare `return nil` froze the retention forever")
	_, hErr := f.keeper.HeldFee.Get(f.ctx, "jTv")
	require.Error(t, hErr, "debt settled: held-summary will not watch a dead entry age")
	require.Equal(t, int64(50000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(),
		"the disputer was right: bond returned, unaffected by the disappearance of the primary")
	require.Equal(t, uint64(0), job.DisputeBond)
}

// The root: a voluntary exit goes through the same guard as the eviction (two red cases, plus the
// two-sided one).

// (takes effect) A juror summoned on an unresolved audit does not desert: with enough deserters the
// seat quorum becomes unreachable forever (an unbounded deferral keeps retention and bond in escrow
// forever), as free griefing, since the exit returned 100% of the stake.
func TestDeleteMinerRefusedOnAnchoredSeat(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creatorAcc := sdk.AccAddress([]byte("creator_sedec_seat__"))
	creator, err := f.addressCodec.BytesToString(creatorAcc)
	require.NoError(t, err)
	f.bank.setBalance(creatorAcc, sdk.NewCoins())

	require.NoError(t, gvSetMiner(f, f.ctx, "mSeat", types.Miner{MinerId: "mSeat", Creator: creator, Stake: 700}))
	// Explicit date, so the test depends on what it asserts. Without it, `mSeat` has neither a commit
	// nor a registration height, so `eligibleAsJuror` falls into its benign fallback ("neither date
	// known, so imported state, do not penalise") and holds the miner for that reason. The test would
	// then pass through the fallback instead of the retention of a summonable juror that it announces,
	// and the day someone dated this fixture, the assertion would change meaning without a decision.
	// A recent commit is therefore written, so the juror is fresh by construction.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mSeat", uint64(vitH-1000)))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jSeat", "mSeat,mJ2,mJ3"))
	require.NoError(t, gvSetJob(f, f.ctx, "jSeat", types.Job{
		JobId: "jSeat", State: "open+paid+optimistic+disputed", MinerId: "mOther",
	}))

	// Explicit height: without it the context is 0, `0 <= lastCommit` would be true and the juror would
	// look fresh by accident. This measures 1,000 blocks of age against a window of 259,200.
	_, err = srv.DeleteMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.MsgDeleteMiner{Creator: creator, MinerId: "mSeat"})
	require.Error(t, err, "seat anchored on an open audit: the exit is refused — releasing yourself means voting, not fleeing")
	_, gErr := f.keeper.Miner.Get(f.ctx, "mSeat")
	require.NoError(t, gErr, "the juror stays registered")
	require.Equal(t, int64(0), f.bank.balOf(creatorAcc).AmountOf("udndr").Int64(), "and nothing was refunded")
}

// The benign fallback gets its own test.
// `eligibleAsJuror` returns true when it knows neither a last commit nor a registration height
// (imported or genesis state): without that fallback, a migration would empty every jury at once. It
// is deliberate, so it is pinned, separately, so that no other test passes thanks to it silently.
func TestDeleteMinerRefusedOnAnchoredSeat_UndatedFallsBackToEligible(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_sedec_fallbk")))
	require.NoError(t, err)
	// No date at all, neither MinerLastCommitHeight nor MinerRegisteredHeight: that is the point.
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mUndated", types.Miner{MinerId: "mUndated", Creator: creator, Stake: 700}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jFallback", "mUndated,mJ2"))
	require.NoError(t, gvSetJob(f, f.ctx, "jFallback", types.Job{
		JobId: "jFallback", State: "open+paid+optimistic+disputed", MinerId: "mOther",
	}))

	_, dErr := srv.DeleteMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.MsgDeleteMiner{Creator: creator, MinerId: "mUndated"})
	require.Error(t, dErr,
		"with no date at all, `eligibleAsJuror` grants the benefit of the doubt: the seat still holds. "+
			"If this test ever turns red, the fallback changed — and a migration would empty the juries.")
}

// (takes effect) The primary of an unresolved disputed job does not leave with its stake: that is the
// "exit before the verdict" dodge that made the slash moot (lying became 0-EV). With no HeldFee and no
// seat, only clause (c), the dispute itself, holds it.

func TestDeleteMinerRefusedForDisputedPrimary(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creatorAcc := sdk.AccAddress([]byte("creator_sedec_prim__"))
	creator, err := f.addressCodec.BytesToString(creatorAcc)
	require.NoError(t, err)
	f.bank.setBalance(creatorAcc, sdk.NewCoins())

	require.NoError(t, gvSetMiner(f, f.ctx, "mPrim", types.Miner{MinerId: "mPrim", Creator: creator, Stake: 700}))
	require.NoError(t, gvSetJob(f, f.ctx, "jDp", types.Job{
		JobId: "jDp", State: "open+paid+optimistic+disputed", MinerId: "mPrim", DisputeBond: 50000,
	}))

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mPrim"})
	require.Error(t, err, "an open dispute on its work: its stake stays slashable until the verdict")
	_, gErr := f.keeper.Miner.Get(f.ctx, "mPrim")
	require.NoError(t, gErr)
	require.Equal(t, int64(0), f.bank.balOf(creatorAcc).AmountOf("udndr").Int64())
}

// (stays silent, the other side) No open obligation (job resolved, audit seat closed): the exit stays a
// clean exit, with the stake returned in full. Without this side, the guard could become a disguised
// confiscation.
func TestDeleteMinerFreeExitStillWorks(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creatorAcc := sdk.AccAddress([]byte("creator_sedec_free__"))
	creator, err := f.addressCodec.BytesToString(creatorAcc)
	require.NoError(t, err)
	f.bank.setBalance(creatorAcc, sdk.NewCoins())

	require.NoError(t, gvSetMiner(f, f.ctx, "mFree", types.Miner{MinerId: "mFree", Creator: creator, Stake: 700}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jRes", "mFree,mJ2,mJ3"))
	require.NoError(t, gvSetJob(f, f.ctx, "jRes", types.Job{ // resolved: neither (b) nor (c) holds
		JobId: "jRes", State: "open+paid+optimistic+disputed+resolved+vindicated+quorum", MinerId: "mFree",
	}))

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mFree"})
	require.NoError(t, err, "instance closed: the exit becomes possible again, as the error message promises")
	require.Equal(t, int64(700), f.bank.balOf(creatorAcc).AmountOf("udndr").Int64(), "stake returned in full")
	_, gErr := f.keeper.Miner.Get(f.ctx, "mFree")
	require.Error(t, gErr, "the miner has indeed left")
}

// (takes effect, prune side, the second way to disappear) Inactivity eviction shares the guard: an
// inactive disputed primary is not evicted (clause (c)) while the instance is open; otherwise the
// prune would create the same vanished primary as the exit, without any action from the attacker.
// Two-sided: the same inactivity after resolution evicts normally (bond returned).
func TestPruneSparesDisputedPrimary(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.MinerPruneBlocks = 20
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	creatorAcc := sdk.AccAddress([]byte("creator_sedec_prun__"))
	creator, err := f.addressCodec.BytesToString(creatorAcc)
	require.NoError(t, err)
	f.bank.setBalance(creatorAcc, sdk.NewCoins())

	require.NoError(t, gvSetMiner(f, f.ctx, "mPp", types.Miner{MinerId: "mPp", Creator: creator, Stake: 5000}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mPp", uint64(vitH)-21))
	require.NoError(t, gvSetJob(f, f.ctx, "jPp", types.Job{
		JobId: "jPp", State: "open+paid+optimistic+disputed", MinerId: "mPp",
	}))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH).WithHeaderInfo(header.Info{Height: vitH})))
	_, gErr := f.keeper.Miner.Get(f.ctx, "mPp")
	require.NoError(t, gErr, "open dispute: the primary's stake stays seizable, so the prune abstains")

	job, _ := f.keeper.Job.Get(f.ctx, "jPp")
	job.State = job.State + "+resolved"
	require.NoError(t, gvSetJob(f, f.ctx, "jPp", job))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH+100).WithHeaderInfo(header.Info{Height: vitH + 100})))
	_, gErr = f.keeper.Miner.Get(f.ctx, "mPp")
	require.Error(t, gErr, "instance closed: inactivity evicts again (the obligation held it, not a punishment)")
	require.Equal(t, int64(5000), f.bank.balOf(creatorAcc).AmountOf("udndr").Int64(), "bond returned at eviction: nothing confiscated")
}
