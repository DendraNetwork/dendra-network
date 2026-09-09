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

// ADR-025 — ADAPTIVE SAMPLING: a large job has its audit rate raised by its own value.
// base=1 (≈0 %) with audit_adaptive=1 (reference) gives effective = 1 + fee/1, saturating at 10000 for
// a large fee, hence a deterministic audit. (Dormant when audit_adaptive=0, covered by the draw test.)
func TestADR025AdaptiveSampling(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1
	p.AuditAdaptive = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// JUDGE POOL: this test is about the adaptive audit RATE, not about the pool. With no registered
	// miner the committee would be EMPTY and the audit is now DEFERRED rather than opened
	// (`deferAuditForSmallJury`: an unreachable quorum must never end in the clawback of an honest
	// miner). The primary comes from the job here, not from a draw, so these judges do not displace it.
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: 1000}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jBig", types.Job{JobId: "jBig", State: "open+paid+optimistic", MinerId: "m1", Fee: 50000}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jBig")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err := f.keeper.Job.Get(ctx6, "jBig")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "large job -> adaptive audit saturated at 100 % -> audited")
}

// ADR-025 — anti-Sybil PROBATION: a new miner (counter <= N) is audited at 100 % even when the base
// rate is near zero. base=1, audit_probation_jobs=5 and counter=1 give effective=10000, a
// deterministic audit.
func TestADR025Probation(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1
	p.AuditProbationJobs = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// JUDGE POOL — same reason as in the adaptive-sampling test: without eligible judges the audit is
	// DEFERRED and this test would measure the guard instead of the PROBATION rate it defends.
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: 1000}))
	}
	require.NoError(t, f.keeper.MinerOptimisticCount.Set(f.ctx, "mNew", uint64(1))) // new miner: first job
	require.NoError(t, gvSetJob(f, f.ctx, "jNew", types.Job{JobId: "jNew", State: "open+paid+optimistic", MinerId: "mNew", Fee: 10}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jNew")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	job, err := f.keeper.Job.Get(ctx6, "jNew")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "miner on probation -> audited at 100 %")
}

// LIVENESS (ADR-025) — the loop must survive a deadline that nobody honoured. The deadline is set
// directly, then EndBlock runs.
func TestADR025AuditResolveTimeout(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, gvSetJob(f, f.ctx, "jStuck", types.Job{JobId: "jStuck", State: "open+paid+optimistic+disputed", MinerId: "m1"}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jStuck")))

	ctx6 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
	require.NoError(t, f.keeper.EndBlock(ctx6))

	// The liveness being defended has a precise shape. Reading "no job is stuck" as "the timeout
	// resolves it" means resolving without a verdict, i.e. auto-vindicating (then clawing back) an audit
	// nobody conducted. Liveness is the liveness of the LOOP: today's deadline is consumed AND a new one
	// is set — the job is never abandoned, and never closed blind either. RED if the consumed deadline
	// is not replaced (a permanent silent freeze, the real death of liveness), or if the job closes
	// without a verdict.
	job, err := f.keeper.Job.Get(ctx6, "jStuck")
	require.NoError(t, err)
	require.NotContains(t, job.State, "resolved", "no verdict -> nothing closes (deferral)")
	has, err := f.keeper.PendingAuditResolve.Has(ctx6, collections.Join(int64(6), "jStuck"))
	require.NoError(t, err)
	require.False(t, has, "today's deadline consumed")
	next, err := f.keeper.PendingAuditResolve.Has(ctx6, collections.Join(int64(6)+keeper.AuditDeferStrideForTest, "jStuck"))
	require.NoError(t, err)
	require.True(t, next, "a new deadline is set (no-deferral fallback, timeout ungoverned here): the loop is ALIVE")
}
