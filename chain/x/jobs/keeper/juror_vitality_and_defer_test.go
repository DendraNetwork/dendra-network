package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-034: guards against a family of silent failures in the miner lifecycle.
//
// Each test is written to fail against the code that lacked the corresponding guard and, when the guard
// has two sides, comes with the case that must stay silent, so the guard cannot degenerate into a
// blanket refusal.

// C2: vitality must not be self-issued. A Sybil cannot refresh its vitality by referencing a real job it
// is not assigned to; checking only that the job exists is not enough.

// c2Setup builds a job with a revealed beacon (seed "s"), three committee members with high stake, and a
// Sybil at stake 1 (sorted last, so never in a committee of size 3). It returns the creator address.
func c2Setup(f *fixture, t *testing.T) string {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c2_vitality_")))
	require.NoError(t, err)
	for _, id := range []string{"m1", "m2", "m3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: aeStake, Operator: creator, Creator: creator}))
	}
	// Sybil: stake 1, a score about 1e12x larger, so it is excluded from the top 3 deterministically.
	require.NoError(t, gvSetMiner(f, f.ctx, "mSybil", types.Miner{MinerId: "mSybil", Stake: 1, Operator: creator, Creator: creator}))
	require.NoError(t, gvSetJob(f, f.ctx, "jReal", types.Job{JobId: "jReal", State: "open", Fee: 100000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jReal", types.Beacon{JobId: "jReal", Seed: "s"}))
	return creator
}

// (C2, the guard fires) the Sybil posts `jReal__mSybil`: the job exists (an existence-only guard would
// accept), but the Sybil is not in the assigned committee, so no vitality may be dated for it.
func TestC2_VitalityDeniedToUnassignedSybil(t *testing.T) {
	f := initFixture(t)
	creator := c2Setup(f, t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jReal__mSybil", ResultCommit: "x"})
	require.NoError(t, err, "the commit itself is never blocked (only the vitality timestamp is)")

	_, gErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, "mSybil")
	require.Error(t, gErr,
		"an unassigned Sybil must never buy vitality for the price of gas")
}

// (C2, the guard stays silent) a real committee member posts `jReal__m1`: its vitality must be dated.
// Without this case, C2 could degenerate into "never date anything", which starves honest miners.
func TestC2_VitalityGrantedToAssignedCommitteeMember(t *testing.T) {
	f := initFixture(t)
	creator := c2Setup(f, t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	// m1/m2/m3 form the committee; the one actually drawn is found by posting a commit for each. At least
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

// M6: a `__redo__` re-adjudication is productive work. A gate that only trims the `__verdict` suffix
// never dates redo jurors, which shrinks the juror pool towards nothing. A `__redo__` commit must date.
func TestM6_RedoCommitGrantsVitality(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_m6_redo_____")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mJudge", types.Miner{MinerId: "mJudge", Stake: aeStake, Operator: creator, Creator: creator}))
	require.NoError(t, gvSetJob(f, f.ctx, "jR", types.Job{JobId: "jR", State: "open+paid+optimistic+disputed"}))
	// mJudge is anchored in the re-adjudication committee (key jR__redo).
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jR__redo", "mJudge"))

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err = srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jR__redo__mJudge", ResultCommit: "1"})
	require.NoError(t, err)

	h, gErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, "mJudge")
	require.NoError(t, gErr, "a re-adjudication juror produces: it must be dated, otherwise the juror pool dies out")
	require.Equal(t, uint64(vitH), h)
}

// C1: a seat in a resolved audit does not protect against eviction. An AuditCommittee that is never
// purged would keep any miner that once held a seat from ever being pruned, and the guard would only catch
// miners that had never been drawn, the opposite of its target.

// (C1, the guard fires) mSeat holds a seat in the committee of a resolved job. Registered long ago and
// never productive, it is the target: a seat in a closed audit must no longer protect it, so it is evicted.
func TestC1_ResolvedCommitteeSeatDoesNotImmunize(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1_resolved_")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mSeat", types.Miner{MinerId: "mSeat", Stake: aeStake, Operator: creator, Creator: creator}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mSeat", vitAncient)) // registered long ago, never committed
	// seat in the committee of an already resolved job.
	require.NoError(t, gvSetJob(f, f.ctx, "jDone", types.Job{JobId: "jDone", State: "open+paid+optimistic+resolved"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jDone", "mSeat"))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mSeat")
	require.Error(t, gErr,
		"a seat in a resolved audit must not immunise, otherwise that miner is immortal")
}

// (C1, the guard stays silent) The invariant "a summonable juror does not desert its seat" holds, but
// it can only be stated on the exit path, not on the prune path.
//
// It cannot be stated on the prune because `pruneInactiveMiners` only considers miners silent beyond
// `minerPruneBlocks`, and `Params.Validate` enforces `miner_prune_blocks >= juror_freshness_blocks`. By
// that invariant every prune candidate is already outside the juror window, so the case "summonable
// juror that is a prune candidate" does not exist, and testing it would test a forbidden combination.
//
// Voluntary desertion remains possible and remains forbidden: a fresh juror asking to leave while it
// sits in an open audit must be refused. That is what this test checks.
func TestC1_ActiveCommitteeSeatStillProtects(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1_active___")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mSeat", types.Miner{MinerId: "mSeat", Stake: aeStake, Operator: creator, Creator: creator}))
	// Fresh: commit anchored 1000 blocks ago, so `eligibleAsJuror` still summons it.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mSeat", uint64(vitH-1000)))
	require.NoError(t, gvSetJob(f, f.ctx, "jLive", types.Job{JobId: "jLive", MinerId: "mOther", State: "open+paid+optimistic+disputed"})) // not resolved
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jLive", "mSeat"))

	_, dErr := srv.DeleteMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.MsgDeleteMiner{Creator: creator, MinerId: "mSeat"})
	require.Error(t, dErr,
		"a summonable juror of an ongoing audit must never be able to desert: the way out is to vote, not to flee")
	_, gErr := f.keeper.Miner.Get(f.ctx, "mSeat")
	require.NoError(t, gErr, "it stays registered")
}

// (C1-bis) The other side: an expired juror sitting in an unresolved audit is evicted. Otherwise, an
// identity whose key is lost stays in the registry forever and keeps the quorum bar high for the whole
// network, so dead identities hold open audits and no path can remove them.
func TestC1bis_ExpiredJurorSeatIsPrunable(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_c1bis_stale__")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mDead", types.Miner{MinerId: "mDead", Stake: aeStake, Operator: creator, Creator: creator}))
	// Expired: last commit at block 1, evaluated at block 700000, so `eligibleAsJuror` refuses to summon it.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mDead", vitAncient))
	require.NoError(t, gvSetJob(f, f.ctx, "jFrozen", types.Job{JobId: "jFrozen", MinerId: "mOther", State: "open+paid+optimistic+disputed"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jFrozen", "mDead"))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mDead")
	require.Error(t, gErr,
		"a seat the protocol already refuses to summon must no longer immunise: keeping it serves nothing "+
			"and blocks everyone's quorum")
}

// C3: an audit already won in the lottery is not drawn again. A deferral for want of a jury must mark
// the job, and the retry must not replay the lottery: at 5000 bps that would cancel the audit one time
// in two, an escape hatch.

// (C3-a) A draw won but deferred for want of a jury sets the marker and does not release the payment.
func TestC3_DeferForSmallJurySetsSelectedMarkerAndHolds(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // lottery always won, so selection is certain
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	// the primary is alone in the registry: eligible jury = 0 < floor of 4, so deferral.
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake}))
	require.NoError(t, gvSetJob(f, f.ctx, "jA", types.Job{JobId: "jA", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jA", uint64(100000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jA")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	sel, sErr := f.keeper.AuditSelected.Has(f.ctx, "jA")
	require.NoError(t, sErr)
	require.True(t, sel, "an audit won then deferred must be marked selected, otherwise the retry replays the lottery")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jA")
	require.NoError(t, hErr, "the payment stays held during the deferral (never released without verification)")
	require.Equal(t, uint64(100000), held)
}

// (C3-b) Marker set plus a lottery that would say no (bps 0): the job stays selected and held instead of
// being released. The counterpoint, where without the marker a no from the lottery releases, is what
// let the audit escape on retry.
func TestC3_MarkerOverridesNegativeLotteryOnRetry(t *testing.T) {
	base := func(t *testing.T, withMarker bool) *fixture {
		f := initFixture(t)
		p := types.DefaultParams()
		p.VerificationMode = 1
		p.AuditSampleBps = 0 // lottery: never selected by the draw
		p.AuditMinQuorum = 4
		p.AuditResolveTimeout = 240
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		op, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_mP_c3_bbbb_")))
		require.NoError(t, err)
		// mP has a valid operator: without the marker, releaseHeld must really be able to pay out and then
		// clear the hold (otherwise a failed transfer would hide the behaviour difference being measured).
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
	require.NoError(t, hErr, "with the marker: the payment stays held -> the audit does not escape")

	fBare := base(t, false)
	_, hErr2 := fBare.keeper.HeldFee.Get(fBare.ctx, "jB")
	require.Error(t, hErr2, "without the marker: a no from the lottery releases -> that was the escape hatch")
}

// M8: burn, then clear. If the burn fails, HeldBurn must be kept (the coins stay tracked), never erased.

// (M8, the guard fires) burn impossible (module unfunded): HeldBurn is kept instead of erased into an
// orphan residue.
func TestM8_FailedBurnKeepsHeldBurn(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 0 // not selected: releaseHeld (optimistic settlement)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// module deliberately unfunded, so BurnCoins will fail.
	require.NoError(t, gvSetJob(f, f.ctx, "jBurn", types.Job{JobId: "jBurn", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jBurn", uint64(50000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jBurn")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	hb, hErr := f.keeper.HeldBurn.Get(f.ctx, "jBurn")
	require.NoError(t, hErr, "failed burn -> HeldBurn kept; erasing it leaves coins neither burned nor tracked")
	require.Equal(t, uint64(50000), hb)
}

// (M8, the guard stays silent) burn possible (module funded): HeldBurn cleared and supply reduced.
// Without this case, M8 could degenerate into "never burn".
func TestM8_SuccessfulBurnRemovesHeldBurn(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f) // module funded: the burn succeeds
	require.NoError(t, gvSetJob(f, f.ctx, "jBurn", types.Job{JobId: "jBurn", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jBurn", uint64(50000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jBurn")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, hErr := f.keeper.HeldBurn.Get(f.ctx, "jBurn")
	require.Error(t, hErr, "successful burn -> HeldBurn cleared")
	require.Equal(t, "50000", f.bank.burned.AmountOf("udndr").String(), "the supply must have been reduced by 50000")
}

// M7: arm the vitality guard again after an import. A miner imported without MinerRegisteredHeight
// (which is not exported) could never be pruned; InitGenesis must date it at the import height.
func TestM7_InitGenesisDatesImportedMiners(t *testing.T) {
	f := initFixture(t)
	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_m7_import___")))
	require.NoError(t, err)
	gs := types.DefaultGenesis()
	// The identifier is derived from the same bytes the creator encodes (ADR-039): since genesis now
	// enforces the rule the message path already did, a fixture naming its miner by hand describes a
	// registry entry the chain cannot produce.
	mImp, dErr := types.DeriveMinerID([]byte("creator_m7_import___"))
	require.NoError(t, dErr)
	gs.MinerMap = []types.Miner{{MinerId: mImp, Stake: aeStake, Operator: creator, Creator: creator}}

	const importH = int64(500000)
	require.NoError(t, f.keeper.InitGenesis(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(importH), *gs))

	reg, gErr := f.keeper.MinerRegisteredHeight.Get(f.ctx, mImp)
	require.NoError(t, gErr, "an imported miner must be dated, otherwise it is unprunable for life")

	// The date is one block below the import height, and that offset is the point.
	// `initH` itself ties with a `create-miner` landing in the first finalised block (on a normal
	// restart the InitChain header is that block), and `minerInFrozenPool` is `reg <= freeze`, so a
	// tie admits a newcomer into the frozen pool of every imported job. The anchor therefore sits
	// strictly below the first height at which any transaction can run. See `frozenPoolAnchor`.
	require.Equal(t, keeper.FrozenPoolAnchorForTest(importH), reg,
		"the import anchor must sit strictly below the first height at which a transaction can run")
	require.Less(t, reg, uint64(importH), "and it must be strictly below, or the sybil window opens")
}
