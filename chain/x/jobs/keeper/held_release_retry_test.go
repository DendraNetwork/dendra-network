package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A HELD PAYMENT STAYS CLAIMABLE. On a failed send, `releaseHeld` kept the held amount but no
// handler ever replayed it, so "still claimable" was FALSE: it was a slow confiscation. The
// `PendingHeldRelease` queue plus a once-per-epoch EndBlocker sweep close that gap.

// A FAILED send enqueues the job for retry, instead of leaving it stuck in silence.
func TestHeldRelease_FailedSendEnqueuesRetry(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 0 // never selected for audit -> releaseHeld (optimistic finalisation)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// The module account is DELIBERATELY unfunded, so sending the held amount will fail.
	op, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("operator_held_feed___")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mF", types.Miner{MinerId: "mF", Stake: aeStake, Operator: op, Creator: op}))
	require.NoError(t, gvSetJob(f, f.ctx, "jF", types.Job{JobId: "jF", State: "open+paid+optimistic", MinerId: "mF", Fee: 100000}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jF", uint64(50000)))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jF")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6)))

	enq, e := f.keeper.PendingHeldRelease.Has(f.ctx, "jF")
	require.NoError(t, e)
	require.True(t, enq, "a failed send must ENQUEUE the job for retry, rather than leave it stuck in silence")
	held, _ := f.keeper.HeldFee.Get(f.ctx, "jF")
	require.Equal(t, uint64(50000), held, "the held amount is KEPT, never erased on failure")
}

// The epoch sweep retries and SETTLES a stuck held payment as soon as the send becomes possible.
func TestHeldRelease_EpochSweepDrainsStuckRelease(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f) // module funded, so the send will succeed during the sweep
	opAcc := sdk.AccAddress([]byte("operator_held_drain__"))
	op, err := f.addressCodec.BytesToString(opAcc)
	require.NoError(t, err)
	f.bank.setBalance(opAcc, sdk.NewCoins()) // baseline 0, so the received amount is measured exactly
	require.NoError(t, gvSetMiner(f, f.ctx, "mH", types.Miner{MinerId: "mH", Stake: aeStake, Operator: op, Creator: op}))
	require.NoError(t, gvSetJob(f, f.ctx, "jH", types.Job{JobId: "jH", State: "open+paid+optimistic+resolved", MinerId: "mH", Fee: 100000}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jH", uint64(50000)))
	// Simulates an EARLIER send failure: the held amount is due and enqueued for retry.
	require.NoError(t, f.keeper.PendingHeldRelease.Set(f.ctx, "jH"))

	// vitH is a multiple of 100, so the epoch sweep runs.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, he := f.keeper.HeldFee.Get(f.ctx, "jH")
	require.Error(t, he, "the held amount was SETTLED during the sweep, so HeldFee is gone")
	stillQ, _ := f.keeper.PendingHeldRelease.Has(f.ctx, "jH")
	require.False(t, stillQ, "settled, therefore removed from the retry queue")
	require.Equal(t, int64(50000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(),
		"the miner's operator did receive the held amount")
}
