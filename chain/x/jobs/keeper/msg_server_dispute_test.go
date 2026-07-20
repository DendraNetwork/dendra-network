package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// INT-1 v0 (inc.1) — primitive de dispute : sur un job REGLE et avec dispute_window>0, le disputeur escrowe
// le bond et l'état devient "...+disputed" ; feature OFF / job non réglé / re-dispute -> refus.
func TestDisputeVerdict(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_addr_0001__")))
	require.NoError(t, err)

	// job REGLE
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", State: "settled", Fee: 5000}))

	// (a) feature OFF (dispute_window=0, défaut) -> refus
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// active les disputes + bond
	p := types.DefaultParams()
	p.DisputeWindow = 10
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// (b) job NON réglé -> refus
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jopen", types.Job{JobId: "jopen", State: "open"}))
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jopen"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (c) dispute valide -> OK + bond escrowé sur le module + état "disputed"
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.NoError(t, err)
	got, err := f.keeper.Job.Get(f.ctx, "j1")
	require.NoError(t, err)
	require.Contains(t, got.State, "disputed")
	require.Equal(t, disp, got.Disputer)
	require.Equal(t, uint64(1000), got.DisputeBond)
	require.Equal(t, "1000", f.bank.mod[types.ModuleName].AmountOf("udndr").String())

	// (d) re-dispute -> refus (anti-rejeu)
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}

// INT-1 v0 (inc.2) — ResolveDispute (autorité) : upheld -> rembourse+récompense ; rejet -> bond->Trésorerie ;
// non-autorité / job non disputé / re-résolution -> refus.
func TestResolveDispute(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0002__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// job DISPUTÉ (bond=1000) + Trésorerie 5000 + module financé (simule stakes/escrows réels)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", State: "settled+disputed", Disputer: disp, DisputeBond: 1000}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000)))

	// non-autorité -> refus
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: disp, JobId: "j1", Upheld: true})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// autorité + upheld -> disputeur reçoit bond+reward (1000+1000) ; Trésorerie 5000 -> 4000 ; état "resolved"
	balBefore := f.bank.balOf(dispBytes).AmountOf("udndr")
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j1", Upheld: true})
	require.NoError(t, err)
	got, _ := f.keeper.Job.Get(f.ctx, "j1")
	require.Contains(t, got.State, "resolved")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(4000), pools.Treasury)
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String())

	// re-résolution -> refus (anti-rejeu)
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j1", Upheld: true})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// job NON disputé -> refus
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j2", types.Job{JobId: "j2", State: "settled"}))
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j2", Upheld: false})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// rejet (upheld=false) -> bond -> Trésorerie
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j3", types.Job{JobId: "j3", State: "settled+disputed", Disputer: disp, DisputeBond: 700}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 100}))
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "j3", Upheld: false})
	require.NoError(t, err)
	pools, _ = f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(800), pools.Treasury)
}

// INT-1 v0 (inc.3) — RESTITUTION : une dispute jugée VALIDE (upheld) INVERSE le slash enregistré au
// règlement (re-crédite le stake du mineur lésé) EN PLUS de rembourser+récompenser le disputeur.
func TestResolveDisputeReversesSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0003__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// mineur slashé au règlement : stake post-slash = 500 (il avait perdu 300)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m1", types.Miner{MinerId: "m1", Stake: 500}))
	// job DISPUTÉ portant la preuve du slash (m1 -300) + bond 1000
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jd", types.Job{
		JobId: "jd", State: "settled+disputed", Disputer: disp, DisputeBond: 1000,
		SlashRecords: []types.SlashRecord{{MinerId: "m1", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000)))

	balBefore := f.bank.balOf(dispBytes).AmountOf("udndr")
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jd", Upheld: true})
	require.NoError(t, err)

	// stake RESTAURÉ : 500 + 300 = 800
	m, _ := f.keeper.Miner.Get(f.ctx, "m1")
	require.Equal(t, uint64(800), m.Stake)
	// Trésorerie : 5000 - 300 (restitution) - 1000 (reward) = 3700
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(3700), pools.Treasury)
	// disputeur : bond + reward = 1000 + 1000 = 2000
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String())
}

// INT-1 v0 (inc.4) — AdjudicateDispute PERMISSIONLESS : un comité FRAIS (re-commits __redo__, pondéré stake,
// hors comité d'origine) tranche SANS gouvernance. Comité d'origine déterministe via le stake (3 gros / 3 petits).
func TestAdjudicateDispute(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	dispBytes := sdk.AccAddress([]byte("disputer_addr_0004__"))
	disp, err := f.addressCodec.BytesToString(dispBytes)
	require.NoError(t, err)

	// 6 mineurs : 3 GROS stake = comité d'origine DÉTERMINISTE (score = hash/stake → gros stake = prioritaire) ;
	// 3 PETIT stake = re-committers (donc hors comité d'origine).
	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	p := types.DefaultParams()
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // au-delà de la fenêtre
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) fenêtre NON écoulée (DisputeHeight=100, 20 < 110) -> refus
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jw", types.Job{JobId: "jw", State: "settled+disputed", DisputeHeight: 100}))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jw"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (b) UPHELD : mC (comité d'origine) slashé à tort ; le comité FRAIS (mD,mE,mF) confirme SA réponse "1,0,0".
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mA", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mB", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__mC", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "ju__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "ju", "mD", "mE", "mF") // ADR-033 : comité frais TIRÉ et ancré
	require.NoError(t, f.keeper.Job.Set(f.ctx, "ju", types.Job{
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
	require.Equal(t, bigStake+300, mC.Stake) // stake RESTAURÉ
	poolsU, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(3700), poolsU.Treasury) // 5000 -300 (restit.) -1000 (reward)
	balAfter := f.bank.balOf(dispBytes).AmountOf("udndr")
	require.Equal(t, "2000", balAfter.Sub(balBefore).String()) // bond+reward

	// (c) anti-rejeu -> refus
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "ju"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (d) REJET : le comité frais CONFIRME le slash (mC original "0,1,0" ne matche pas la majorité fraîche "1,0,0").
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__mC", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jr__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "jr", "mD", "mE", "mF") // ADR-033
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jr", types.Job{
		JobId: "jr", State: "settled+disputed", Disputer: disp, DisputeBond: 700, DisputeHeight: 1,
		SlashRecords: []types.SlashRecord{{MinerId: "mC", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 100}))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jr"})
	require.NoError(t, err)
	poolsR, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(800), poolsR.Treasury) // 100 + 700 (bond slashé), aucune restitution
}
