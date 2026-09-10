package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Dispute primitive: on a SETTLED job with dispute_window>0, the disputer escrows the bond and the state
// becomes "...+disputed"; feature OFF, unsettled job, or re-dispute -> refused.
func TestDisputeVerdict(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_addr_0001__")))
	require.NoError(t, err)

	// SETTLED job
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", State: "settled", Fee: 5000}))

	// (a) feature OFF (dispute_window=0, the default) -> refused
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// Enable disputes and the bond.
	p := types.DefaultParams()
	p.DisputeWindow = 10
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// (b) UNSETTLED job -> refused
	require.NoError(t, gvSetJob(f, f.ctx, "jopen", types.Job{JobId: "jopen", State: "open"}))
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jopen"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (c) valid dispute -> OK, bond escrowed on the module, state "disputed"
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.NoError(t, err)
	got, err := f.keeper.Job.Get(f.ctx, "j1")
	require.NoError(t, err)
	require.Contains(t, got.State, "disputed")
	require.Equal(t, disp, got.Disputer)
	require.Equal(t, uint64(1000), got.DisputeBond)
	require.Equal(t, "1000", f.bank.mod[types.ModuleName].AmountOf("udndr").String())

	// (d) re-dispute -> refused (anti-replay)
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}

// ResolveDispute (authority path): upheld -> refund and reward; rejected -> bond goes to the Treasury;
// non-authority, undisputed job, or re-resolution -> refused.
func TestResolveDispute(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0002__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// DISPUTED job (bond=1000), Treasury at 5000, module funded (simulates real stakes and escrows).
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", State: "settled+disputed", Disputer: disp, DisputeBond: 1000}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000)))

	// non-authority -> refused
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: disp, JobId: "j1", Upheld: true})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// authority + upheld -> the disputer receives bond+reward (1000+1000); Treasury 5000 -> 4000; state "resolved"
	balBefore := f.bank.balOf(dispBytes).AmountOf("udndr")
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j1", Upheld: true})
	require.NoError(t, err)
	got, _ := f.keeper.Job.Get(f.ctx, "j1")
	require.Contains(t, got.State, "resolved")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(4000), pools.Treasury)
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String())

	// re-resolution -> refused (anti-replay)
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j1", Upheld: true})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// UNDISPUTED job -> refused
	require.NoError(t, gvSetJob(f, f.ctx, "j2", types.Job{JobId: "j2", State: "settled"}))
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j2", Upheld: false})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// rejected (upheld=false) -> bond -> Treasury
	require.NoError(t, gvSetJob(f, f.ctx, "j3", types.Job{JobId: "j3", State: "settled+disputed", Disputer: disp, DisputeBond: 700}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 100}))
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j3", Upheld: false})
	require.NoError(t, err)
	pools, _ = f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(800), pools.Treasury)
}

// RESTITUTION: a dispute judged VALID (upheld) REVERSES the slash recorded at settlement (it re-credits the
// wronged miner's stake) IN ADDITION to refunding and rewarding the disputer.
func TestResolveDisputeReversesSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0003__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// Miner slashed at settlement: post-slash stake = 500 (it lost 300).
	require.NoError(t, gvSetMiner(f, f.ctx, "m1", types.Miner{MinerId: "m1", Stake: 500}))
	// DISPUTED job carrying the proof of the slash (m1 -300) and a bond of 1000.
	require.NoError(t, gvSetJob(f, f.ctx, "jd", types.Job{
		JobId: "jd", State: "settled+disputed", Disputer: disp, DisputeBond: 1000,
		SlashRecords: []types.SlashRecord{{MinerId: "m1", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000)))

	balBefore := f.bank.balOf(dispBytes).AmountOf("udndr")
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jd", Upheld: true})
	require.NoError(t, err)

	// Stake RESTORED: 500 + 300 = 800.
	m, _ := f.keeper.Miner.Get(f.ctx, "m1")
	require.Equal(t, uint64(800), m.Stake)
	// Treasury: 5000 - 300 (restitution) - 1000 (reward) = 3700.
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(3700), pools.Treasury)
	// Disputer: bond + reward = 1000 + 1000 = 2000.
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String())
}

// PERMISSIONLESS AdjudicateDispute: a FRESH committee (__redo__ re-commits, stake-weighted, outside the
// original committee) decides WITHOUT governance. The original committee is made deterministic through stake
// (3 large / 3 small).
func TestAdjudicateDispute(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0004__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// 6 miners: 3 with a LARGE stake form the DETERMINISTIC original committee (score = hash/stake, so a large
	// stake takes precedence); 3 with a SMALL stake are the re-committers (hence outside the original committee).
	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	p := types.DefaultParams()
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) window NOT elapsed (DisputeHeight=100, 20 < 110) -> refused
	require.NoError(t, gvSetJob(f, f.ctx, "jw", types.Job{JobId: "jw", State: "settled+disputed", DisputeHeight: 100}))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jw"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (b) UPHELD: mC (original committee) was slashed wrongly; the FRESH committee (mD, mE, mF) confirms ITS answer "1,0,0".
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mA", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mB", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mC", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "ju", "mD", "mE", "mF") // ADR-033: fresh committee DRAWN and anchored
	require.NoError(t, gvSetJob(f, f.ctx, "ju", types.Job{
		JobId: "ju", State: "settled+disputed", Disputer: disp, DisputeBond: 1000, DisputeHeight: 1,
		SlashRecords: []types.SlashRecord{{MinerId: "mC", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))

	balBefore := f.bank.balOf(dispBytes).AmountOf("udndr")
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "ju"})
	require.NoError(t, err)
	gotJob, _ := f.keeper.Job.Get(f.ctx, "ju")
	require.Contains(t, gotJob.State, "resolved")
	mC, _ := f.keeper.Miner.Get(f.ctx, "mC")
	require.Equal(t, bigStake+300, mC.Stake) // stake RESTORED
	poolsU, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(3700), poolsU.Treasury) // 5000 -300 (restitution) -1000 (reward)
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String()) // bond+reward

	// (c) anti-replay -> refused
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "ju"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (d) REJECTED: the fresh committee CONFIRMS the slash (mC's original "0,1,0" does not match the fresh majority "1,0,0").
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__mC", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "jr", "mD", "mE", "mF") // ADR-033
	require.NoError(t, gvSetJob(f, f.ctx, "jr", types.Job{
		JobId: "jr", State: "settled+disputed", Disputer: disp, DisputeBond: 700, DisputeHeight: 1,
		SlashRecords: []types.SlashRecord{{MinerId: "mC", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 100}))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jr"})
	require.NoError(t, err)
	poolsR, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(800), poolsR.Treasury) // 100 + 700 (confiscated bond), no restitution
}
