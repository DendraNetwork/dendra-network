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

// ADR-025: audit draw in EndBlock. In optimistic mode, a job settled with k=1 is scheduled for audit at
// the next block; at audit_sample_bps=10000 (100 %) the draw is deterministic, so the job moves to
// +disputed, a protocol dispute that the fresh committee will settle. Dormant in mode 0: settleOptimistic
// never runs, so there is no PendingAudit entry at all.
func TestADR025OptimisticAuditEndBlock(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 %: deterministic draw (always audited)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Optimistic settlement at block 5, audit scheduled at block 6.
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
	// Registered at block 4, the job opens at 5. A job freezes its pool on the block before its own
	// (`frozenPoolAnchor`), so a miner appearing in the opening block is not in that pool; that is the rule.
	// Registering one block earlier is also what an operator does: it registers, and clients open jobs
	// afterwards. This case observes the audit draw, so it gets a miner the draw can reach.
	idM1 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4),
		&types.MsgCreateMiner{Creator: creator, MinerId: idM1, Operator: creator, Stake: 1000})
	require.NoError(t, err)
	// Judge pool. The audit committee excludes the primary: with `m1` as the only registered miner there
	// would be no eligible judge, so the quorum would be unreachable. Since `deferAuditForSmallJury`, that
	// case is deferred instead of opening a dispute whose only possible outcome was the clawback of an
	// honest miner. This test observes the draw, so it gets a viable network; otherwise it would measure the
	// guard instead of the mechanism.
	//
	// Stake 1 against 1000 for `m1`: the primary is itself drawn stake-weighted among all miners. Judges with
	// an equal stake would move the primary away from `m1`, and the commit `jobA__m1` would no longer match
	// the expected primary, the commit/primary divergence that once blocked every settlement. `m1` is
	// therefore dominant in the draw while a pool of eligible judges exists. Written directly instead of
	// through CreateMiner: a stake of 1 is below `min_stake` and the message would refuse it, and the real
	// bond does not matter here.
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

	// EndBlock at block 6: 100 % draw, protocol dispute opened and the entry consumed.
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
