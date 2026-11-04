package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Integrated scenario: an audit outlasts the prune window.
//
// The pieces were tested separately (TestPruneNeverEvacuatesMinerWithHeldFee, the PendingHeldRelease
// queue, the held-summary tests). This test covers the end-to-end path that unbounded audit deferral
// makes reachable: an honest primary whose audit drags past `miner_prune_blocks` stops producing, so it
// looks inactive and becomes an eviction candidate while its held fee is still owed. If the guard gave
// way, `releaseHeld` would no longer find the miner and would return without paying, and since the held
// entry has already left the store, network slowness alone would destroy an honest miner's payment. That
// violates invariant 1: an infrastructure outage is never billed to the honest party.
//
// It stitches four properties into one trajectory: (a) not evicted, (b) the held fee survives, (c) the
// held summary still counts it and its age keeps rising, (d) the honest miner is paid when the audit
// resolves.

func TestAuditOutlastsPruneWindow_HonestPrimaryFullyProtected(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.MinerPruneBlocks = 20 // short window: 21 blocks of silence are enough to make it a candidate
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	qs := keeper.NewQueryServerImpl(f.keeper)

	opAcc := sdk.AccAddress([]byte("operator_honest_p2__"))
	op, err := f.addressCodec.BytesToString(opAcc)
	require.NoError(t, err)
	f.bank.setBalance(opAcc, sdk.NewCoins()) // baseline 0, so the received held fee is measured exactly

	// Honest primary of an open audit (disputed, unresolved), silent for 21 blocks (> the 20-block window).
	// Unbounded deferral silenced it, through no fault of its own: its held fee is still owed.
	require.NoError(t, gvSetMiner(f, f.ctx, "mHonest", types.Miner{
		MinerId: "mHonest", Stake: aeStake, Operator: op, Creator: op,
	}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mHonest", uint64(vitH)-21))
	require.NoError(t, gvSetJob(f, f.ctx, "jAudit", types.Job{
		JobId: "jAudit", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jAudit", uint64(100000)))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jAudit", 1)) // very old held fee

	// (1) Prune past the window: the primary stays, and so does the held fee.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.NoError(t, gErr,
		"(a) primary of an open audit with a held fee owed: never evicted, even after more than miner_prune_blocks of silence")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jAudit")
	require.NoError(t, hErr, "(b) the held fee survives the prune")
	require.Equal(t, uint64(100000), held)

	// (c) The held summary still counts it, and its age keeps rising.
	resp, err := qs.HeldSummary(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(100000), resp.HeldTotal,
		"(c) held-summary still counts the honest miner's held fee: \"held is not lost\" stays falsifiable during a long audit")
	require.Equal(t, uint64(1), resp.HeldCount)
	require.Greater(t, resp.OldestAgeBlocks, uint64(0), "and its age rises: the staleness alarm sees it age")
	require.Equal(t, "jAudit", resp.OldestJobId)

	// (d) The audit resolves and the PendingHeldRelease replay settles the honest miner. The held fee was
	// deferred by the slow audit, never lost; once the audit resolves, the epoch sweep pays the miner.
	job, _ := f.keeper.Job.Get(f.ctx, "jAudit")
	job.State = job.State + "+resolved+vindicated+quorum"
	require.NoError(t, gvSetJob(f, f.ctx, "jAudit", job))
	require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "jAudit")) // claimable debt (releaseHeld enqueues it on a deferred send)

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH+100)))

	_, he := f.keeper.HeldFee.Get(f.ctx, "jAudit")
	require.Error(t, he, "(d) held fee settled by the epoch sweep after resolution")
	require.Equal(t, int64(100000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(),
		"the honest miner is paid in full: the long audit destroyed nothing — invariant (1) holds end to end")
	stillQ, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jAudit")
	require.False(t, stillQ, "settled -> removed from the replay queue")
}
