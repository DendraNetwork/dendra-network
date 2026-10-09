package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Coverage for the AssignedCommittee query, the AuditDeferred query, the `+adjudicated` marker and
// `audit_distinct_voters` (ADR-034 partition), the governable vitality windows, and the genesis export
// of AuditSelected / PendingHeldRelease.
//
// Every test names the value that would turn it red, and each two-sided guard is exercised on both
// faces.

// ---------------------------------------------------------------------------------------------------------
// AssignedCommittee query — the query that removes any need for an off-chain replica of the draw.
// ---------------------------------------------------------------------------------------------------------

// (prefix = payment) members[0] of the query MUST be the miner settleOptimistic pays
// (assignedCommittee(.,1)). This is the parity whose silent divergence mis-settles every job on the
// network: if anyone reorders the query (alphabetically, by id, ...), this test turns red. The query
// is compared to the PAYMENT PATH, not to its own implementation, which would be a tautology.
func TestQueryAssignedCommitteePrefixIsThePaidPrimary(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	// Very different stakes: the weighted order (h/stake) diverges from the pure-hash order — exactly
	// the client/chain divergence that equal stakes would hide.
	require.NoError(t, gvSetMiner(f, f.ctx, "mBig", types.Miner{MinerId: "mBig", Stake: 1_000_000_000_000}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mMid", types.Miner{MinerId: "mMid", Stake: 5_000_000}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mLow", types.Miner{MinerId: "mLow", Stake: 60_000}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mTiny", types.Miner{MinerId: "mTiny", Stake: 1}))
	require.NoError(t, gvSetJob(f, f.ctx, "jQ", types.Job{JobId: "jQ", State: "open", Fee: 1000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jQ", types.Beacon{JobId: "jQ", Seed: "abcdef0123456789"}))

	resp, err := qs.AssignedCommittee(f.ctx, &types.QueryAssignedCommitteeRequest{JobId: "jQ"})
	require.NoError(t, err)
	require.Len(t, resp.Members, keeper.CommitteeSize, "full committee when the eligible pool suffices")

	paid, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jQ", 1)
	require.NoError(t, err)
	require.True(t, paid[resp.Members[0]],
		"members[0] must be THE miner settleOptimistic pays, otherwise the client serves X and the chain pays Y")
	// The giant (stake 1e12 against 1) is in the committee: were the query to ignore stake weighting,
	// the pure-hash order could drop it. Stake weighting must survive the query.
	require.Contains(t, resp.Members, "mBig", "stake weighting must survive the query")
}

// (loud refusal, both faces) unrevealed seed -> FailedPrecondition, unknown job -> NotFound.
// NEVER an empty list: a failure indistinguishable from the normal case is the defect that makes an
// outage invisible. A query returning `[]` instead of an error turns this test red.
func TestQueryAssignedCommitteeRefusesLoudly(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	require.NoError(t, gvSetMiner(f, f.ctx, "m1", types.Miner{MinerId: "m1", Stake: 1000}))
	require.NoError(t, gvSetJob(f, f.ctx, "jWait", types.Job{JobId: "jWait", State: "open"}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jWait", types.Beacon{JobId: "jWait", Seed: ""})) // deferred reveal

	_, err := qs.AssignedCommittee(f.ctx, &types.QueryAssignedCommitteeRequest{JobId: "jWait"})
	require.Error(t, err, "unrevealed seed -> ERROR, never an empty committee")
	require.Equal(t, codes.FailedPrecondition, status.Code(err))

	_, err = qs.AssignedCommittee(f.ctx, &types.QueryAssignedCommitteeRequest{JobId: "jInconnu"})
	require.Error(t, err)
	require.Equal(t, codes.NotFound, status.Code(err), "unknown job -> NotFound (the client mistyped the id; it is not an empty committee)")
}

// ---------------------------------------------------------------------------------------------------------
// AuditDeferred query — the `deferred_no_jury` class, the only one that cannot be derived from list-job.
// ---------------------------------------------------------------------------------------------------------

func TestQueryAuditDeferredListsSelectedJobs(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	// Silent face: nothing selected -> empty list, not an error.
	resp, err := qs.AuditDeferred(f.ctx, &types.QueryAuditDeferredRequest{})
	require.NoError(t, err)
	require.Empty(t, resp.JobIds)

	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jDef1"))
	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jDef2"))
	resp, err = qs.AuditDeferred(f.ctx, &types.QueryAuditDeferredRequest{})
	require.NoError(t, err)
	require.ElementsMatch(t, []string{"jDef1", "jDef2"}, resp.JobIds,
		"selected-but-deferred jobs must be VISIBLE; without them the partition sum cannot balance")
}

// ---------------------------------------------------------------------------------------------------------
// PARTITION — `+adjudicated` and `audit_distinct_voters`.
// ---------------------------------------------------------------------------------------------------------

// A successful adjudication must be CLASSIFIABLE (resolved_by_adjudication) and carry the PLURALITY
// behind its decision: here 3 re-commits held by 2 operators, so 2 and not 3 — seats are not operators.
func TestAdjudicationMarksAdjudicatedAndCountsDistinctOperators(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_regen_d01")
	opA, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_regen_opA__")))
	require.NoError(t, err)
	opB, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_regen_opB__")))
	require.NoError(t, err)

	for _, id := range []string{"mCheat", "mB", "mC"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAdj__mCheat", types.Commit{ResultCommit: "1,0,0"}))
	// 3 fresh jurors: mD and mE share operator opA, mF belongs to opB -> 2 distinct operators.
	for id, op := range map[string]string{"mD": opA, "mE": opA, "mF": opB} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1, Operator: op}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAdj__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jAdj", "mD", "mE", "mF")
	require.NoError(t, gvSetJob(f, f.ctx, "jAdj", types.Job{
		JobId: "jAdj", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jAdj"})
	require.NoError(t, err)

	job, err := f.keeper.Job.Get(f.ctx, "jAdj")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "adjudicated"),
		"an adjudicated audit must be CLASSIFIABLE: without the marker, resolved_by_adjudication is invisible and the partition lies")
	require.Equal(t, uint64(3), job.AuditVoters, "3 re-commits counted (denominator of the share)")
	require.Equal(t, uint64(2), job.AuditMaxOperatorVotes, "opA holds 2 votes out of 3 (numerator: >= 50 %, the gate would see it)")
	require.Equal(t, uint64(2), job.AuditDistinctVoters,
		"3 seats held by 2 operators = plurality 2 — counting seats would inflate the MIN the release gate reads")
}

// At the timeout, a quorum of "invalid" verdicts posted by 4 seats held by 3 operators must record
// distinct_voters=3. This is the field the release gate reads as a MIN: a quorum reached by 15 seats
// held by a single operator is a quorum in form only.
func TestResolveTimeoutRecordsDistinctVoterOperators(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opX, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_regen_opX__")))
	require.NoError(t, err)
	opY, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_regen_opY__")))
	require.NoError(t, err)
	opZ, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_regen_opZ__")))
	require.NoError(t, err)

	aeReg(f, t, "mP", aeStake)
	// 4 voting jurors: j1+j2 -> opX, j3 -> opY, j4 -> opZ. Seats = 4, operators = 3.
	for id, op := range map[string]string{"j1": opX, "j2": opX, "j3": opY, "j4": opZ} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: aeStake, Operator: op}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jDV", types.Job{
		JobId: "jDV", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000, DisputeHeight: 1,
	}))
	aeAnchor(f, t, "jDV", "j1", "j2", "j3", "j4")
	for _, j := range []string{"j1", "j2", "j3", "j4"} {
		aeVerdict(f, t, "jDV", j, "0")
	}
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jDV")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jDV")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "quorum"), "quorum reached -> the audit was CONDUCTED")
	require.Equal(t, uint64(3), job.AuditDistinctVoters,
		"4 seats, 3 operators -> 3. Counting 4 would inflate the plurality; 0 means the field is never written")
	require.Equal(t, uint64(4), job.AuditVoters, "4 votes counted (denominator of the share)")
	require.Equal(t, uint64(2), job.AuditMaxOperatorVotes, "opX holds 2 votes -> share 2/4 = 50 % (a gate at < 50 % would see it)")
}

// ---------------------------------------------------------------------------------------------------------
// GOVERNABLE VITALITY WINDOWS — the governed value BITES, the default of 0 STAYS SILENT.
// ---------------------------------------------------------------------------------------------------------

// (bites) JurorFreshnessBlocks=10: a miner whose last commit is 11 blocks old is excluded from the
// audit draw. If `effectiveJurorFreshness` ignored the parameter and used a hard-coded constant, the
// compiled window of about three days would keep it eligible and this test would turn red.
func TestGovernedJurorWindowBites(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.JurorFreshnessBlocks = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	h := int64(1000)
	require.NoError(t, gvSetMiner(f, f.ctx, "mStale", types.Miner{MinerId: "mStale", Stake: 1000}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStale", uint64(h)-11)) // outside the governed window
	require.NoError(t, gvSetMiner(f, f.ctx, "mFresh", types.Miner{MinerId: "mFresh", Stake: 1000}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mFresh", uint64(h)-5)) // inside the window

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(ctx, "jGov", ^uint64(0))) // ADR-037: freeze the pool wide open
	members, err := f.keeper.DrawAuditCommitteeForTest(ctx, "seedgov", "jGov", "mPrim", "")
	require.NoError(t, err)
	require.NotContains(t, members, "mStale", "governed window of 10 blocks: a miner silent for 11 blocks no longer sits")
	require.Contains(t, members, "mFresh", "and the recent miner still sits — the guard knows when to stay quiet")
}

// (stays silent) parameter at 0 means dormant: the compiled constant (about three days) applies and
// the same miner stays eligible. Inverting the meaning of the dormant value (0 = zero-length window)
// would exclude EVERY juror at once, and this test would catch it.
func TestJurorWindowDormantDefaultKeepsCompiledConstant(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // JurorFreshnessBlocks = 0

	h := int64(1000)
	require.NoError(t, gvSetMiner(f, f.ctx, "mStale", types.Miner{MinerId: "mStale", Stake: 1000}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mStale", uint64(h)-11))

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(ctx, "jGov", ^uint64(0))) // ADR-037: freeze the pool wide open
	members, err := f.keeper.DrawAuditCommitteeForTest(ctx, "seedgov", "jGov", "mPrim", "")
	require.NoError(t, err)
	require.Contains(t, members, "mStale", "parameter 0 = compiled constant (~3 days): 11 blocks of silence exclude nobody")
}

// (bites) MinerPruneBlocks=20: a miner silent for 21 blocks, with no open obligation, is evicted at
// the epoch boundary and its bond returned. A hard-coded seven-day constant would never touch it.
func TestGovernedPruneWindowBites(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.MinerPruneBlocks = 20
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_prune_gov___")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mGone", types.Miner{MinerId: "mGone", Stake: 5000, Creator: creator}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mGone", uint64(vitH)-21))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH).WithHeaderInfo(header.Info{Height: vitH})))
	_, gErr := f.keeper.Miner.Get(f.ctx, "mGone")
	require.Error(t, gErr, "governed window of 20 blocks: 21 blocks of silence evict (bond returned to the creator)")

	// (stays silent) same silence, parameter 0 -> compiled ~7-day constant -> NO eviction.
	f2 := initFixture(t)
	require.NoError(t, f2.keeper.Params.Set(f2.ctx, types.DefaultParams()))
	f2.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	require.NoError(t, f2.keeper.Miner.Set(f2.ctx, "mGone", types.Miner{MinerId: "mGone", Stake: 5000, Creator: creator}))
	require.NoError(t, f2.keeper.MinerLastCommitHeight.Set(f2.ctx, "mGone", uint64(vitH)-21))
	require.NoError(t, f2.keeper.EndBlock(sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(vitH).WithHeaderInfo(header.Info{Height: vitH})))
	_, gErr = f2.keeper.Miner.Get(f2.ctx, "mGone")
	require.NoError(t, gErr, "parameter 0 = compiled constant: 21 blocks of silence do NOT evict")
}

// ---------------------------------------------------------------------------------------------------------
// GENESIS — AuditSelected and PendingHeldRelease SURVIVE the export/import round trip.
// ---------------------------------------------------------------------------------------------------------

// A collection absent from GenesisState is lost in silence. Losing AuditSelected replays the lottery
// on the retry, which reopens the one-in-two evasion; losing PendingHeldRelease makes "the held fee
// remains claimable" FALSE after a migration.
func TestGenesisRoundTripCarriesAuditSelectedAndHeldReleaseQueue(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jSel"))
	require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "jDette"))

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.Contains(t, exported.AuditSelected, "jSel", "a selected audit is EXPORTED")
	require.Contains(t, exported.PendingHeldRelease, "jDette", "an outstanding release debt is EXPORTED")

	f2 := initFixture(t)
	require.NoError(t, f2.keeper.InitGenesis(f2.ctx, *exported))
	hasSel, err := f2.keeper.AuditSelected.Has(f2.ctx, "jSel")
	require.NoError(t, err)
	require.True(t, hasSel, "after import the selection marker holds: the retry will NOT replay the lottery")
	hasDebt, err := f2.keeper.PendingHeldRelease.Has(f2.ctx, "jDette")
	require.NoError(t, err)
	require.True(t, hasDebt, "after import the replay queue holds: the held fee really does stay claimable")
}

// ---------------------------------------------------------------------------------------------------------
// AUDITS_OPENED — the INDEPENDENT counter that makes the partition sum FALSIFIABLE.
// Comparing classes and a total drawn from the SAME enumeration is true by construction: a sum that
// cannot come out wrong verifies nothing (ADR-033).
// ---------------------------------------------------------------------------------------------------------

// (once per job, across a deferral and a retry) The first selection counts 1; the retry arrives with
// alreadySelected=true and does not count again. Red against code that counted at EVERY pass of the
// deferral loop, where the 20-block stride would inflate `opened` without end on a network with no
// jurors.
func TestAuditsOpenedCountsOnceAcrossDeferAndRetry(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // selection is certain
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake}))
	require.NoError(t, gvSetJob(f, f.ctx, "jOp", types.Job{JobId: "jOp", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jOp", uint64(100000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jOp")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))
	n1, err := f.keeper.AuditsOpened.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1), n1, "first selection (deferred for lack of a jury) = ONE opening counted")

	// Retry at the deferral stride: alreadySelected -> NO second count.
	retry := vitH + keeper.AuditDeferStrideForTest
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(retry)))
	n2, err := f.keeper.AuditsOpened.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1), n2, "retrying an ALREADY selected job does not count the opening again")
}

// (human dispute: counted once, and no double count when it overlaps with sampling)
func TestAuditsOpenedHumanDisputeOnceNoDoubleWithSampling(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	p.DisputeWindow = 10
	p.DisputeBond = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputeur_opened_001")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake}))
	require.NoError(t, gvSetJob(f, f.ctx, "jHd", types.Job{JobId: "jHd", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jHd"})
	require.NoError(t, err)
	n1, err := f.keeper.AuditsOpened.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1), n1, "an accepted human dispute = ONE opening")

	// Anti-replay: a second dispute is refused, so the counter does not move.
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jHd"})
	require.Error(t, err)
	// LATER sampling of the same, already-disputed job: the audit ADDS to it, it does not reopen it.
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jHd")))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))
	n2, err := f.keeper.AuditsOpened.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1), n2, "human dispute THEN sampling of the same job = still ONE opening (the overlap is deduplicated)")
}

// (the counter travels) It is monotonic: without an export, the partition sum would break forever
// after a migration, since the classes are recounted from the carried jobs while `opened` restarts at
// zero.
func TestGenesisRoundTripCarriesAuditsOpened(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.AuditsOpened.Set(f.ctx, 7))

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(7), exported.AuditsOpened)

	f2 := initFixture(t)
	require.NoError(t, f2.keeper.InitGenesis(f2.ctx, *exported))
	n, err := f2.keeper.AuditsOpened.Get(f2.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(7), n, "the opening counter SURVIVES the migration")
}
