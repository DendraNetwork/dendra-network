package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// INTEGRATED SCENARIO: AN AUDIT OUTLASTS THE PRUNE WINDOW.
//
// The pieces existed separately (TestPruneNeverEvacuatesMinerWithHeldFee; the PendingHeldRelease
// queue; the held-summary tests). What no test exercised is the end-to-end path that unbounded audit
// deferral makes reachable: an HONEST primary whose audit drags past `miner_prune_blocks` stops
// producing, therefore looks "inactive", therefore becomes an eviction candidate WHILE ITS HELD FEE
// IS STILL OWED. If the guard gave way, `releaseHeld` would no longer find it, would return without
// paying, and since the held entry has already left the KV store, an honest miner's payment would be
// destroyed by network slowness alone — a direct violation of invariant ① (an infrastructure outage
// is never billed to the honest party).
//
// This test stitches four properties into ONE trajectory: (a) not evicted, (b) held fee survives,
// (c) held-summary still counts it and its age keeps rising, (d) the honest miner is eventually paid
// when the audit resolves.

func TestAuditOutlastsPruneWindow_HonestPrimaryFullyProtected(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.MinerPruneBlocks = 20 // SHORT window: 21 blocks of silence are enough to make it a candidate
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	qs := keeper.NewQueryServerImpl(f.keeper)

	opAcc := sdk.AccAddress([]byte("operator_honest_p2__"))
	op, err := f.addressCodec.BytesToString(opAcc)
	require.NoError(t, err)
	f.bank.setBalance(opAcc, sdk.NewCoins()) // baseline 0 -> the received held fee is measured EXACTLY

	// Honest primary of an OPEN audit (disputed, unresolved), mute for 21 blocks (> the 20-block
	// window). Unbounded deferral is what silenced it, not a fault: its held fee is still owed.
	require.NoError(t, gvSetMiner(f, f.ctx, "mHonest", types.Miner{
		MinerId: "mHonest", Stake: aeStake, Operator: op, Creator: op,
	}))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mHonest", uint64(vitH)-21))
	require.NoError(t, gvSetJob(f, f.ctx, "jAudit", types.Job{
		JobId: "jAudit", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jAudit", uint64(100000)))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jAudit", 1)) // VERY old held fee

	// ---- (1) PRUNE past the window: the primary holds, the held fee holds. ----
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.NoError(t, gErr,
		"(a) primary of an OPEN audit with a held fee owed: NEVER evicted, even after more than miner_prune_blocks of silence")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jAudit")
	require.NoError(t, hErr, "(b) the held fee SURVIVES the prune")
	require.Equal(t, uint64(100000), held)

	// ---- (c) held-summary still counts it, and its age keeps RISING. ----
	resp, err := qs.HeldSummary(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH), &types.QueryHeldSummaryRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(100000), resp.HeldTotal,
		"(c) held-summary STILL counts the honest miner's held fee: \"held is not lost\" stays falsifiable during a long audit")
	require.Equal(t, uint64(1), resp.HeldCount)
	require.Greater(t, resp.OldestAgeBlocks, uint64(0), "and its age rises: the staleness alarm sees it age")
	require.Equal(t, "jAudit", resp.OldestJobId)

	// ---- (d) the audit eventually resolves; the PendingHeldRelease replay settles the honest miner. ----
	// The held fee was not lost, only deferred by the slowness of the audit. Once the audit resolves,
	// the epoch sweep pays the honest miner.
	job, _ := f.keeper.Job.Get(f.ctx, "jAudit")
	job.State = job.State + "+resolved+vindicated+quorum"
	require.NoError(t, gvSetJob(f, f.ctx, "jAudit", job))
	require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "jAudit")) // claimable debt (releaseHeld enqueues it on a deferred send)

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH+100)))

	_, he := f.keeper.HeldFee.Get(f.ctx, "jAudit")
	require.Error(t, he, "(d) held fee SETTLED by the epoch sweep after resolution")
	require.Equal(t, int64(100000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(),
		"the honest miner IS paid in full: the long audit destroyed nothing — invariant ① holds end to end")
	stillQ, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jAudit")
	require.False(t, stillQ, "settled -> removed from the replay queue")
}
