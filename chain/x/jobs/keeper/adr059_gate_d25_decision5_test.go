package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Consensus epoch 15, decision 5: the jury is the smaller of the draw size and the eligible judges,
// the draw size and its compiled default stay what they are, the safety net that raises the size to
// audit_min_quorum stays, and below the jury floor the audit defers.

// Decision 5: the compiled default of audit_committee_draw_size is 15, and both draws seat 15 of 20
// eligible judges at the default parameters.
func TestADR059_TheCompiledDrawSizeIsFifteenOnBothDraws(t *testing.T) {
	const h = uint64(1_000_000)
	require.Equal(t, 15, keeper.AuditCommitteeDrawSizeForTest)
	f := initFixture(t)
	p := types.DefaultParams()
	require.Zero(t, p.AuditCommitteeDrawSize, "premise: 0 selects the compiled default")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	for i := 0; i < 20; i++ {
		d25Judge(t, f, fmt.Sprintf("d25-c%02d", i), h, h)
	}
	audit, redo := d25DrawBoth(t, f, "d25-jCompiled", h)
	require.Len(t, audit, 15, "the audit jury at the compiled draw size")
	require.Len(t, redo, 15, "the redo jury at the compiled draw size")
}

// Decision 5: a posted audit_committee_draw_size is the size of both juries when more judges are
// eligible than there are seats.
func TestADR059_BothDrawsSeatThePostedDrawSize(t *testing.T) {
	const h = uint64(1_000_000)
	f := initFixture(t)
	p := types.DefaultParams()
	p.AuditCommitteeDrawSize = 4
	require.NoError(t, p.Validate(), "premise: a parameter set governance can post")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	for i := 0; i < 8; i++ {
		d25Judge(t, f, fmt.Sprintf("d25-g%02d", i), h, h)
	}
	audit, redo := d25DrawBoth(t, f, "d25-jPosted", h)
	require.Len(t, audit, 4, "the audit jury at the posted size")
	require.Len(t, redo, 4, "the redo jury at the posted size")
}

// Decision 5: the safety net that raises the draw size to audit_min_quorum stays. A quorum above the
// compiled size of 15 is a valid parameter set, and the audit jury is then drawn at the quorum, or at
// every eligible judge when fewer are eligible.
func TestADR059_TheSafetyNetRaisesTheAuditJuryToTheQuorum(t *testing.T) {
	const h = uint64(1_000_000)
	for _, c := range []struct {
		judges int
		want   int
	}{
		{25, 20},
		{18, 18},
	} {
		f := initFixture(t)
		p := types.DefaultParams()
		p.AuditMinQuorum = 20
		require.NoError(t, p.Validate(), "premise: a parameter set governance can post")
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		for i := 0; i < c.judges; i++ {
			d25Judge(t, f, fmt.Sprintf("d25-q%02d", i), h, h)
		}
		require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "d25-jNet", h))
		ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(h))
		got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "d25-seed", "d25-jNet", "", "")
		require.NoError(t, err)
		require.Len(t, got, c.want, "%d eligible judges, audit_min_quorum=20, audit_committee_draw_size=0", c.judges)
	}
}

// Decision 5: below the jury floor the audit defers as before epoch 15, and the floor counts declared
// judges only. Besides the primary the pool holds two undeclared miners and three or four judges.
// Three is one below the floor of four: the audit defers with no anchor. Four opens it on four seats.
func TestADR059_BelowTheFloorOfDeclaredJudgesTheAuditDefers(t *testing.T) {
	for _, c := range []struct {
		judges int
		opens  bool
	}{
		{3, false},
		{4, true},
	} {
		f := initFixture(t)
		p := types.DefaultParams()
		p.VerificationMode = 1
		p.AuditSampleBps = 10000 // the audit is certain: the case is the floor, not the lottery
		p.AuditMinQuorum = 4
		p.AuditResolveTimeout = 240
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		aeReg(f, t, "d25-p", aeStake) // the primary
		for i := 0; i < c.judges; i++ {
			aeReg(f, t, fmt.Sprintf("d25-fj%d", i), aeStake) // gvSetMiner declares the miner at 0
		}
		for i := 0; i < 2; i++ {
			a59Miner(t, f, fmt.Sprintf("d25-fu%d", i), aeStake) // registered, never declared
		}
		clientAcc := sdk.AccAddress([]byte("client_adr059_d25fl_"))
		client, err := f.addressCodec.BytesToString(clientAcc)
		require.NoError(t, err)
		f.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, gvSetJob(f, f.ctx, "d25-jFloor", types.Job{
			JobId: "d25-jFloor", State: "open+paid+optimistic", MinerId: "d25-p", Client: client, Fee: 100000,
		}))
		require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "d25-jFloor")))

		require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

		job, err := f.keeper.Job.Get(f.ctx, "d25-jFloor")
		require.NoError(t, err)
		raw, cErr := f.keeper.AuditCommittee.Get(f.ctx, "d25-jFloor")
		deferred, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(int64(6)+keeper.AuditDeferStrideForTest, "d25-jFloor"))
		require.NoError(t, err)
		if c.opens {
			require.Contains(t, job.State, "disputed", "%d judges: the floor is met and the audit opens", c.judges)
			require.NoError(t, cErr)
			require.Len(t, splitNonEmpty(raw), 4, "the jury is the four declared judges")
			require.False(t, deferred)
			continue
		}
		require.NotContains(t, job.State, "disputed", "%d judges: below the floor no audit opens", c.judges)
		require.Error(t, cErr, "below the floor no jury is anchored")
		require.True(t, deferred, "the audit is deferred, never dropped")
		selected, err := f.keeper.AuditSelected.Has(f.ctx, "d25-jFloor")
		require.NoError(t, err)
		require.True(t, selected, "the selection is kept for the retry")
	}
}

// Decision 5: audit_committee_draw_size keeps its bound, effectiveAuditFloor (types/params.go). At
// audit_min_quorum=0 that bound is AuditFallbackFloor (4): a posted size below it is refused by
// Validate, by governance and by the genesis check. The bound reaches 4 only there or in mode 1, where
// Validate keeps a governed quorum at 4 or more. In mode 0 a governed quorum of 1 to 3 lowers the
// bound to the quorum, so a draw size of 1 to 3 can be stored; adr059_gate_d25_mode0_test.go draws
// both juries on such sets.
func TestADR059_ADrawSizeBelowTheJuryFloorCannotBePosted(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.Zero(t, types.DefaultParams().AuditMinQuorum, "premise: the fallback floor applies")
	for size := uint64(1); size < types.AuditFallbackFloor; size++ {
		p := types.DefaultParams()
		p.AuditCommitteeDrawSize = size
		require.Error(t, p.Validate(), "draw size %d", size)
		_, err := srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
		require.Error(t, err, "draw size %d through governance", size)
		g := types.DefaultGenesis()
		g.Params = p
		require.Error(t, g.Validate(), "draw size %d in a genesis", size)
	}
	p := types.DefaultParams()
	p.AuditCommitteeDrawSize = types.AuditFallbackFloor
	_, err := srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
	require.NoError(t, err, "the floor itself is a valid size")
	stored, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(types.AuditFallbackFloor), stored.AuditCommitteeDrawSize)

	// In mode 0 the bound follows a governed quorum: one below it is refused, the quorum is stored.
	for q := uint64(1); q < types.AuditFallbackFloor; q++ {
		p := types.DefaultParams()
		p.AuditMinQuorum = q
		if q > 1 {
			p.AuditCommitteeDrawSize = q - 1
			_, err := srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
			require.Error(t, err, "draw size %d below audit_min_quorum=%d", q-1, q)
		}
		p.AuditCommitteeDrawSize = q
		_, err := srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
		require.NoError(t, err, "draw size %d at audit_min_quorum=%d in mode 0", q, q)
		stored, err := f.keeper.Params.Get(f.ctx)
		require.NoError(t, err)
		require.Equal(t, q, stored.AuditCommitteeDrawSize)
	}
}
