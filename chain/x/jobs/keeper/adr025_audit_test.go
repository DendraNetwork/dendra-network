package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-025 — AUDIT DRAW in EndBlock. Under optimistic mode, a job settled with k=1 is scheduled for
// audit at the NEXT block; at audit_sample_bps=10000 (100 %) the draw is deterministic, so the job
// moves to +disputed — a protocol dispute that the fresh committee will settle. DORMANT: in mode 0,
// settleOptimistic never runs, so there is no PendingAudit entry at all.
func TestADR025OptimisticAuditEndBlock(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 % -> deterministic draw (always audited)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Optimistic settlement at block 5 -> audit scheduled at block 6.
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
	idM1 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(ctx5, &types.MsgCreateMiner{Creator: creator, MinerId: idM1, Operator: creator, Stake: 1000})
	require.NoError(t, err)
	// JUDGE POOL. The audit committee EXCLUDES the primary: with `m1` as the only registered miner
	// there would be ZERO eligible judge, hence a quorum unreachable by construction. Since
	// `deferAuditForSmallJury`, that case is (rightly) DEFERRED instead of opening a dispute whose only
	// possible outcome was the clawback of an honest miner. This test observes the DRAW, not the pool,
	// so it is given a viable network; otherwise it would measure the guard instead of the mechanism.
	//
	// STAKE 1 against 1000 for `m1`: the PRIMARY is itself drawn stake-weighted among ALL miners. Judges
	// with an equal stake would push the primary away from `m1`, and the commit `jobA__m1` would no
	// longer match the expected primary — exactly the commit/primary divergence that once blocked every
	// settlement. `m1` is therefore made dominant in the draw while a pool of eligible judges exists.
	// Written DIRECTLY rather than through CreateMiner: a stake of 1 is below `min_stake` and the
	// message would refuse it, and the real bond is beside the point here.
	for _, jid := range []string{"mj1", "mj2", "mj3", "mj4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, jid, types.Miner{MinerId: jid, Stake: 1, Operator: creator}))
	}
	_, err = srv.OpenJob(ctx5, &types.MsgOpenJob{Creator: creator, JobId: "jobA", Fee: 30})
	require.NoError(t, err)
	_, err = srv.CreateCommit(ctx5, &types.MsgCreateCommit{Creator: creator, JobId: "jobA__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err)
	_, err = srv.SettleSemantic(ctx5, &types.MsgSettleSemantic{Creator: creator, JobId: "jobA"})
	require.NoError(t, err)

	// No audit at settlement time; the entry is scheduled at h+1=6.
	job, err := f.keeper.Job.Get(ctx5, "jobA")
	require.NoError(t, err)
	require.NotContains(t, job.State, "disputed", "no audit at settlement time")
	has, err := f.keeper.PendingAudit.Has(ctx5, collections.Join(int64(6), "jobA"))
	require.NoError(t, err)
	require.True(t, has, "audit scheduled at h+1=6")

	// EndBlock at block 6 -> 100 % draw -> protocol dispute opened and the entry consumed.
	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("apphash-6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err = f.keeper.Job.Get(ctx6, "jobA")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "audited -> +disputed")
	require.Equal(t, int64(6), job.DisputeHeight, "audit height recorded")
	has, err = f.keeper.PendingAudit.Has(ctx6, collections.Join(int64(6), "jobA"))
	require.NoError(t, err)
	require.False(t, has, "audit entry consumed (a single decision)")
}
