package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-026 — RESOLUTION BY VERDICT (LLM-as-judge). The fresh committee commits a binary verdict under
// "<jobId>__verdict__<minerId>" ("0" = primary invalid, anything else = valid) INSTEAD of an embedding;
// AdjudicateDispute tallies the STAKE MAJORITY of those verdicts and slashes the primary when the
// verdict is invalid, otherwise the primary is vindicated. With no verdict at all, the cosine path is
// used as a fallback (covered by TestADR025OptimisticSlash).
func TestADR026VerdictSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_adr_026_01_")))
	require.NoError(t, err)

	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} { // original committee (large stake)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF", "mG"} { // fresh judging committee (small stake, disjoint) — 4 is the floor
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	p := types.DefaultParams()
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) CHEATING: the fresh committee unanimously verdicts "0" (invalid) -> the primary mA is slashed.
	aeAnchor(f, t, "jv", "mD", "mE", "mF", "mG") // ADR-032: jurors SUMMONED (without anchoring, the tally fails closed)
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mD", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mE", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mF", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mG", types.Commit{ResultCommit: "0"}))
	require.NoError(t, gvSetJob(f, f.ctx, "jv", types.Job{
		JobId: "jv", State: "open+paid+optimistic+disputed", MinerId: "mA",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jv"})
	require.NoError(t, err)
	gotJob, _ := f.keeper.Job.Get(f.ctx, "jv")
	require.Contains(t, gotJob.State, "resolved")
	require.Len(t, gotJob.SlashRecords, 1, "invalid verdict -> the primary is slashed")
	require.Equal(t, "mA", gotJob.SlashRecords[0].MinerId)
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, bigStake-bigStake*8000/10000, mA.Stake, "80 % slash")

	// (b) VALID: verdicts are "1" -> the primary mB is NOT slashed.
	aeAnchor(f, t, "jok", "mD", "mE", "mF", "mG") // ADR-032: jurors SUMMONED (without anchoring, the tally fails closed)
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mD", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mE", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mF", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mG", types.Commit{ResultCommit: "1"}))
	require.NoError(t, gvSetJob(f, f.ctx, "jok", types.Job{
		JobId: "jok", State: "open+paid+optimistic+disputed", MinerId: "mB",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	mBbefore, _ := f.keeper.Miner.Get(f.ctx, "mB")
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jok"})
	require.NoError(t, err)
	gotOk, _ := f.keeper.Job.Get(f.ctx, "jok")
	require.Contains(t, gotOk.State, "resolved")
	require.Len(t, gotOk.SlashRecords, 0, "valid verdict -> no slash")
	mBafter, _ := f.keeper.Miner.Get(f.ctx, "mB")
	require.Equal(t, mBbefore.Stake, mBafter.Stake, "the honest primary's stake is untouched")
}
