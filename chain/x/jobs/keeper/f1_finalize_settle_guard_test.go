package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The FinalizeJob -> SettleSemantic double slash, closed by a TARGETED guard.
//
// Vector: in mode 0, FinalizeJob returns the verdict, slashes the divergent miners and marks
// `+finalized` WITHOUT setting paid/settled; SettleSemantic only tested jobIsPaid, so it PROCEEDED and
// re-slashed the SAME anchored commits, which are immutable and therefore still divergent. Both
// handlers are permissionless, so the penalty compounded on a miner that may well be honest — GPU
// inference is not deterministic.
//
// The guard lives in SettleSemantic ALONE, and is not a blanket jobIsTerminal check: the documented
// FinalizeJob -> Payout flow must stay open, see TestF1FinalizeThenPayoutStillWorks.
func TestF1FinalizeThenSettleSemanticRejectedNoDoubleSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	addr20 := func(s string) string {
		b := make([]byte, 20)
		copy(b, s)
		out, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return out
	}

	// 3 miners (a full committee of size 3); commits are parsable VECTORS (the SettleSemantic format):
	// ma and mb share the canonical answer, mc is the opposite vector — divergent for FinalizeJob
	// (different string) AND a cosine outlier for SettleSemantic. Without the guard, BOTH handlers
	// slash it.
	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	client := addr20("client-xyz")
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", Fee: 10000, Client: client}))
	trigger := addr20("trigger-addr")

	// (1) FinalizeJob: verdict and one slash of the divergent miner (default slash_leak_bps=8000: 1000 -> 200).
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: trigger, JobId: "j1"})
	require.NoError(t, err)
	mc, err := f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, uint64(200), mc.Stake, "expected FinalizeJob slash: 1000 -> 200 (80%%)")
	job, err := f.keeper.Job.Get(f.ctx, "j1")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "finalized"))

	// (2) SettleSemantic on the finalized job: REFUSED by the guard. Without it, the handler proceeded
	// and re-slashed mc (200 -> 40).
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: trigger, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (3) The divergent miner's stake is taken ONCE: still 200, not 40.
	mc, err = f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, uint64(200), mc.Stake, "double slash detected: the stake was taken a second time")
}

// Non-regression: the DOCUMENTED mode-0 flow `FinalizeJob (verdict) -> Payout (payment)` stays OPEN.
// Excluding `finalized` from jobIsPaid (job_state.go) is deliberate, and Payout does not slash. A
// blanket `jobIsTerminal` guard on Payout would freeze the escrow of every finalized job: winners
// never paid, client never refunded — which is exactly what this test forbids.
func TestF1FinalizeThenPayoutStillWorks(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	addr20 := func(s string) string {
		b := make([]byte, 20)
		copy(b, s)
		out, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return out
	}

	const fee = 10000
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(fee)))

	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j2__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	client := addr20("client-xyz")
	require.NoError(t, gvSetJob(f, f.ctx, "j2", types.Job{JobId: "j2", Fee: fee, Client: client}))
	trigger := addr20("trigger-addr")

	// Verdict first (slashes mc), then payment of the winners of the canonical answer: BOTH pass.
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: trigger, JobId: "j2"})
	require.NoError(t, err)
	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: trigger, JobId: "j2"})
	require.NoError(t, err, "non-regression: FinalizeJob -> Payout is the documented flow and must PASS")

	// Effective payment: 5%% burn (500) plus 2 winners at 4750 clears the escrow; mc, the divergent
	// miner, is not paid.
	require.Equal(t, "500udndr", f.bank.burned.String())
	require.True(t, f.bank.mod[types.ModuleName].Empty(), "escrow not cleared: %s", f.bank.mod[types.ModuleName])
	job, err := f.keeper.Job.Get(f.ctx, "j2")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "paid"))
}
