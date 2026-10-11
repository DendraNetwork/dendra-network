package keeper_test

import (
	"reflect"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The juror lock, instrumented. A design change such as "allow exit after voting" is decided on
// measurements, not on intuition. Because of the exit guard, a seat anchored on an unresolved audit
// withholds its holder's stake: "locked is not seized" belongs to the same family of promises as
// "withheld is not lost", and a promise nothing can contradict is worth nothing.
// The two figures that can contradict it live in held-summary:
//   - `jurors_locked_count` growing: the guard holds a significant share of the juror pool;
//   - `oldest_lock_age_blocks` only ever rising: honest jurors are paying for the silence of others
//     (unbounded deferral), which makes exit-after-vote a decision based on data rather than a
//     debate.
// The date lives in CommitteeSince, the twin of AuditCommittee, written by anchorCommittee (both
// production draws go through it) and purged by clearAuditCommittee.

// The measurement counts the distinct miners holding seats on unresolved audits and nothing else: a
// resolved job withholds nobody, the `__humandispute` key summons nobody (its value is the disputer's
// address), a committee anchored before the twin existed counts towards the number but not towards the
// age (no seniority is invented), and a juror seated twice counts once (the lock is per miner, not per
// seat).
func TestHeldSummaryCountsLockedJurors(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	// Empty store: zero everywhere, with no invented lock. The sentinel, however, is present: in this
	// "everything at zero" case it is the only proof that those zeros were measured. A sentinel that
	// was itself 0 here would be omitted by the codec and would prove nothing.
	resp, err := qs.HeldSummary(f.ctx, &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Zero(t, resp.JurorsLockedCount)
	require.Zero(t, resp.OldestLockAgeBlocks)
	require.Equal(t, uint64(3), resp.PocketsMeasured,
		"the sentinel is always non-zero: \"0 measured\" stays provable despite proto3 omitting zero values")

	// jL1: open audit, main committee dated (h=40) plus redo committee dated (h=60); mC sits on both.
	require.NoError(t, gvSetJob(f, f.ctx, "jL1", types.Job{JobId: "jL1", State: "open+paid+optimistic+disputed", MinerId: "mP1"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jL1", "mA,mB,mC"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jL1", 40))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jL1__redo", "mC,mD"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jL1__redo", 60))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jL1__humandispute", "cosmos1disputeraddress"))
	// jL2: resolved audit, whose seats withhold nobody (same filter as hasOpenObligation).
	require.NoError(t, gvSetJob(f, f.ctx, "jL2", types.Job{JobId: "jL2", State: "open+paid+optimistic+disputed+resolved+vindicated+quorum", MinerId: "mP2"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jL2", "mX,mY,mZ"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jL2", 10))
	// jL3: open audit, committee anchored before the twin existed (no date): counted, never dated.
	require.NoError(t, gvSetJob(f, f.ctx, "jL3", types.Job{JobId: "jL3", State: "open+paid+optimistic+disputed", MinerId: "mP3"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jL3", "mE"))

	ctx100 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100)
	resp, err = qs.HeldSummary(ctx100, &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(5), resp.JurorsLockedCount,
		"mA,mB,mC,mD,mE: distinct miners (mC counts once), resolved jL2 excluded, the __humandispute disputer excluded")
	require.Equal(t, uint64(60), resp.OldestLockAgeBlocks,
		"the oldest dated anchor still open: jL1 at h=40 (100-40) — jL2 (h=10) is closed and no longer ages")
	require.Equal(t, "jL1", resp.OldestLockJobId)
}

// anchorCommittee writes the seat and its date together (the twin, on the Set side). Without this
// test the twin could exist only on the read path: a green query over dates that were never written
// gives an age of zero forever, making the lock invisible by construction, the same defect that
// TestSettleOptimisticDatesTheRetention forbids for HeldSince. Both production draws go through this
// primitive (audit_sampling.go and redo_committee.go both call anchorCommittee; no bare Set of seats
// remains).
func TestAnchorCommitteeDatesTheLock(t *testing.T) {
	f := initFixture(t)
	ctx7 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(7)
	require.NoError(t, f.keeper.AnchorCommitteeForTest(ctx7, "jAn", "m1,m2,m3"))

	members, err := f.keeper.AuditCommittee.Get(f.ctx, "jAn")
	require.NoError(t, err)
	require.Equal(t, "m1,m2,m3", members)
	since, err := f.keeper.CommitteeSince.Get(f.ctx, "jAn")
	require.NoError(t, err)
	require.Equal(t, uint64(7), since, "the date is born with the seat, at the anchoring height")
}

// The date leaves with the seat (the twin, on the Remove side, through the production resolution path:
// the EndBlocker calls clearAuditCommittee). An orphan date would make `oldest_lock_age` wrong: an
// eternal lock displayed on a closed audit, an alarm ringing over nothing, the opposite of the intended
// signal.
func TestAuditResolutionRemovesTheLockDate(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	require.NoError(t, gvSetMiner(f, f.ctx, "mPrim17", types.Miner{MinerId: "mPrim17", Stake: aeStake}))
	sedSeats(f, t, "jClr", "kj", 10) // 10 "0" verdicts: the bar is met, so the audit resolves
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jClr", 2))
	require.NoError(t, gvSetJob(f, f.ctx, "jClr", types.Job{
		JobId: "jClr", State: "open+paid+optimistic+disputed", MinerId: "mPrim17", Fee: 1000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jClr")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jClr")
	require.Contains(t, job.State, "resolved", "the audit resolved (precondition of this test)")
	_, cErr := f.keeper.AuditCommittee.Get(f.ctx, "jClr")
	require.Error(t, cErr, "seats purged at resolution")
	_, sErr := f.keeper.CommitteeSince.Get(f.ctx, "jClr")
	require.Error(t, sErr, "the twin: the date leaves with the seat — no ghost lock survives the audit")
}

// The sentinel is guarded in both directions. A guard that compares the constant to its own literal
// value is tautological, and the rule "increment it for every new pocket" would then live only in a
// comment. It lives instead in two red tests, one per direction of divergence.

// (direction 1: the constant cannot exceed reality) The 3 pockets are each populated with a non-zero
// value, then: sentinel == number of non-empty groups in the response. The right-hand side comes from
// the data, not from the same constant. A sentinel announcing a 4th pocket the handler never emits
// would face 3 groups and turn red. And if a 4th pocket is emitted with the constant at 4, this test
// also breaks until its group is declared below, so the enumeration is updated under a red test, not
// from memory.
func TestSentinelMatchesPopulatedPockets(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	// pocket 1: a held fee; pocket 2: an escrowed bond; pocket 3: a locked juror.
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jP1", 100))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jP1", 1))
	require.NoError(t, gvSetJob(f, f.ctx, "jP2", types.Job{
		JobId: "jP2", State: "open+paid+optimistic+disputed", MinerId: "m2", DisputeBond: 500,
	}))
	require.NoError(t, gvSetJob(f, f.ctx, "jP3", types.Job{
		JobId: "jP3", State: "open+paid+optimistic+disputed", MinerId: "m3",
	}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jP3", "mL1"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jP3", 2))

	resp, err := qs.HeldSummary(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(10), &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	groups := uint64(0)
	if resp.HeldTotal > 0 || resp.HeldCount > 0 {
		groups++ // pocket 1: held fee
	}
	if resp.BondEscrowedTotal > 0 || resp.BondEscrowedCount > 0 {
		groups++ // pocket 2: escrowed bond
	}
	if resp.JurorsLockedCount > 0 || resp.OldestLockAgeBlocks > 0 {
		groups++ // pocket 3: locked jurors
	}
	require.Equal(t, uint64(3), groups, "precondition: this fixture does populate all 3 pockets")
	require.Equal(t, groups, resp.PocketsMeasured,
		"the sentinel equals the groups actually emitted — never compared to its own literal value")
}

// (direction 2: the constant cannot fall below reality) A 4th pocket added without incrementing the
// sentinel leaves everything green, including the test above, whose enumeration only counts the known
// groups (3 == 3). On the client side that is still safe (the pocket reads as None, never as a false
// zero), but the measurement guarantee silently becomes false. Shape lock: the response currently has
// 10 fields (9 data fields plus the sentinel). Adding any field breaks this test, which brings the
// author here and states the rule as a red test: a new pocket means incrementing `heldSummaryPockets`
// and declaring its group in the direction-1 test; a new field on an existing pocket means updating
// only the count below. Still manual, but a manual step that fails loudly.
func TestSentinelShapeLockOnResponseStruct(t *testing.T) {
	require.Equal(t, 10, reflect.TypeOf(types.QueryHeldSummaryResponse{}).NumField(),
		"the QueryHeldSummaryResponse struct changed shape: a new pocket -> increment "+
			"heldSummaryPockets (query_held_summary.go) and declare its group in "+
			"TestSentinelMatchesPopulatedPockets; a field on an existing pocket -> update this count. "+
			"In the same change.")
}

// The lock age survives migration (exported as elapsed time, re-anchored at import, clamped on the
// old side), with the same rule as TestGenesisHeldSinceTravelsAsAge: a lock that got younger by
// migrating would silence `oldest_lock_age` when a long-held juror most deserves to be seen.
func TestGenesisLockAgeTravelsAsElapsed(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, gvSetJob(f, f.ctx, "jMig", types.Job{JobId: "jMig", State: "open+paid+optimistic+disputed", MinerId: "mP"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jMig", "mA,mB"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jMig", 40))

	exported, err := f.keeper.ExportGenesis(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100))
	require.NoError(t, err)
	require.Len(t, exported.CommitteeSinceElapsed, 1)
	require.Equal(t, "jMig", exported.CommitteeSinceElapsed[0].Key)
	require.Equal(t, uint64(60), exported.CommitteeSinceElapsed[0].Value, "exported as an age (100-40), never as an absolute height")

	f2 := initFixture(t)
	require.NoError(t, f2.keeper.InitGenesis(sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(1), *exported))
	qs2 := keeper.NewQueryServerImpl(f2.keeper)
	resp, err := qs2.HeldSummary(sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(30), &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(2), resp.JurorsLockedCount, "the committee travelled (audit_committee): the lock persists")
	require.GreaterOrEqual(t, resp.OldestLockAgeBlocks, uint64(29),
		"after import the lock age keeps rising (clamped on the old side): the measurement does not get younger by migrating")
}
