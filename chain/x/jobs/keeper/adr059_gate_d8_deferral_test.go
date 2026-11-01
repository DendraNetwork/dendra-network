package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Epoch 15 keeps the branches of resolveDisputedAudit. Below the bar it defers, with the appeal
// deadline first only when allowAppeal and appeal_window say so, and it mutates the job on a real
// resolution only. Where a case has a twin, the twin shows that the same fixture takes the other branch.

// Branch (i), the silent redo jury, releases the retention to the primary. A job the lottery selected
// was summoned even without an audit jury, so a one-seat redo jury that never answers must not release
// it: it defers like any audit without a quorum. The control, never selected, is released as before.
func TestADR059Decision8_ASilentRedoJuryDoesNotReleaseAJobTheLotterySelected(t *testing.T) {
	for _, selected := range []bool{true, false} {
		f := initFixture(t)
		p := d8Params(t, 0)
		p.AuditResolveTimeout = 120
		p.DisputeBond = 1000
		require.NoError(t, p.Validate(), "premise: the configuration validates")
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		opAcc := sdk.AccAddress([]byte("operator_d8_redo____"))
		op, _ := f.addressCodec.BytesToString(opAcc)
		f.bank.setBalance(opAcc, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1000))))
		require.NoError(t, gvSetMiner(f, f.ctx, "mSel", types.Miner{MinerId: "mSel", Operator: op, Stake: 1000}))
		require.NoError(t, gvSetJob(f, f.ctx, "jSel", types.Job{JobId: "jSel", State: "open+paid+optimistic", MinerId: "mSel", Fee: 100000}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jSel", 80000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jSel", 900))
		if selected {
			require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jSel")) // drawn, then deferred for a small jury
		}
		const h = int64(1000)
		_, err := keeper.NewMsgServerImpl(f.keeper).DisputeVerdict(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h),
			&types.MsgDisputeVerdict{Creator: op, JobId: "jSel"})
		require.NoError(t, err, "the primary disputes its own job")
		// A one-seat redo jury is anchored and stays silent.
		aeReg(f, t, "mRedo", 1000)
		redoAnchor(f, t, "jSel", "mRedo")
		end := h + int64(p.AuditResolveTimeout)
		require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(end).WithHeaderInfo(header.Info{Height: end, AppHash: []byte("ah")})))

		job, _ := f.keeper.Job.Get(f.ctx, "jSel")
		held, hErr := f.keeper.HeldFee.Get(f.ctx, "jSel")
		if selected {
			require.NoError(t, hErr, "selected: the retention stays held")
			require.Equal(t, uint64(80000), held)
			require.Equal(t, int64(0), e14Udndr(f, opAcc), "the self-dispute bought nothing")
			require.NotContains(t, job.State, "resolved", "no verdict, no resolution")
			require.Equal(t, p.DisputeBond, job.DisputeBond, "the bond stays in escrow while an instance remains")
			require.Equal(t, []int64{end + int64(p.AuditResolveTimeout)}, d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jSel"),
				"deferred to the next deadline")
			continue
		}
		require.Error(t, hErr, "control: a never-selected job with a silent redo jury is released")
		require.Contains(t, job.State, "resolved")
		require.Equal(t, int64(80000+1000), e14Udndr(f, opAcc), "control: retention released and bond returned")
	}
}

// Resolution is final: a deadline reached by a job already resolved (here by AdjudicateDispute, whose
// vindication still owes the retention to an absent primary) changes nothing and moves no funds. The
// control is the same debt on an open audit, which this deadline unwinds.
func TestADR059Decision8_AStaleDeadlineDoesNotReopenAResolvedAudit(t *testing.T) {
	for _, resolved := range []bool{true, false} {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, d8Params(t, 500)))
		aeFundModule(f)
		clientAcc, client := d8Account(t, f, "client_d8_replay____")
		state := "open+paid+optimistic+disputed"
		if resolved {
			state += "+resolved+adjudicated"
		}
		// mGone has no miner record: releaseHeld keeps the retention and queues the release.
		before := types.Job{JobId: "jDone", State: state, MinerId: "mGone", Client: client, Fee: 100000}
		require.NoError(t, gvSetJob(f, f.ctx, "jDone", before))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jDone", 80000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jDone", 1))
		require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "jDone"))
		require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(d8H, "jDone")))

		require.NoError(t, f.keeper.EndBlock(d8Ctx(f, d8H)))

		job, err := f.keeper.Job.Get(f.ctx, "jDone")
		require.NoError(t, err)
		if !resolved {
			require.Contains(t, job.State, "unwound", "control: an open audit at this deadline is unwound")
			require.Equal(t, int64(80000), e14Udndr(f, clientAcc))
			continue
		}
		require.Equal(t, before, job, "the resolved job is not written again")
		held, hErr := f.keeper.HeldFee.Get(f.ctx, "jDone")
		require.NoError(t, hErr, "the retention owed to the vindicated primary stays held")
		require.Equal(t, uint64(80000), held)
		require.Equal(t, int64(0), e14Udndr(f, clientAcc), "and is not refunded to the client")
		queued, qErr := f.keeper.PendingHeldRelease.Has(f.ctx, "jDone")
		require.NoError(t, qErr)
		require.True(t, queued, "the release to the primary stays queued")
		require.Empty(t, d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jDone"), "no new deadline for a resolved audit")
		require.Empty(t, d8HeightsOf(t, f, f.keeper.PendingAppealResolve, "jDone"))
	}
}

// The floor deferral files its retry auditDeferStride blocks after the current height, whatever height
// the due entry was filed under: an entry left behind by a capped block must not come back already due.
func TestADR059Decision8_AFloorDeferralCountsItsRetryFromTheCurrentHeight(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, d8Params(t, 0)))
	aeFundModule(f)
	_, client := d8Account(t, f, "client_d8_stride____")
	aeReg(f, t, "mStride", aeStake) // the only miner: the draw seats nobody
	require.NoError(t, gvSetJob(f, f.ctx, "jStride", types.Job{
		JobId: "jStride", State: "open+paid+optimistic", MinerId: "mStride", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jStride", 80000))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jStride", 1))
	filed := d8H - 50
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(filed, "jStride")))

	require.NoError(t, f.keeper.EndBlock(d8Ctx(f, d8H)))

	require.Equal(t, []int64{d8H + keeper.AuditDeferStrideForTest}, e14PendingAuditOf(t, f, "jStride"),
		"one entry, a stride after the current height; the filed one is gone")
}

// The appeal deadline comes first only when allowAppeal and appeal_window say so. At appeal_window 0
// the deferral writes no appeal deadline and announces none; the twin at 60 does.
func TestADR059Decision8_AnAppealWindowOfZeroSchedulesNoAppeal(t *testing.T) {
	setup := func(t *testing.T, window uint64) (*fixture, types.Params) {
		f := initFixture(t)
		p := d8Params(t, 0)
		p.AppealWindow = window
		require.NoError(t, p.Validate(), "premise: the configuration validates")
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		_, client := d8Account(t, f, "client_d8_appeal____")
		aeReg(f, t, "mApp", aeStake)
		for _, id := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
			aeReg(f, t, id, aeStake)
		}
		aeAnchor(f, t, "jApp", "mJ1", "mJ2", "mJ3", "mJ4") // anchored and silent: below the bar
		require.NoError(t, gvSetJob(f, f.ctx, "jApp", types.Job{
			JobId: "jApp", State: "open+paid+optimistic+disputed", MinerId: "mApp", Client: client, Fee: 100000,
			Disputer: f.authority,
		}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jApp", 80000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jApp", 1))
		return f, p
	}

	t.Run("direct call", func(t *testing.T) {
		for _, window := range []uint64{0, 60} {
			f, p := setup(t, window)
			require.NoError(t, f.keeper.ResolveDisputedAuditForDecision8Test(d8Ctx(f, d8H), d8H, "jApp", p, true))
			appeal := d8HeightsOf(t, f, f.keeper.PendingAppealResolve, "jApp")
			resolve := d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jApp")
			if window == 0 {
				require.Empty(t, appeal, "appeal_window 0: no appeal deadline")
				require.Equal(t, []int64{d8H + int64(p.AuditResolveTimeout)}, resolve, "the plain deferral instead")
				continue
			}
			require.Equal(t, []int64{d8H + int64(window)}, appeal, "twin: an armed window schedules the appeal")
			require.Empty(t, resolve)
		}
	})

	t.Run("end block", func(t *testing.T) {
		f, p := setup(t, 0)
		require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(d8H, "jApp")))
		ctx := d8Ctx(f, d8H)
		require.NoError(t, f.keeper.EndBlock(ctx))
		var appealed, deferred bool
		for _, ev := range ctx.EventManager().Events() {
			switch ev.Type {
			case "audit_appeal_scheduled":
				appealed = true
			case "audit_resolve_deferred":
				deferred = true
			}
		}
		require.False(t, appealed, "no appeal is announced while appeal_window is 0")
		require.True(t, deferred, "the audit is deferred")
		require.Equal(t, []int64{d8H + int64(p.AuditResolveTimeout)}, d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jApp"))
		require.Empty(t, d8HeightsOf(t, f, f.keeper.PendingAppealResolve, "jApp"))
	})
}
