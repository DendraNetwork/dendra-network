package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-034 — GUARDS AGAINST A FAMILY OF SILENT FAILURES IN THE MINER LIFECYCLE.
//
// Each test is written to FAIL against the code that lacked the corresponding guard, and — when the guard
// has two faces — comes with the case that must STAY SILENT, so the guard cannot degenerate into a blanket
// refusal.

// ---------------------------------------------------------------------------------------------------------
// C2 — VITALITY MUST NOT BE SELF-ISSUED. A Sybil cannot refresh its vitality by referencing a REAL job it is
// not assigned to. Checking only that the job EXISTS is not enough.
// ---------------------------------------------------------------------------------------------------------

// c2Setup builds a job with a revealed beacon (seed "s"), three committee members with HIGH stake, and a
// Sybil at stake 1 (sorted last -> NEVER in a committee of size 3). Returns the creator address.
func c2Setup(f *fixture, t *testing.T) string {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c2_vitality_")))
	require.NoError(t, err)
	for _, id := range []string{"m1", "m2", "m3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: aeStake, Operator: creator, Creator: creator}))
	}
	// Sybil: stake 1 -> score ~1e12x larger -> deterministically excluded from the top 3.
	require.NoError(t, gvSetMiner(f, f.ctx, "mSybil", types.Miner{MinerId: "mSybil", Stake: 1, Operator: creator, Creator: creator}))
	require.NoError(t, gvSetJob(f, f.ctx, "jReal", types.Job{JobId: "jReal", State: "open", Fee: 100000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jReal", types.Beacon{JobId: "jReal", Seed: "s"}))
	return creator
}

// (C2, the guard bites) the Sybil posts `jReal__mSybil`: the job EXISTS (an existence-only guard would
// accept), but it is NOT in the assigned committee -> NO vitality may be dated for it.
func TestC2_VitalityDeniedToUnassignedSybil(t *testing.T) {
	f := initFixture(t)
	creator := c2Setup(f, t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jReal__mSybil", ResultCommit: "x"})
	require.NoError(t, err, "the commit itself is never blocked (only the vitality TIMESTAMP is)")

	_, gErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, "mSybil")
	require.Error(t, gErr,
		"an unassigned Sybil must NEVER buy vitality for the price of gas")
}

// (C2, the guard stays silent) a REAL committee member posts `jReal__m1`: its vitality MUST be dated.
// Without this case, C2 could degenerate into a disguised "never date anything" that starves honest miners.
func TestC2_VitalityGrantedToAssignedCommitteeMember(t *testing.T) {
	f := initFixture(t)
	creator := c2Setup(f, t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	// m1/m2/m3 form the committee; the one actually drawn is found by posting a commit for each — at least
	// one is a member, and that member must be dated.
	granted := false
	for _, id := range []string{"m1", "m2", "m3"} {
		_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jReal__" + id, ResultCommit: "x"})
		require.NoError(t, err)
		if h, gErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, id); gErr == nil {
			require.Equal(t, uint64(vitH), h)
			granted = true
		}
	}
	require.True(t, granted, "at least one assigned committee member must have its vitality dated (the guard is not punitive)")
}

// ---------------------------------------------------------------------------------------------------------
// M6 — A `__redo__` RE-ADJUDICATION IS PRODUCTIVE WORK. A gate that only trims the `__verdict` suffix never
// dates redo jurors, which starves the juror pool into a death spiral. A `__redo__` commit must date.
// ---------------------------------------------------------------------------------------------------------
func TestM6_RedoCommitGrantsVitality(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_m6_redo_____")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mJuge", types.Miner{MinerId: "mJuge", Stake: aeStake, Operator: creator, Creator: creator}))
	require.NoError(t, gvSetJob(f, f.ctx, "jR", types.Job{JobId: "jR", State: "open+paid+optimistic+disputed"}))
	// mJuge is ANCHORED in the re-adjudication committee (key jR__redo).
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jR__redo", "mJuge"))

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err = srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jR__redo__mJuge", ResultCommit: "1"})
	require.NoError(t, err)

	h, gErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, "mJuge")
	require.NoError(t, gErr, "a re-adjudication juror PRODUCES: it must be dated, otherwise the juror pool dies out")
	require.Equal(t, uint64(vitH), h)
}

// ---------------------------------------------------------------------------------------------------------
// C1 — A SEAT IN A RESOLVED AUDIT NO LONGER IMMUNISES AGAINST EVICTION. AuditCommittee was never purged, so
// any miner that once held a seat was unprunable FOR LIFE, and the guard bit only on miners that had never
// been drawn — the exact opposite of its target.
// ---------------------------------------------------------------------------------------------------------

// (C1, the guard bites) mSeat holds a seat in the committee of a RESOLVED job. Registered long ago, never
// productive: it is the target. A seat in a closed audit must no longer protect it -> it is evicted.
func TestC1_ResolvedCommitteeSeatDoesNotImmunize(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1_resolved_")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mSeat", types.Miner{MinerId: "mSeat", Stake: aeStake, Operator: creator, Creator: creator}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mSeat", vitAncient)) // registered long ago, never committed
	// seat in the committee of an ALREADY RESOLVED job.
	require.NoError(t, gvSetJob(f, f.ctx, "jDone", types.Job{JobId: "jDone", State: "open+paid+optimistic+resolved"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jDone", "mSeat"))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mSeat")
	require.Error(t, gErr,
		"a seat in a RESOLVED audit must not immunise, otherwise that miner is immortal")
}

// (C1, the guard stays silent) The invariant — "a summonable juror does not desert its seat" — holds, but
// it can only be stated on the exit path, not on the prune path.
//
// WHY IT CANNOT BE STATED ON THE PRUNE. `pruneInactiveMiners` only considers miners silent beyond
// `minerPruneBlocks`, and `Params.Validate` enforces `miner_prune_blocks >= juror_freshness_blocks`. By
// that invariant every prune candidate is already outside the juror window: the case "SUMMONABLE juror
// that is a prune candidate" does not exist. Testing it would test a combination the protocol forbids.
//
// Voluntary desertion, on the other hand, remains possible and remains forbidden: a FRESH juror asking to
// leave while it sits in an open audit must be refused. That is what this test verifies.
func TestC1_ActiveCommitteeSeatStillProtects(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1_active___")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mSeat", types.Miner{MinerId: "mSeat", Stake: aeStake, Operator: creator, Creator: creator}))
	// FRESH: commit anchored 1000 blocks ago -> `eligibleAsJuror` still summons it.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mSeat", uint64(vitH-1000)))
	require.NoError(t, gvSetJob(f, f.ctx, "jLive", types.Job{JobId: "jLive", MinerId: "mAutre", State: "open+paid+optimistic+disputed"})) // NOT resolved
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jLive", "mSeat"))

	_, dErr := srv.DeleteMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.MsgDeleteMiner{Creator: creator, MinerId: "mSeat"})
	require.Error(t, dErr,
		"a SUMMONABLE juror of an ongoing audit must never be able to desert: the way out is to vote, not to flee")
	_, gErr := f.keeper.Miner.Get(f.ctx, "mSeat")
	require.NoError(t, gErr, "it stays registered")
}

// (C1-bis) THE OTHER FACE: an EXPIRED juror sitting in an unresolved audit IS evicted. Without it, an
// identity whose key is lost stays in the registry FOR LIFE and keeps the quorum bar high for the whole
// network — dead identities immobilise open audits and no path can remove them.
func TestC1bis_ExpiredJurorSeatIsPrunable(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1bis_stale__")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mMort", types.Miner{MinerId: "mMort", Stake: aeStake, Operator: creator, Creator: creator}))
	// EXPIRED: last commit at block 1, evaluated at block 700000 -> `eligibleAsJuror` refuses to summon it.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mMort", vitAncient))
	require.NoError(t, gvSetJob(f, f.ctx, "jFige", types.Job{JobId: "jFige", MinerId: "mAutre", State: "open+paid+optimistic+disputed"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jFige", "mMort"))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mMort")
	require.Error(t, gErr,
		"a seat the protocol already refuses to summon must no longer immunise: keeping it serves nothing "+
			"and blocks everyone's quorum")
}

// ---------------------------------------------------------------------------------------------------------
// C3 — AN AUDIT ALREADY WON IN THE LOTTERY IS NOT REDRAWN. A deferral for want of a jury must MARK the job,
// and the retry must not replay the lottery — at 5000 bps that would cancel the audit one time in two, which
// is an escape hatch.
// ---------------------------------------------------------------------------------------------------------

// (C3-a) a draw won but deferred for want of a jury SETS the marker and does NOT release the payment.
func TestC3_DeferForSmallJurySetsSelectedMarkerAndHolds(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // lottery always won -> selection is certain
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	// the primary is alone in the registry -> eligible jury = 0 < floor of 4 -> deferral.
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake}))
	require.NoError(t, gvSetJob(f, f.ctx, "jA", types.Job{JobId: "jA", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jA", uint64(100000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jA")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	sel, sErr := f.keeper.AuditSelected.Has(f.ctx, "jA")
	require.NoError(t, sErr)
	require.True(t, sel, "an audit won then deferred must be MARKED selected, otherwise the retry replays the lottery")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jA")
	require.NoError(t, hErr, "the payment stays HELD during the deferral (never released without verification)")
	require.Equal(t, uint64(100000), held)
}

// (C3-b) marker set plus a lottery that would say NO (bps 0): the job stays selected and HELD instead of
// being released. The counterpoint — without the marker, a NO from the lottery releases — is what let the
// audit escape on retry.
func TestC3_MarkerOverridesNegativeLotteryOnRetry(t *testing.T) {
	base := func(t *testing.T, withMarker bool) *fixture {
		f := initFixture(t)
		p := types.DefaultParams()
		p.VerificationMode = 1
		p.AuditSampleBps = 0 // lottery: NEVER selected by the draw
		p.AuditMinQuorum = 4
		p.AuditResolveTimeout = 240
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		op, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_mP_c3_bbbb_")))
		require.NoError(t, err)
		// mP has a VALID operator: without the marker, releaseHeld must really be able to pay out and then
		// clear the hold (otherwise a failed transfer would mask the behaviour difference being measured).
		require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Operator: op, Creator: op}))
		require.NoError(t, gvSetJob(f, f.ctx, "jB", types.Job{JobId: "jB", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jB", uint64(100000)))
		require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jB")))
		if withMarker {
			require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jB"))
		}
		require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))
		return f
	}

	fMarked := base(t, true)
	_, hErr := fMarked.keeper.HeldFee.Get(fMarked.ctx, "jB")
	require.NoError(t, hErr, "WITH the marker: the payment stays held -> the audit does not escape")

	fBare := base(t, false)
	_, hErr2 := fBare.keeper.HeldFee.Get(fBare.ctx, "jB")
	require.Error(t, hErr2, "WITHOUT the marker: a NO from the lottery releases -> that was the escape hatch")
}

// ---------------------------------------------------------------------------------------------------------
// M8 — BURN, THEN CLEAR. If the burn fails, HeldBurn must be KEPT (the coins stay tracked), never erased.
// ---------------------------------------------------------------------------------------------------------

// (M8, the guard bites) burn impossible (module unfunded) -> HeldBurn KEPT, instead of erased into an
// orphan residue.
func TestM8_FailedBurnKeepsHeldBurn(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 0 // not selected -> releaseHeld (optimistic settlement)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// module DELIBERATELY unfunded -> BurnCoins will fail.
	require.NoError(t, gvSetJob(f, f.ctx, "jBurn", types.Job{JobId: "jBurn", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jBurn", uint64(50000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jBurn")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	hb, hErr := f.keeper.HeldBurn.Get(f.ctx, "jBurn")
	require.NoError(t, hErr, "failed burn -> HeldBurn KEPT; erasing it leaves coins neither burned nor tracked")
	require.Equal(t, uint64(50000), hb)
}

// (M8, the guard stays silent) burn possible (module funded) -> HeldBurn cleared AND supply reduced. Without
// this case, M8 could degenerate into "never burn".
func TestM8_SuccessfulBurnRemovesHeldBurn(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f) // module funded -> the burn succeeds
	require.NoError(t, gvSetJob(f, f.ctx, "jBurn", types.Job{JobId: "jBurn", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jBurn", uint64(50000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jBurn")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, hErr := f.keeper.HeldBurn.Get(f.ctx, "jBurn")
	require.Error(t, hErr, "successful burn -> HeldBurn cleared")
	require.Equal(t, "50000", f.bank.burned.AmountOf("udndr").String(), "the supply must have been reduced by 50000")
}

// ---------------------------------------------------------------------------------------------------------
// M7 — RE-ARM THE VITALITY GUARD AFTER AN IMPORT. A miner imported without MinerRegisteredHeight (which is
// not exported) is unprunable for life; InitGenesis must date it at the import height.
// ---------------------------------------------------------------------------------------------------------
func TestM7_InitGenesisDatesImportedMiners(t *testing.T) {
	f := initFixture(t)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_m7_import___")))
	require.NoError(t, err)
	gs := types.DefaultGenesis()
	// The identifier is DERIVED from the same bytes the creator encodes (ADR-039): since the genesis
	// door enforces the rule the message door already did, a fixture naming its miner by hand
	// describes a registry entry the chain cannot produce.
	mImp, dErr := types.DeriveMinerID([]byte("creator_m7_import___"))
	require.NoError(t, dErr)
	gs.MinerMap = []types.Miner{{MinerId: mImp, Stake: aeStake, Operator: creator, Creator: creator}}

	const importH = int64(500000)
	require.NoError(t, f.keeper.InitGenesis(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(importH), *gs))

	reg, gErr := f.keeper.MinerRegisteredHeight.Get(f.ctx, mImp)
	require.NoError(t, gErr, "an imported miner must be dated, otherwise it is unprunable for life")
	require.Equal(t, uint64(importH), reg)
}
