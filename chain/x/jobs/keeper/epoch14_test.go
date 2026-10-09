package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ ADR-048 — CONSENSUS EPOCH 14 ═══════════════════════════════════════════════════════════════
//
// One bench per item, each with the twin that gives it its meaning: a guard that refused everything, a
// payment that paid twice or a filter that dropped everyone would pass a one-sided case. The cases drive
// the shipped functions (OpenJob, EndBlock, releaseHeld, the draws); none recomputes a decision beside
// the code.

// retainCutForTest writes the state `settleOptimistic` leaves for a job since epoch 14: the three shares
// of the cut retained per job, and the pending subsidy right. Fixtures that fabricated the epoch-13 state
// (the cut added to Pools counters, the Demand already credited) were measuring a chain that no longer
// exists.
func retainCutForTest(t *testing.T, f *fixture, jobId string, validators, team, treasury, demand uint64) {
	t.Helper()
	for _, e := range []struct {
		m   collections.Map[string, uint64]
		amt uint64
	}{{f.keeper.HeldValidatorShare, validators}, {f.keeper.HeldTeamShare, team}, {f.keeper.HeldTreasuryShare, treasury}, {f.keeper.HeldDemand, demand}} {
		if e.amt > 0 {
			require.NoError(t, e.m.Set(f.ctx, jobId, e.amt))
		}
	}
}

func e14Udndr(f *fixture, addr sdk.AccAddress) int64 {
	return f.bank.balOf(addr).AmountOf("udndr").Int64()
}

func e14Pools(f *fixture) types.Pools {
	if p, err := f.keeper.Pools.Get(f.ctx); err == nil {
		return p
	}
	return types.Pools{}
}

// ── ITEM 1 — A JOB OPENS BELOW THE JURY FLOOR WHEN NOTHING CAN BE LOST BY IT ─────────────────────

func e14ArmedParams(t *testing.T) types.Params {
	t.Helper()
	p := qzOptimisticParams(t)
	p.AuditUnwindBlocks = 500 // >= audit_resolve_timeout (120), as Validate requires
	p.HoldBps = 10000
	require.NoError(t, p.Validate(), "premise: the relaunch configuration validates")
	return p
}

func e14Open(t *testing.T, f *fixture, p types.Params, miners int, jobId string) error {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	for i := 0; i < miners; i++ {
		qzMiner(t, f, fmt.Sprintf("%s-m%d", jobId, i), qzFreeze-10, p.MinStake)
	}
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))
	_, err := keeper.NewMsgServerImpl(f.keeper).OpenJob(ctx, &types.MsgOpenJob{Creator: ssAddr20(t, f, "e14-client"), JobId: jobId, Fee: 30})
	return err
}

func TestEpoch14_OneMinerOpensAJobWhenUnwindAndFullHoldAreArmed(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, e14Open(t, f, e14ArmedParams(t), 1, "jE14One"),
		"armed unwind + full hold: below the jury floor an audited job can only be refunded, so refusing it protects nothing")
	_, err := f.keeper.Job.Get(f.ctx, "jE14One")
	require.NoError(t, err, "the job exists, escrow taken")
}

func TestEpoch14_BelowTheFloorStillRefusedWhenUnwindIsDormant(t *testing.T) {
	f := initFixture(t)
	p := e14ArmedParams(t)
	p.AuditUnwindBlocks = 0
	err := e14Open(t, f, p, 1, "jE14Dormant")
	require.Error(t, err, "without the unwind, a retention no jury can judge is held for ever: the refusal must stand")
	require.Contains(t, err.Error(), "5 needed")
}

func TestEpoch14_BelowTheFloorStillRefusedAtAPartialHold(t *testing.T) {
	f := initFixture(t)
	p := e14ArmedParams(t)
	p.HoldBps = 9999 // one basis point reaches the miner before any audit
	err := e14Open(t, f, p, 1, "jE14Partial")
	require.Error(t, err, "at a partial hold the immediate part is paid unverified and no jury could claw it back")
	require.Contains(t, err.Error(), "5 needed")
}

func TestEpoch14_NobodyToServeIsStillRefused(t *testing.T) {
	f := initFixture(t)
	err := e14Open(t, f, e14ArmedParams(t), 0, "jE14Empty")
	require.Error(t, err, "the floor drops to one, never to zero: someone must be there to serve")
	require.Contains(t, err.Error(), "1 needed")
}

// ── ITEM 2 — THE NO-SEED DEFERRAL ENDS LIKE THE OTHER TWO ────────────────────────────────────────

func e14NoSeedSetup(t *testing.T, f *fixture, unwind uint64) (sdk.AccAddress, sdk.Context) {
	t.Helper()
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.CommitteeSeedSource = 1 // the draw needs a decentralised seed, and none exists in this store
	p.AuditUnwindBlocks = unwind
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	clientAcc := sdk.AccAddress([]byte("client_e14_noseed__1"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mNS", types.Miner{MinerId: "mNS", Stake: 1000}))
	require.NoError(t, gvSetJob(f, f.ctx, "jNS", types.Job{
		JobId: "jNS", State: "open+paid+optimistic", MinerId: "mNS", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jNS", 80000))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jNS", 1))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jNS", 5000))
	retainCutForTest(t, f, "jNS", 7500, 3000, 4500, 7500)
	const h = int64(2000)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jNS")))
	return clientAcc, sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
}

func e14PendingAuditOf(t *testing.T, f *fixture, jobId string) []int64 {
	t.Helper()
	var hs []int64
	require.NoError(t, f.keeper.PendingAudit.Walk(f.ctx, nil, func(k collections.Pair[int64, string]) (bool, error) {
		if k.K2() == jobId {
			hs = append(hs, k.K1())
		}
		return false, nil
	}))
	return hs
}

func TestEpoch14_NoSeedDeferralUnwindsARetentionOlderThanTheBound(t *testing.T) {
	f := initFixture(t)
	clientAcc, ctx := e14NoSeedSetup(t, f, 500)
	require.NoError(t, f.keeper.EndBlock(ctx))

	job, _ := f.keeper.Job.Get(f.ctx, "jNS")
	require.Contains(t, job.State, "unwound", "the no-seed deferral now ends in the unwind, like the jury-floor and below-quorum ones")
	require.Equal(t, int64(100000), e14Udndr(f, clientAcc), "the client gets the WHOLE fee back: retention, cut and burn")
	require.Empty(t, e14PendingAuditOf(t, f, "jNS"), "settled, not postponed: no pending audit entry survives")
	m, _ := f.keeper.Miner.Get(f.ctx, "mNS")
	require.Equal(t, uint64(0), m.Demand, "and the miner earned no right on a job that was undone")
}

func TestEpoch14_NoSeedDeferralStillDefersWhenUnwindIsDormant(t *testing.T) {
	f := initFixture(t)
	clientAcc, ctx := e14NoSeedSetup(t, f, 0)
	require.NoError(t, f.keeper.EndBlock(ctx))

	job, _ := f.keeper.Job.Get(f.ctx, "jNS")
	require.NotContains(t, job.State, "resolved", "dormant: nothing is closed")
	require.Equal(t, int64(0), e14Udndr(f, clientAcc), "and nothing moves")
	require.Equal(t, []int64{2000 + keeper.AuditDeferStrideForTest}, e14PendingAuditOf(t, f, "jNS"),
		"deferred by the stride, exactly as before epoch 14")
}

// ── ITEMS 3 AND 4 — FINALITY PAYS THE CUT AND CREDITS THE RIGHT; A REFUND RETURNS THIS JOB'S CUT ──

type e14Final struct {
	op, team, client sdk.AccAddress
	job              types.Job
}

func e14FinalSetup(t *testing.T, f *fixture, teamAddress bool, withMiner bool) e14Final {
	t.Helper()
	r := e14Final{
		op:     sdk.AccAddress([]byte("operator_e14_final_1")),
		team:   sdk.AccAddress([]byte("team_address_e14____")),
		client: sdk.AccAddress([]byte("client_e14_final____")),
	}
	p := qzOptimisticParams(t)
	p.HoldBps = 10000
	if teamAddress {
		p.TeamAddress, _ = f.addressCodec.BytesToString(r.team)
	}
	require.NoError(t, p.Validate(), "premise: a mode-1 set carrying a team_address validates")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	for _, a := range []sdk.AccAddress{r.op, r.team, r.client} {
		f.bank.setBalance(a, sdk.NewCoins())
	}
	opS, _ := f.addressCodec.BytesToString(r.op)
	clS, _ := f.addressCodec.BytesToString(r.client)
	if withMiner {
		require.NoError(t, gvSetMiner(f, f.ctx, "mFin", types.Miner{MinerId: "mFin", Operator: opS, Stake: 1000}))
	}
	r.job = types.Job{JobId: "jFin", State: "open+paid+optimistic", MinerId: "mFin", Client: clS, Fee: 100000}
	require.NoError(t, gvSetJob(f, f.ctx, "jFin", r.job))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jFin", 80000))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jFin", 1))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jFin", 5000))
	retainCutForTest(t, f, "jFin", 7500, 3000, 4500, 7500)
	return r
}

func e14NoneRetained(t *testing.T, f *fixture, jobId string) {
	t.Helper()
	for _, m := range []collections.Map[string, uint64]{f.keeper.HeldValidatorShare, f.keeper.HeldTeamShare, f.keeper.HeldTreasuryShare, f.keeper.HeldDemand} {
		has, err := m.Has(f.ctx, jobId)
		require.NoError(t, err)
		require.False(t, has, "nothing of this job's cut or right is still retained")
	}
}

func TestEpoch14_FinalityPaysEachShareToItsOwnerAndCreditsTheRight(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, true)

	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)

	require.Equal(t, "7500udndr", f.bank.mod[authtypes.FeeCollectorName].String(),
		"the validators' share LEAVES for fee_collector: x/distribution pays it to stakers — it is no longer a counter")
	require.Equal(t, int64(3000), e14Udndr(f, r.team), "the team share reaches team_address")
	require.Equal(t, int64(80000), e14Udndr(f, r.op), "the miner's retention is released as before")
	pl := e14Pools(f)
	require.Equal(t, uint64(4500), pl.Treasury, "the treasury share is credited at finality, where it always lived")
	require.Equal(t, uint64(0), pl.Validators+pl.Team, "and neither the validators' nor the team's counter grows any more")
	m, _ := f.keeper.Miner.Get(f.ctx, "mFin")
	require.Equal(t, uint64(7500), m.Demand, "the subsidy right is credited NOW, when the miner is paid")
	e14NoneRetained(t, f, "jFin")
	has, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jFin")
	require.False(t, has, "nothing left owed, nothing queued")

	// ⛔ PAID ONCE. Finality has three callers and a retry sweep; a second pass must find nothing to pay.
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)
	require.Equal(t, "7500udndr", f.bank.mod[authtypes.FeeCollectorName].String(), "no second payment to fee_collector")
	require.Equal(t, int64(3000), e14Udndr(f, r.team), "no second team payment")
	m, _ = f.keeper.Miner.Get(f.ctx, "mFin")
	require.Equal(t, uint64(7500), m.Demand, "no second credit of the right")
}

func TestEpoch14_WithoutATeamAddressTheTeamShareStaysInTheModule(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, false, true)
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)
	require.Equal(t, int64(0), e14Udndr(f, r.team), "no recipient was named, so none is invented")
	require.Equal(t, uint64(3000), e14Pools(f).Team, "the share is counted in Pools.Team, the epoch-13 behaviour")
	e14NoneRetained(t, f, "jFin")
}

func TestEpoch14_AnAbsentPrimaryKeepsItsRightPendingUntilItReturns(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, false) // no miner record at finality
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)

	d, err := f.keeper.HeldDemand.Get(f.ctx, "jFin")
	require.NoError(t, err, "an eviction is not a forfeiture: the right waits")
	require.Equal(t, uint64(7500), d)
	queued, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jFin")
	require.True(t, queued, "and the job is queued, or the right would wait for a retry that never comes")
	require.Equal(t, "7500udndr", f.bank.mod[authtypes.FeeCollectorName].String(),
		"the cut does not depend on the miner: it is paid at finality regardless")

	opS, _ := f.addressCodec.BytesToString(r.op)
	require.NoError(t, gvSetMiner(f, f.ctx, "mFin", types.Miner{MinerId: "mFin", Operator: opS, Stake: 1000}))
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)
	m, _ := f.keeper.Miner.Get(f.ctx, "mFin")
	require.Equal(t, uint64(7500), m.Demand, "the miner is back, the right is credited")
	e14NoneRetained(t, f, "jFin")
	queued, _ = f.keeper.PendingHeldRelease.Has(f.ctx, "jFin")
	require.False(t, queued, "and the queue is cleared once nothing is owed")
}

func TestEpoch14_AtZeroHoldFinalityStillPaysTheCut(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, true)
	require.NoError(t, f.keeper.HeldFee.Remove(f.ctx, "jFin")) // hold_bps = 0: the miner was paid at settlement
	require.NoError(t, f.keeper.HeldSince.Remove(f.ctx, "jFin"))
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)
	require.Equal(t, "7500udndr", f.bank.mod[authtypes.FeeCollectorName].String(),
		"the early return for 'nothing held' sits AFTER the cut payment: with no retention the cut is still owed")
	e14NoneRetained(t, f, "jFin")
}

func TestEpoch14_ARefundBeforeFinalityReturnsThisJobsCutAndNothingElse(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, true)
	// Another job's retained cut, and a treasury counter that belongs to nobody in this case.
	retainCutForTest(t, f, "jOther", 750, 300, 450, 750)
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 999}))

	m, _ := f.keeper.Miner.Get(f.ctx, "mFin")
	// The miner already holds a right earned on ANOTHER, final job. A pending right on this one must be
	// dropped without touching it: reversing "as if credited" would take that other job's right away.
	m.Demand = 9999
	p, _ := f.keeper.Params.Get(f.ctx)
	refunded := f.keeper.RefundRetainedToClientForTest(f.ctx, &r.job, p, &m)

	require.Equal(t, uint64(100000), refunded, "retention + this job's three shares + burn = the whole fee")
	require.Equal(t, int64(100000), e14Udndr(f, r.client))
	require.Equal(t, uint64(999), e14Pools(f).Treasury, "the refund no longer draws on Pools: other money stays where it is")
	v, err := f.keeper.HeldValidatorShare.Get(f.ctx, "jOther")
	require.NoError(t, err, "another job's retained cut is untouched")
	require.Equal(t, uint64(750), v)
	require.Equal(t, uint64(9999), m.Demand,
		"this job's right was still PENDING, so nothing is reversed: the Demand the miner holds belongs to other jobs")
	e14NoneRetained(t, f, "jFin")
}

func TestEpoch14_AtZeroHoldAnAbsentPrimaryIsQueuedForItsRight(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, false) // no miner record at finality
	require.NoError(t, f.keeper.HeldFee.Remove(f.ctx, "jFin"))
	require.NoError(t, f.keeper.HeldSince.Remove(f.ctx, "jFin"))
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job)

	_, err := f.keeper.HeldDemand.Get(f.ctx, "jFin")
	require.NoError(t, err, "the right waits for its miner")
	queued, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jFin")
	require.True(t, queued,
		"with nothing held the early return used to CLEAR the queue: a right left pending there would wait for a retry that never comes")
}

func TestEpoch14_ASlashAfterFinalityReversesTheCreditedRight(t *testing.T) {
	f := initFixture(t)
	r := e14FinalSetup(t, f, true, true)
	f.keeper.ReleaseHeldForTest(f.ctx, &r.job) // final: shares paid out, right credited

	m, _ := f.keeper.Miner.Get(f.ctx, "mFin")
	require.Equal(t, uint64(7500), m.Demand, "premise: the right was credited at finality")
	p, _ := f.keeper.Params.Get(f.ctx)
	refunded := f.keeper.RefundRetainedToClientForTest(f.ctx, &r.job, p, &m)

	require.Equal(t, uint64(0), refunded, "after finality nothing is retained: what was paid out is owed by the bond, as at a partial hold")
	require.Equal(t, uint64(0), m.Demand, "the credited right is reversed, so a slashed job unlocks no subsidy")
}

func TestEpoch14_SelfDealingEarnsNoRight(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	idM := deriveIDFor(t, f, op)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: idM, Stake: 1000})
	require.NoError(t, err)
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: op, JobId: "jSelf", Fee: 80}) // client == operator
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "jSelf__" + idM, ResultCommit: "1,2,3"})
	require.NoError(t, err)
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: op, JobId: "jSelf"})
	require.NoError(t, err)

	has, err := f.keeper.HeldDemand.Has(f.ctx, "jSelf")
	require.NoError(t, err)
	require.False(t, has, "client == operator: no right is recorded, so finality has nothing to credit")
	v, err := f.keeper.HeldValidatorShare.Get(f.ctx, "jSelf")
	require.NoError(t, err, "the cut is still taken and retained: self-dealing pays the protocol like anyone")
	require.Equal(t, uint64(6), v)
}

// ── ITEM 5 — ONLY A PRESENT MINER IS DRAWN ───────────────────────────────────────────────────────

func TestEpoch14_PresenceRuleEdges(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 100

	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "new", 0))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "old", 0))
	require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, "old", 4))

	off := p
	off.AvailEpochBlocks = 0
	require.True(t, f.keeper.MinerPresentAtForTest(f.ctx, "nodate", 5000, off),
		"windows disarmed: presence is not measured, so it filters nobody")

	require.True(t, f.keeper.MinerPresentAtForTest(f.ctx, "new", 150, p), "registered in window 0, at window 1: newcomer grace")
	require.False(t, f.keeper.MinerPresentAtForTest(f.ctx, "new", 250, p), "window 2 with no proof: absent")

	require.True(t, f.keeper.MinerPresentAtForTest(f.ctx, "old", 450, p), "proved window 4, inside it: present")
	require.True(t, f.keeper.MinerPresentAtForTest(f.ctx, "old", 550, p),
		"proved window 4, at the start of window 5: still present, it has not had the chance to answer the new challenge")
	require.False(t, f.keeper.MinerPresentAtForTest(f.ctx, "old", 650, p), "a whole window missed: absent")

	require.False(t, f.keeper.MinerPresentAtForTest(f.ctx, "nodate", 50, p),
		"no proof and no registration date: a state no path produces is not granted presence")
}

func TestEpoch14_ProvingAvailabilityRecordsTheWindowForward(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // mode 0: the plain echo is accepted, no VRF key needed for this case
	p.AvailEpochBlocks = 100
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	opS := ssAddr20(t, f, "e14-prover")
	require.NoError(t, gvSetMiner(f, f.ctx, "mProve", types.Miner{MinerId: "mProve", Operator: opS, Stake: 1000}))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "chal"))
	srv := keeper.NewMsgServerImpl(f.keeper)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(250)
	_, err := srv.ProveAvailability(ctx, &types.MsgProveAvailability{Creator: opS, MinerId: "mProve", Challenge: "chal"})
	require.NoError(t, err)
	e, err := f.keeper.MinerLastPresentEpoch.Get(f.ctx, "mProve")
	require.NoError(t, err, "the proof leaves a date the draws can read after the window is purged")
	require.Equal(t, uint64(2), e)
}

// e14DrawSetup — two miners in the frozen pool of one job, both vital, one present and one absent at the
// freeze height. Window = 100 blocks, freeze = 1000 (window 10).
func e14DrawSetup(t *testing.T, f *fixture, eb uint64) {
	t.Helper()
	p := types.DefaultParams()
	p.AvailEpochBlocks = eb
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	for _, id := range []string{"mHere", "mGone"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1000}))
		require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 500))
	}
	require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, "mHere", 9))
	require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, "mGone", 7))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jDraw", types.Beacon{JobId: "jDraw", Seed: "seed-e14"}))
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jDraw", 1000))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jDraw", types.Job{JobId: "jDraw", State: "open"}))
}

func TestEpoch14_AnAbsentMinerIsNotDrawnAsPrimary(t *testing.T) {
	f := initFixture(t)
	e14DrawSetup(t, f, 100)
	got, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jDraw", 3)
	require.NoError(t, err)
	require.Equal(t, []string{"mHere"}, got, "the miner that missed window 8 and 9 is not drawn at window 10")

	g := initFixture(t)
	e14DrawSetup(t, g, 0)
	got, err = g.keeper.AssignedCommitteeOrderedForTest(g.ctx, "jDraw", 3)
	require.NoError(t, err)
	require.Len(t, got, 2, "control: windows disarmed, both are drawn as before epoch 14")
}

func TestEpoch14_AnAbsentMinerIsNotSeatedAsAJuror(t *testing.T) {
	f := initFixture(t)
	e14DrawSetup(t, f, 100)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(1000)
	got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jDraw", "nobody", "")
	require.NoError(t, err)
	require.Equal(t, []string{"mHere"}, got, "a juror whose machine is off would hold a seat and never vote")
}

func TestEpoch14_AdmissionCountsOnlyPresentMiners(t *testing.T) {
	f := initFixture(t)
	p := e14ArmedParams(t)
	p.AvailEpochBlocks = 100
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	qzMiner(t, f, "mAbsent", 100, p.MinStake) // registered in window 1, never proved; the job opens in window 4
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err := srv.OpenJob(ctx, &types.MsgOpenJob{Creator: ssAddr20(t, f, "e14-client"), JobId: "jAbs", Fee: 30})
	require.Error(t, err, "the only registered miner is absent: nobody could serve this job")

	require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, "mAbsent", 4))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: ssAddr20(t, f, "e14-client"), JobId: "jPres", Fee: 30})
	require.NoError(t, err, "the same miner, having proved this window, is counted")
}

// ── ITEM 6 — FOUR CONSTANTS BECOME BOUNDED PARAMETERS ────────────────────────────────────────────

func TestEpoch14_TheFallbackFloorIsNamedOnceAndAgrees(t *testing.T) {
	require.Equal(t, types.AuditFallbackFloor, keeper.AuditSlashFloorForTest(),
		"types bounds the draw size against this number; if the keeper's derivation moves, both must move")
}

func e14PendingExpiryOf(t *testing.T, f *fixture, jobId string) []int64 {
	t.Helper()
	var hs []int64
	require.NoError(t, f.keeper.PendingJobExpiry.Walk(f.ctx, nil, func(k collections.Pair[int64, string]) (bool, error) {
		if k.K2() == jobId {
			hs = append(hs, k.K1())
		}
		return false, nil
	}))
	return hs
}

func TestEpoch14_GovernedJobExpiryBooksTheAppointment(t *testing.T) {
	f := initFixture(t)
	p := qzOptimisticParams(t)
	p.JobExpiryBlocks = 5000
	require.NoError(t, p.Validate())
	require.NoError(t, e14Open(t, f, p, 5, "jExp"))
	require.Equal(t, []int64{int64(qzFreeze) + 5000}, e14PendingExpiryOf(t, f, "jExp"))

	g := initFixture(t)
	require.NoError(t, e14Open(t, g, qzOptimisticParams(t), 5, "jExpDefault"))
	require.Equal(t, []int64{int64(qzFreeze) + 100000}, e14PendingExpiryOf(t, g, "jExpDefault"),
		"control: 0 selects the compiled 100 000")
}

func TestEpoch14_GovernedDrawSizeIsTheJurySize(t *testing.T) {
	for _, c := range []struct {
		size uint64
		want int
	}{{6, 6}, {0, keeper.AuditCommitteeDrawSizeForTest}} {
		f := initFixture(t)
		p := types.DefaultParams()
		p.AuditCommitteeDrawSize = c.size
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		for i := 0; i < 20; i++ {
			id := fmt.Sprintf("mJ%02d", i)
			require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1000}))
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 500))
		}
		require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jSize", 1000))
		ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(1000)
		got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jSize", "nobody", "")
		require.NoError(t, err)
		require.Len(t, got, c.want, "audit_committee_draw_size=%d", c.size)
	}
}

func TestEpoch14_GovernedWorkCommitteeSizeIsWhatGetsAnchored(t *testing.T) {
	for _, c := range []struct {
		size uint64
		want int
	}{{1, 1}, {0, keeper.CommitteeSize}} {
		f := initFixture(t)
		e14DrawSetup(t, f, 0)
		for _, id := range []string{"mX1", "mX2"} {
			require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1000}))
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 500))
		}
		p, _ := f.keeper.Params.Get(f.ctx)
		p.WorkCommitteeSize = c.size
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		f.keeper.AnchorWorkCommitteeForTest(f.ctx, "jDraw")
		raw, err := f.keeper.WorkCommitteeRawForTest(f.ctx, "jDraw")
		require.NoError(t, err)
		require.Len(t, splitNonEmpty(raw), c.want, "work_committee_size=%d", c.size)
	}
}

func splitNonEmpty(s string) []string {
	var out []string
	start := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == ',' {
			if i > start {
				out = append(out, s[start:i])
			}
			start = i + 1
		}
	}
	return out
}

func TestEpoch14_AnAbsentMinerIsNotSeatedOnARedoCommittee(t *testing.T) {
	for _, c := range []struct {
		eb       uint64
		seatGone bool
	}{{10, false}, {0, true}} {
		f := initFixture(t)
		gvJob(t, f, "j-redo14", true)
		p := types.DefaultParams()
		p.AvailEpochBlocks = c.eb // gvFreeze=100 is window 10, gvNow=110 is window 11
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		for _, id := range []string{"a1", "a2", "a3", "a4", "a5"} {
			gvMiner(t, f, id, gvStakeHi, 50)
			require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, id, 10))
		}
		gvMiner(t, f, "gone", gvStakeLo, 50) // dust stake: never in the original committee of three
		require.NoError(t, f.keeper.MinerLastPresentEpoch.Set(f.ctx, "gone", 3))
		require.NoError(t, f.keeper.BlockHash.Set(f.ctx, gvNow, []byte("hash-de-bloc-de-banc")))

		require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(gvCtx(f), "j-redo14", "", ""))
		anchor, err := f.keeper.AuditCommittee.Get(gvCtx(f), keeper.RedoAnchorKeyForTest("j-redo14"))
		require.NoError(t, err, "a redo committee had to be anchored, or this case measures nothing")
		require.Equal(t, c.seatGone, containsID(anchor, "gone"),
			"avail_epoch_blocks=%d: a miner absent for seven windows is seated only when presence is not measured", c.eb)
	}
}

func containsID(list, id string) bool {
	for _, x := range splitNonEmpty(list) {
		if x == id {
			return true
		}
	}
	return false
}

// The governed cap is what the WORK draw applies. Deterministic: same seeds, same jobs, so the counts are
// exact and the two bounds below are chosen far from them on both sides.
func TestEpoch14_GovernedStakeCapIsWhatTheWorkDrawApplies(t *testing.T) {
	count := func(multiple uint64) int {
		f := initFixture(t)
		p := types.DefaultParams() // min_stake 1000
		p.AssignmentStakeCapMultiple = multiple
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		require.NoError(t, f.keeper.Miner.Set(f.ctx, "big", types.Miner{MinerId: "big", Stake: 1_000_000_000}))
		require.NoError(t, f.keeper.Miner.Set(f.ctx, "small", types.Miner{MinerId: "small", Stake: 1000}))
		for _, id := range []string{"big", "small"} {
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 500))
		}
		n := 0
		for i := 0; i < 40; i++ {
			j := fmt.Sprintf("jCap%02d", i)
			require.NoError(t, f.keeper.Beacon.Set(f.ctx, j, types.Beacon{JobId: j, Seed: "cap-seed"}))
			require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, j, 1000))
			got, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, j, 1)
			require.NoError(t, err)
			if len(got) == 1 && got[0] == "big" {
				n++
			}
		}
		return n
	}
	flat, wide := count(1), count(100)
	require.LessOrEqual(t, flat, 30, "multiple 1: both weigh min_stake, the big stake buys nothing (measured %d/40)", flat)
	require.GreaterOrEqual(t, wide, 36, "multiple 100: the big stake weighs a hundredfold (measured %d/40)", wide)
}
