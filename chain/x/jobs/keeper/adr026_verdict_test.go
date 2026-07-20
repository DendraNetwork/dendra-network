package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// ADR-026 J3 — RÉSOLUTION PAR VERDICT (LLM-as-juge). Le comité frais commit un verdict binaire sous
// "<jobId>__verdict__<minerId>" ("0" = primaire invalide, sinon valide) AU LIEU d'un embedding ;
// AdjudicateDispute tally la MAJORITÉ DE STAKE des verdicts -> slash 80 % du primaire si invalide, sinon
// vindiqué. Repli cosinus si aucun verdict (rétro-compat, couvert par TestADR025OptimisticSlash).
func TestADR026VerdictSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_adr_026_01_")))
	require.NoError(t, err)

	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} { // comité d'origine (gros stake)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF", "mG"} { // comité frais juge (petit stake, hors origine) — 4 = plancher
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	p := types.DefaultParams()
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) TRICHE : verdicts du comité frais = "0" (invalide) à l'unanimité (4 = plancher) -> SLASH 80 % du primaire mA.
	aeAnchor(f, t, "jv", "mD", "mE", "mF", "mG") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mD", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mE", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mF", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jv__verdict__mG", types.Commit{ResultCommit: "0"}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jv", types.Job{
		JobId: "jv", State: "open+paid+optimistic+disputed", MinerId: "mA",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jv"})
	require.NoError(t, err)
	gotJob, _ := f.keeper.Job.Get(f.ctx, "jv")
	require.Contains(t, gotJob.State, "resolved")
	require.Len(t, gotJob.SlashRecords, 1, "verdict invalide -> primaire slashé")
	require.Equal(t, "mA", gotJob.SlashRecords[0].MinerId)
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, bigStake-bigStake*8000/10000, mA.Stake, "slash 80 %")

	// (b) VALIDE : verdicts = "1" (4 = plancher) -> AUCUN slash du primaire mB.
	aeAnchor(f, t, "jok", "mD", "mE", "mF", "mG") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mD", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mE", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mF", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jok__verdict__mG", types.Commit{ResultCommit: "1"}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jok", types.Job{
		JobId: "jok", State: "open+paid+optimistic+disputed", MinerId: "mB",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	mBbefore, _ := f.keeper.Miner.Get(f.ctx, "mB")
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jok"})
	require.NoError(t, err)
	gotOk, _ := f.keeper.Job.Get(f.ctx, "jok")
	require.Contains(t, gotOk.State, "resolved")
	require.Len(t, gotOk.SlashRecords, 0, "verdict valide -> pas de slash")
	mBafter, _ := f.keeper.Miner.Get(f.ctx, "mB")
	require.Equal(t, mBbefore.Stake, mBafter.Stake, "stake du primaire honnête intact")
}
