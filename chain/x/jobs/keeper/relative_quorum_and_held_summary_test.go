package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Audit quorum: vindication and deferral are symmetric.
//
// ①a: vindication requires the RELATIVE bar (⌈2/3⌉ of the anchored seats), the same bar as the slash.
// ①b: sub-quorum -> DEFERRAL (nothing closes, nothing moves, a new deadline is scheduled).
// Escrow conservation: at least one test must turn RED if a sub-quorum RELEASES the escrow, which is
// what would make evasion free.
// Observability: held_total / held_oldest_age must be measurable (HeldSince + the HeldSummary query).
// The permissionless AdjudicateDispute path is covered too: it used to vindicate at the ABSOLUTE floor.

// unSeats anchors a committee of `n` seats (mJA..), all registered with stake 1, and posts `votesValid`
// "1" verdicts. One operator per judge (distinct addresses) unless stated otherwise.
func unSeats(f *fixture, t *testing.T, jobId string, n, votesValid int) {
	t.Helper()
	ids := make([]string, 0, n)
	for i := 1; i <= n; i++ {
		id := "mJ" + string(rune('A'+i-1))
		op, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("op_undecies_" + id + "_____")[:20]))
		require.NoError(t, err)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1, Operator: op}))
		ids = append(ids, id)
	}
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId, strings.Join(ids, ",")))
	for i := 0; i < votesValid && i < n; i++ {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__verdict__"+ids[i], types.Commit{ResultCommit: "1"}))
	}
}

// ---------------------------------------------------------------------------------------------------------
// ①a — VINDICATION REQUIRES ⌈2/3⌉ OF THE ANCHORED SEATS.
// ---------------------------------------------------------------------------------------------------------

// 15 anchored seats, 4 "valid" votes: the absolute floor is reached, but that is only ~27% of the
// committee. Under an absolute floor those 4 votes would vindicate (releaseHeld, +vindicated), which
// means 27% of the stake buys the vindication of any cheat. Expected: NO vindication, DEFERRAL — the
// held fee does not move. This test turns red if 4 out of 15 seats ever suffice again.
func TestVindicationRequiresRelativeQuorumOfSeats(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4 // production setting: the relative bar governs (max(4, ⌈2/3x15⌉=10) = 10)
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_undecies_1a"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	unSeats(f, t, "j1a", 15, 4) // 4 "1" votes over 15 seats: above the absolute floor, below the bar
	require.NoError(t, gvSetJob(f, f.ctx, "j1a", types.Job{
		JobId: "j1a", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "j1a", uint64(80000)))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "j1a")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "j1a")
	require.NotContains(t, job.State, "vindicated",
		"4 votes over 15 seats (~27%) do not vindicate: the bar is ⌈2/3⌉ of the seats, as for the slash")
	require.NotContains(t, job.State, "resolved", "below the bar -> DEFERRAL")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "j1a")
	require.NoError(t, hErr, "the held fee was NOT released")
	require.Equal(t, uint64(80000), held)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "operator not paid")
}

// Same 15 seats, 10 "valid" votes = ⌈2/3⌉ reached: vindication goes through. A raised bar must not
// degenerate into "never vindicate" — a guard that cannot stay silent carries no information. The
// held fee is released to the operator, the job carries `vindicated+quorum`, and the plurality is
// recorded (10 voters, 10 distinct operators, at most 1 vote per operator).
func TestVindicationAtRelativeQuorumStillVindicates(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_undecies_1b"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	unSeats(f, t, "j1b", 15, 10) // ⌈2/3x15⌉ = 10: exactly at the bar
	require.NoError(t, gvSetJob(f, f.ctx, "j1b", types.Job{
		JobId: "j1b", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "j1b", uint64(80000)))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "j1b", uint64(2)))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "j1b")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "j1b")
	require.Contains(t, job.State, "vindicated", "10/15 = ⌈2/3⌉: the honest miner is vindicated (the bar can stay silent)")
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "held fee released")
	require.Equal(t, uint64(10), job.AuditVoters, "plurality recorded: 10 votes counted")
	require.Equal(t, uint64(10), job.AuditDistinctVoters, "10 distinct operators")
	require.Equal(t, uint64(1), job.AuditMaxOperatorVotes, "at most 1 vote per operator")
	_, sErr := f.keeper.HeldSince.Get(f.ctx, "j1b")
	require.Error(t, sErr, "the DATE leaves with the debt it dates: an age must never outlive what it measures")
}

// ---------------------------------------------------------------------------------------------------------
// ESCROW CONSERVATION — the test that turns red the moment evasion becomes free, right at the bar.
// ---------------------------------------------------------------------------------------------------------

// 9 "valid" votes over 15 seats = 60%, ONE vote short of the bar (10). This is the exact adversarial
// case: if a future regression rounds the quorum wrong, counts seats instead of votes, or turns a
// deferral into a finalization, this is where the escrow would escape. FULL CONSERVATION is required:
// the held fee stays with the module, the deferred burn stays held, operator AND client at zero,
// stake intact, job still open.
func TestSubQuorumOneVoteShortNeverReleasesEscrow(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_undecies_c2"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	clientAcc := sdk.AccAddress([]byte("client_undecies_c2__"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	unSeats(f, t, "jC2", 15, 9) // 9 < 10: one vote short of the bar
	require.NoError(t, gvSetJob(f, f.ctx, "jC2", types.Job{
		JobId: "jC2", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jC2", uint64(80000)))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jC2", uint64(5000)))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jC2")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jC2")
	require.NoError(t, hErr, "one vote short of the bar, the escrow is NOT released")
	require.Equal(t, uint64(80000), held)
	burn, bErr := f.keeper.HeldBurn.Get(f.ctx, "jC2")
	require.NoError(t, bErr, "deferred burn still held (neither burned nor returned)")
	require.Equal(t, uint64(5000), burn)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "operator: nothing")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client: nothing")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "stake intact")
	job, _ := f.keeper.Job.Get(f.ctx, "jC2")
	require.NotContains(t, job.State, "resolved", "nothing closes at 9/15")
	next, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(int64(6)+keeper.AuditDeferStrideForTest, "jC2"))
	require.True(t, next, "and the audit is rescheduled (the loop stays alive)")
}

// ---------------------------------------------------------------------------------------------------------
// THE PERMISSIONLESS TWIN — AdjudicateDispute vindicated de facto at the absolute floor.
// ---------------------------------------------------------------------------------------------------------

// 15 anchored seats, 5 "valid" verdicts: enough to enter the verdict branch (absolute floor 4), NOT
// enough for the relative bar (10). Without the bar, "no cheat" leads to releaseHeld and
// +resolved+adjudicated — a vindication pronounced by ~27% of the stake through a permissionless tx.
// Expected: adjudication REFUSES (error), no funds move, the audit stays open.
func TestAdjudicateRefusesVerdictsBelowRelativeBar(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_undecies_tw")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	opAcc := sdk.AccAddress([]byte("operator_undecies_tw"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: adr033Big}))
	unSeats(f, t, "jTw", 15, 5) // 5 "1" votes: above the absolute floor only
	require.NoError(t, gvSetJob(f, f.ctx, "jTw", types.Job{
		JobId: "jTw", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jTw", uint64(80000)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jTw"})
	require.Error(t, err, "neither slash nor vindication at the relative bar -> adjudication does NOT go through")

	job, _ := f.keeper.Job.Get(f.ctx, "jTw")
	require.NotContains(t, job.State, "resolved", "the refusal closed nothing")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jTw")
	require.NoError(t, hErr, "the held fee did not move")
	require.Equal(t, uint64(80000), held)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "the permissionless entry point did not pay the operator")
}

// Same seats, 10 "valid" verdicts = the bar is reached: adjudication goes through, vindicates, marks
// `+adjudicated` and records the plurality derived from the verdict set.
func TestAdjudicateVindicatesAtRelativeBar(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_undecies_ok")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	opAcc := sdk.AccAddress([]byte("operator_undeciesok_"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: adr033Big}))
	unSeats(f, t, "jOk2", 15, 10)
	require.NoError(t, gvSetJob(f, f.ctx, "jOk2", types.Job{
		JobId: "jOk2", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jOk2", uint64(80000)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jOk2"})
	require.NoError(t, err, "10/15: the bar is reached, adjudication succeeds")

	job, _ := f.keeper.Job.Get(f.ctx, "jOk2")
	require.Contains(t, job.State, "adjudicated")
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "vindicated -> held fee released")
	require.Equal(t, uint64(10), job.AuditVoters)
	require.Equal(t, uint64(1), job.AuditMaxOperatorVotes, "one operator, one vote here")
}

// ---------------------------------------------------------------------------------------------------------
// held_total / held_oldest_age — "held is not lost" becomes falsifiable.
// ---------------------------------------------------------------------------------------------------------

func TestHeldSummaryMeasuresTotalAndOldestAge(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	// Empty: everything at zero (no age is invented).
	resp, err := qs.HeldSummary(f.ctx, &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Zero(t, resp.HeldTotal)
	require.Zero(t, resp.HeldCount)
	require.Zero(t, resp.OldestAgeBlocks)

	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jA", 100))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jA", 50))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jB", 200))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jB", 80))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jC", 300)) // legacy entry: held WITHOUT a date

	ctx100 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100)
	resp, err = qs.HeldSummary(ctx100, &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(600), resp.HeldTotal, "sum of the held fees: the number that would show deferral freezing the escrow")
	require.Equal(t, uint64(3), resp.HeldCount)
	require.Equal(t, uint64(50), resp.OldestAgeBlocks, "age of the OLDEST DATED held fee: the number that would show the network not healing")
	require.Equal(t, "jA", resp.OldestJobId)
}

// THE AGE SURVIVES MIGRATION ONLY WHEN THE RESTART HEIGHT EXCEEDS IT, and a lower bound cannot see the
// other half: `OldestAgeBlocks >= 29` holds just as well on a fixture returning 30 as on one returning
// 89, so fifty-nine blocks of accumulated age can vanish under a green assertion. Each row below is
// therefore an EQUALITY.
//
// MEASURED at three restart heights, on one held fee exported with an age of 60 and read 29 blocks
// after import: h=1 gives 30, h=30 gives 59, h=200 gives 89. Below the accumulated age the clamp fires,
// `since` becomes 0, and the debt is presented as being exactly as old as the new chain. A chain
// restarted from a fresh genesis therefore understates EVERY retention age for its first blocks —
// including `held_oldest_age`, which `audit_sampling.go` designates as the figure that makes "held is
// not lost" falsifiable.
//
// ⛔ PINNED, NOT FIXED, and the reason is the state shape: a uint64 anchor cannot express "60 blocks
// old" at height 1, because it would have to be negative. Carrying the age across a height reset means
// STORING the age — a collection change, a genesis field and a migration. When that lands, the first
// two rows below must flip to 89.
func TestGenesisHeldSinceTravelsAsAge(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jOld", 500))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jOld", 40))

	exported, err := f.keeper.ExportGenesis(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(100))
	require.NoError(t, err)
	require.Len(t, exported.HeldSinceElapsed, 1)
	require.Equal(t, uint64(60), exported.HeldSinceElapsed[0].Value, "exported as an AGE (100-40), never as an absolute height")

	// Three restart heights, one variable. The age carried in the export is 60; each row is read 29
	// blocks after its own import, so the faithful answer is 89 every time.
	for _, cas := range []struct {
		importH int64
		attendu uint64
	}{
		{1, 30},   // below the age: the clamp fires and the debt is presented as new-chain-old
		{30, 59},  // still below: half the age is gone
		{200, 89}, // above the age: the re-anchoring is exact, and the promise holds
	} {
		f2 := initFixture(t)
		require.NoError(t, f2.keeper.InitGenesis(sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(cas.importH), *exported))
		qs2 := keeper.NewQueryServerImpl(f2.keeper)
		resp, qErr := qs2.HeldSummary(
			sdk.UnwrapSDKContext(f2.ctx).WithBlockHeight(cas.importH+29), &types.QueryHeldSummaryRequest{})
		require.NoError(t, qErr)
		require.Equal(t, cas.attendu, resp.OldestAgeBlocks,
			"restart at height %d, 29 blocks later: the faithful age is 89 (60 carried + 29 elapsed)", cas.importH)
	}
}

// The date is born WITH the held fee: an optimistic settle at full hold writes HeldFee AND HeldSince
// at the same height. Without this test the pairing could exist only on the read path (a green query
// over dates that are never written -> age always zero -> an alarm mute by construction).
func TestSettleOptimisticDatesTheRetention(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.HoldBps = 10000 // full hold: ALL of minerNet is held -> the date MUST be written
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	idM1 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idM1, Operator: creator, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "jobHs", Fee: 30})
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jobHs__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err)

	ctx7 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(7)
	_, err = srv.SettleSemantic(ctx7, &types.MsgSettleSemantic{Creator: creator, JobId: "jobHs"})
	require.NoError(t, err)

	_, hErr := f.keeper.HeldFee.Get(f.ctx, "jobHs")
	require.NoError(t, hErr, "full hold: the held fee is written at settle time")
	since, sErr := f.keeper.HeldSince.Get(f.ctx, "jobHs")
	require.NoError(t, sErr, "and its DATE is born with it")
	require.Equal(t, uint64(7), since, "dated at the settle height")
}

// ---------------------------------------------------------------------------------------------------------
// A DISMISSED GRIEFER DOES NOT GET ITS BOND BACK.
// ---------------------------------------------------------------------------------------------------------

// Returning the bond to a dismissed disputer would make contesting an honest job FREE the day
// dispute_bond is armed, and the whole anti-grief property would disappear. Human dispute with a real
// bond plus an anchored audit committee that VINDICATES at the ⌈2/3⌉ bar: the disputer was WRONG, so
// the bond goes to the Treasury. This setup also shows that a job CAN be both audit-sampled and
// human-disputed: runOptimisticAudit does not overwrite a human dispute, it adds to it.
func TestVindicatedDisputeSendsBondToTreasury(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	dispAcc := sdk.AccAddress([]byte("disputeur_t2_deboute"))
	disp, _ := f.addressCodec.BytesToString(dispAcc)
	f.bank.setBalance(dispAcc, sdk.NewCoins())
	opAcc := sdk.AccAddress([]byte("operator_t2_vindique"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	unSeats(f, t, "jT2", 15, 10) // vindication exactly at the bar
	require.NoError(t, gvSetJob(f, f.ctx, "jT2", types.Job{
		JobId: "jT2", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 50000, DisputeHeight: 1, // REAL escrowed bond (min_stake, ADR-033)
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jT2", uint64(80000)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jT2")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jT2")
	require.Contains(t, job.State, "vindicated", "the committee decided: the primary is honest")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(50000), pools.Treasury,
		"the disputer was WRONG -> its bond goes to the Treasury (anti-grief)")
	require.Equal(t, uint64(0), job.DisputeBond, "bond consumed, not replayable")
	require.Equal(t, int64(0), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(),
		"a dismissed griefer does NOT get its stake back: contesting an honest job is never free")
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "and the honest miner is paid")
}

// The other side: a disputer that was RIGHT is still refunded. Slash at the bar means the bond is
// RETURNED. Without this face, the anti-grief rule could degenerate into "the bond always goes to the
// Treasury", which punishes the whistleblower and inverts the intent.
func TestSlashedDisputeStillRefundsBond(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	dispAcc := sdk.AccAddress([]byte("disputeur_t2_raison_"))
	disp, _ := f.addressCodec.BytesToString(dispAcc)
	f.bank.setBalance(dispAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mCheat", types.Miner{MinerId: "mCheat", Stake: aeStake}))
	// 15 anchored seats, 10 "0" verdicts (invalid) = slash at the relative bar.
	ids := make([]string, 0, 15)
	for i := 1; i <= 15; i++ {
		id := "mJx" + string(rune('A'+i-1))
		aeReg(f, t, id, 1)
		ids = append(ids, id)
	}
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jT2b", strings.Join(ids, ",")))
	for i := 0; i < 10; i++ {
		aeVerdict(f, t, "jT2b", ids[i], "0")
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jT2b", types.Job{
		JobId: "jT2b", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
		Disputer: disp, DisputeBond: 50000, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jT2b")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jT2b")
	require.Contains(t, job.State, "clawed+quorum", "cheat confirmed at quorum: the disputer was right")
	require.Equal(t, int64(50000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(),
		"a whistleblower that was RIGHT gets its bond back (confiscation only hits the dismissed disputer)")
	require.Equal(t, uint64(0), job.DisputeBond)
}

// ---------------------------------------------------------------------------------------------------------
// THE SECOND POCKET — an escrowed bond is VISIBLE in held-summary.
// ---------------------------------------------------------------------------------------------------------

// Without bond_escrowed_*, a deferred bond is an invisible orphan fund and "the bond stays claimable"
// is unverifiable. An OPEN dispute carrying a bond counts; a RESOLVED dispute (bond consumed or
// returned) does not.
func TestHeldSummaryCountsEscrowedBonds(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	disp, _ := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputeur_t1_visible")))

	require.NoError(t, gvSetJob(f, f.ctx, "jOpen", types.Job{ // OPEN dispute: counts
		JobId: "jOpen", State: "open+paid+optimistic+disputed", MinerId: "m1", Disputer: disp, DisputeBond: 50000,
	}))
	require.NoError(t, gvSetJob(f, f.ctx, "jDone", types.Job{ // resolved: bond already disposed of, does not count
		JobId: "jDone", State: "open+paid+optimistic+disputed+resolved+vindicated+quorum", MinerId: "m2", Disputer: disp, DisputeBond: 0,
	}))
	require.NoError(t, gvSetJob(f, f.ctx, "jNoBond", types.Job{ // open WITHOUT a bond (sampled): does not count
		JobId: "jNoBond", State: "open+paid+optimistic+disputed", MinerId: "m3",
	}))

	resp, err := qs.HeldSummary(f.ctx, &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(50000), resp.BondEscrowedTotal,
		"the bond of an OPEN dispute is VISIBLE: \"the bond stays claimable\" becomes falsifiable")
	require.Equal(t, uint64(1), resp.BondEscrowedCount, "a single open dispute carries a bond")
}
