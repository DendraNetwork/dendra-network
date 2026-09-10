package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-025 — OPTIMISTIC AUDIT RESOLUTION through AdjudicateDispute: an optimistic primary that has
// already been PAID and whose ORIGINAL commit diverges from the fresh committee's stake majority takes
// a hard SLASH; if it agrees, the primary is vindicated and nothing moves. Fixture pattern taken from
// TestAdjudicateDispute (3 large stakes = the original committee, including the primary; 3 small ones
// = the fresh __redo__ committee, outside the origin).
func TestADR025OptimisticSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_addr_m4_01_")))
	require.NoError(t, err)

	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} { // original committee (large stake)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF"} { // fresh committee (small stake, outside the origin)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	p := types.DefaultParams() // slash_leak_bps = 8000 (80 %)
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) CHEATING: primary mA committed "1,0,0"; the fresh committee (mD,mE,mF) says "0,1,0" -> 80 % SLASH.
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__mA", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mD", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mE", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mF", types.Commit{ResultCommit: "0,1,0"}))
	redoAnchor(f, t, "jc", "mD", "mE", "mF") // ADR-033: the fresh committee must be DRAWN (summoned explicitly here)
	require.NoError(t, gvSetJob(f, f.ctx, "jc", types.Job{
		JobId: "jc", State: "open+paid+optimistic+disputed", MinerId: "mA",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jc"})
	require.NoError(t, err)
	gotJob, _ := f.keeper.Job.Get(f.ctx, "jc")
	require.Contains(t, gotJob.State, "resolved")
	require.Len(t, gotJob.SlashRecords, 1, "the primary's slash is recorded")
	require.Equal(t, "mA", gotJob.SlashRecords[0].MinerId)
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, bigStake-bigStake*8000/10000, mA.Stake, "primary slashed 80 %")
	poolsC, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, bigStake*8000/10000, poolsC.Treasury, "slashed amount -> Treasury")

	// (b) HONEST: primary mB committed "1,0,0"; the fresh committee confirms "1,0,0" -> NO slash.
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__mB", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "jh", "mD", "mE", "mF") // ADR-033
	require.NoError(t, gvSetJob(f, f.ctx, "jh", types.Job{
		JobId: "jh", State: "open+paid+optimistic+disputed", MinerId: "mB",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	mBbefore, _ := f.keeper.Miner.Get(f.ctx, "mB")
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jh"})
	require.NoError(t, err)
	gotH, _ := f.keeper.Job.Get(f.ctx, "jh")
	require.Contains(t, gotH.State, "resolved")
	require.Len(t, gotH.SlashRecords, 0, "honest primary -> no slash")
	mBafter, _ := f.keeper.Miner.Get(f.ctx, "mB")
	require.Equal(t, mBbefore.Stake, mBafter.Stake, "honest primary's stake untouched")
}

// FEE HOLD — the PERMISSIONLESS resolution path (AdjudicateDispute) must ALSO consume the held amounts
// (HeldFee/HeldBurn), otherwise the coins are FROZEN forever.
// Cheater -> the client is refunded out of the held amounts (held + cut + burn) and Demand is reversed;
// vindicated -> the held fee is released to the operator and the burn share is burned. Fixture pattern
// taken from TestADR025OptimisticSlash (3 fresh __redo__ jurors outside the origin).
func TestADR025OptimisticFeeHoldAdjudicate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, _ := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_fh_adj_01__")))

	const bigStake = uint64(1_000_000_000_000)
	const fee = uint64(100000)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	cut := fee * p.ProtocolFeeBps / 10000                   // 15000
	burn := fee * p.FeeBurnBps / 10000                      // 5000
	minerNet := fee - cut - burn                            // 80000
	validators := cut * p.ValidatorRewardBps / 10000        // 7500
	team := cut * p.TeamFeeBps / 10000                      // 3000
	treasury := cut - validators - team                     // 4500
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	// origin = 3 LARGE stakes (stake-weighted selection, as in TestADR025OptimisticSlash); fresh = 3 SMALL ones outside the origin.
	opAcc := sdk.AccAddress([]byte("operator_fh_adj_vind_"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	require.NoError(t, gvSetMiner(f, f.ctx, "mA", types.Miner{MinerId: "mA", Stake: bigStake, Demand: treasury + team})) // cheating primary, case (a)
	require.NoError(t, gvSetMiner(f, f.ctx, "mB", types.Miner{MinerId: "mB", Operator: op, Stake: bigStake}))            // vindicated primary, case (b)
	require.NoError(t, gvSetMiner(f, f.ctx, "mC", types.Miner{MinerId: "mC", Stake: bigStake}))
	for _, id := range []string{"mD", "mE", "mF"} { // fresh committee (outside the origin)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	// (a) CHEATING: mA "1,0,0" against a fresh committee at "0,1,0" -> slash, and the client is refunded FROM the held amounts (not frozen).
	clientAcc := sdk.AccAddress([]byte("client_fh_adj_cheat_"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__mA", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jc", "mD", "mE", "mF") // ADR-033
	require.NoError(t, gvSetJob(f, f.ctx, "jc", types.Job{
		JobId: "jc", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jc", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jc", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jc"})
	require.NoError(t, err)
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "cheater -> the client is refunded the WHOLE fee (held + cut + burn), nothing frozen")
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, uint64(0), mA.Demand, "the cheater's Demand is reversed")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jc")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jc")
	require.Error(t, e1, "HeldFee consumed (not frozen)")
	require.Error(t, e2, "HeldBurn consumed (not frozen)")

	// (b) VINDICATED: mB "1,0,0" confirmed as "1,0,0" -> the held fee is RELEASED to the operator, the burn share is BURNED (not paid out).
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__mB", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__"+id, types.Commit{ResultCommit: "1,0,0"}))
	}
	redoAnchor(f, t, "jh", "mD", "mE", "mF") // ADR-033
	require.NoError(t, gvSetJob(f, f.ctx, "jh", types.Job{
		JobId: "jh", State: "open+paid+optimistic+disputed", MinerId: "mB", Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jh", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jh", burn))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jh"})
	require.NoError(t, err)
	require.Equal(t, int64(minerNet), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "vindicated -> minerNet released to the operator (burn share burned, NOT paid out)")
	_, e3 := f.keeper.HeldFee.Get(f.ctx, "jh")
	_, e4 := f.keeper.HeldBurn.Get(f.ctx, "jh")
	require.Error(t, e3, "HeldFee consumed")
	require.Error(t, e4, "HeldBurn burned for good")
}

// CROSSING an appeal with AdjudicateDispute. A job DEFERRED to appeal (scheduled in
// PendingAppealResolve) can be resolved EARLIER by the permissionless AdjudicateDispute during the
// window. The second deadline (runAppealResolveTimeout) must then NEITHER re-resolve NOR re-consume
// the held amounts (no double refund) — the shared `jobIsResolved` guard must stop it. Fixture pattern
// taken from TestADR025OptimisticFeeHoldAdjudicate.
func TestADR028AppealCrossAdjudicate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, _ := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_cross_appe")))

	const bigStake = uint64(1_000_000_000_000)
	const fee = uint64(100000)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.DisputeWindow = 10
	p.AppealWindow = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	cut := fee * p.ProtocolFeeBps / 10000
	burn := fee * p.FeeBurnBps / 10000
	minerNet := fee - cut - burn
	validators := cut * p.ValidatorRewardBps / 10000
	team := cut * p.TeamFeeBps / 10000
	treasury := cut - validators - team
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	require.NoError(t, gvSetMiner(f, f.ctx, "mA", types.Miner{MinerId: "mA", Stake: bigStake, Demand: treasury + team}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mB", types.Miner{MinerId: "mB", Stake: bigStake}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mC", types.Miner{MinerId: "mC", Stake: bigStake}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	clientAcc := sdk.AccAddress([]byte("client_cross_appeal"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jX__mA", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jX__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jX", "mD", "mE", "mF") // ADR-033
	require.NoError(t, gvSetJob(f, f.ctx, "jX", types.Job{
		JobId: "jX", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jX", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jX", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	// the job is ALSO scheduled for a second, appeal deadline (as if it had been deferred at the first timeout):
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(int64(30), "jX")))

	// (a) AdjudicateDispute resolves DURING the window -> cheater slashed, client refunded FROM the held amounts, once.
	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jX"})
	require.NoError(t, err)
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client refunded ONCE (from the held amounts)")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jX")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jX")
	require.Error(t, e1, "HeldFee consumed")
	require.Error(t, e2, "HeldBurn consumed")
	jobR, _ := f.keeper.Job.Get(f.ctx, "jX")
	require.Contains(t, jobR.State, "resolved")

	// (b) Second deadline (EndBlock h=30): runAppealResolveTimeout finds PendingAppealResolve[30,jX] BUT jX is
	// already +resolved -> the jobIsResolved guard -> SKIP. NO double refund, NO second consumption.
	clientBefore := f.bank.balOf(clientAcc).AmountOf("udndr").Int64()
	ctx30 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(30)
	require.NoError(t, f.keeper.EndBlock(ctx30))
	require.Equal(t, clientBefore, f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "NO double refund at the second deadline (jobIsResolved guard)")
}
