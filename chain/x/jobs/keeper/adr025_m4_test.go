package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// M4 (ADR-025) — RÉSOLUTION D'AUDIT OPTIMISTE via AdjudicateDispute étendu : un primaire optimiste PAYÉ
// dont le commit ORIGINAL diverge de la majorité de stake du comité frais est SLASHÉ dur ; s'il concorde,
// il est vindiqué (rien). Montage = patron TestAdjudicateDispute (3 gros stake = comité d'origine dont le
// primaire ; 3 petits = comité frais __redo__, hors origine).
func TestADR025OptimisticSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_addr_m4_01_")))
	require.NoError(t, err)

	const bigStake = uint64(1_000_000_000_000)
	for _, id := range []string{"mA", "mB", "mC"} { // comité d'origine (gros stake)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: bigStake}))
	}
	for _, id := range []string{"mD", "mE", "mF"} { // comité frais (petit stake, hors origine)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	p := types.DefaultParams() // slash_leak_bps = 8000 (80 %)
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // au-delà de la fenêtre
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(100000)))

	// (a) TRICHE : primaire mA a committé "1,0,0" ; le comité frais (mD,mE,mF) dit "0,1,0" -> SLASH 80 %.
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__mA", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mD", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mE", types.Commit{ResultCommit: "0,1,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__mF", types.Commit{ResultCommit: "0,1,0"}))
	redoAnchor(f, t, "jc", "mD", "mE", "mF") // ADR-033 : le comité frais doit être TIRÉ (ici : convoqué explicitement)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jc", types.Job{
		JobId: "jc", State: "open+paid+optimistic+disputed", MinerId: "mA",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jc"})
	require.NoError(t, err)
	gotJob, _ := f.keeper.Job.Get(f.ctx, "jc")
	require.Contains(t, gotJob.State, "resolved")
	require.Len(t, gotJob.SlashRecords, 1, "slash du primaire enregistré")
	require.Equal(t, "mA", gotJob.SlashRecords[0].MinerId)
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, bigStake-bigStake*8000/10000, mA.Stake, "primaire slashé 80 %")
	poolsC, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, bigStake*8000/10000, poolsC.Treasury, "montant slashé -> Trésorerie")

	// (b) HONNÊTE : primaire mB a committé "1,0,0" ; le comité frais confirme "1,0,0" -> AUCUN slash.
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__mB", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mD", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mE", types.Commit{ResultCommit: "1,0,0"}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__mF", types.Commit{ResultCommit: "1,0,0"}))
	redoAnchor(f, t, "jh", "mD", "mE", "mF") // ADR-033
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jh", types.Job{
		JobId: "jh", State: "open+paid+optimistic+disputed", MinerId: "mB",
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	mBbefore, _ := f.keeper.Miner.Get(f.ctx, "mB")
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jh"})
	require.NoError(t, err)
	gotH, _ := f.keeper.Job.Get(f.ctx, "jh")
	require.Contains(t, gotH.State, "resolved")
	require.Len(t, gotH.SlashRecords, 0, "primaire honnête -> pas de slash")
	mBafter, _ := f.keeper.Miner.Get(f.ctx, "mB")
	require.Equal(t, mBbefore.Stake, mBafter.Stake, "stake du primaire honnête intact")
}

// fee-hold v2 (internal audit 2026-06-21) — la RÉSOLUTION PERMISSIONLESS (AdjudicateDispute) doit AUSSI consommer la
// rétention (HeldFee/HeldBurn), sinon les coins sont GELÉS à jamais (défaut trouvé par audit 4-agents 2026-06-21).
// Tricheur -> client remboursé depuis la rétention (held+cut+burn) + Demand reversé ; vindiqué -> rétention libérée
// à l'opérateur + burn brûlé. Patron = TestADR025OptimisticSlash (3 frais __redo__ hors origine).
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
	cut := fee * p.ProtocolFeeBps / 10000            // 15000
	burn := fee * p.FeeBurnBps / 10000               // 5000
	minerNet := fee - cut - burn                     // 80000
	validators := cut * p.ValidatorRewardBps / 10000 // 7500
	team := cut * p.TeamFeeBps / 10000               // 3000
	treasury := cut - validators - team              // 4500
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // au-delà de la fenêtre
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	// origine = 3 GROS stake (sélection pondérée stake, comme TestADR025OptimisticSlash) ; frais = 3 PETITS hors origine.
	opAcc := sdk.AccAddress([]byte("operator_fh_adj_vind_"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mA", types.Miner{MinerId: "mA", Stake: bigStake, Demand: treasury + team})) // primaire tricheur (a)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mB", types.Miner{MinerId: "mB", Operator: op, Stake: bigStake}))            // primaire vindiqué (b)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mC", types.Miner{MinerId: "mC", Stake: bigStake}))
	for _, id := range []string{"mD", "mE", "mF"} { // comité frais (hors origine)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}

	// (a) TRICHE : mA "1,0,0" vs comité frais "0,1,0" -> slash + client remboursé DEPUIS la rétention (pas gelée).
	clientAcc := sdk.AccAddress([]byte("client_fh_adj_cheat_"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__mA", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jc__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jc", "mD", "mE", "mF") // ADR-033
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jc", types.Job{
		JobId: "jc", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jc", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jc", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jc"})
	require.NoError(t, err)
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "tricheur -> client remboursé fee ENTIÈRE (rétention+cut+burn), pas gelée")
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, uint64(0), mA.Demand, "Demand du tricheur reversé")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jc")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jc")
	require.Error(t, e1, "HeldFee consommé (pas gelé)")
	require.Error(t, e2, "HeldBurn consommé (pas gelé)")

	// (b) VINDIQUÉ : mB "1,0,0" confirmé "1,0,0" -> rétention LIBÉRÉE à l'opérateur, burn BRÛLÉ (pas versé).
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__mB", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jh__redo__"+id, types.Commit{ResultCommit: "1,0,0"}))
	}
	redoAnchor(f, t, "jh", "mD", "mE", "mF") // ADR-033
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jh", types.Job{
		JobId: "jh", State: "open+paid+optimistic+disputed", MinerId: "mB", Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jh", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jh", burn))
	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jh"})
	require.NoError(t, err)
	require.Equal(t, int64(minerNet), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "vindiqué -> minerNet libéré à l'opérateur (burn brûlé, PAS versé)")
	_, e3 := f.keeper.HeldFee.Get(f.ctx, "jh")
	_, e4 := f.keeper.HeldBurn.Get(f.ctx, "jh")
	require.Error(t, e3, "HeldFee consommé")
	require.Error(t, e4, "HeldBurn brûlé à finalité")
}

// N3 (internal audit 2026-06-22) — CROISEMENT appel ↔ AdjudicateDispute. Un job DÉFÉRÉ en appel (programmé dans
// PendingAppealResolve) peut être résolu PLUS TÔT par AdjudicateDispute (permissionless) pendant la fenêtre.
// La 2e échéance (runAppealResolveTimeout) ne doit alors NI re-résoudre NI re-consommer la rétention (pas de
// double-remboursement) — la garde `jobIsResolved` partagée doit l'en empêcher. Patron = TestADR025OptimisticFeeHoldAdjudicate.
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
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // au-delà de la fenêtre de dispute
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mA", types.Miner{MinerId: "mA", Stake: bigStake, Demand: treasury + team}))
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mB", types.Miner{MinerId: "mB", Stake: bigStake}))
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mC", types.Miner{MinerId: "mC", Stake: bigStake}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	clientAcc := sdk.AccAddress([]byte("client_cross_appeal"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jX__mA", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jX__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jX", "mD", "mE", "mF") // ADR-033
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jX", types.Job{
		JobId: "jX", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jX", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jX", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	// le job est AUSSI programmé pour une 2e échéance d'appel (comme s'il avait été déféré au 1er timeout) :
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(int64(30), "jX")))

	// (a) AdjudicateDispute résout PENDANT la fenêtre -> tricheur slashé + client remboursé DEPUIS la rétention (1 fois).
	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jX"})
	require.NoError(t, err)
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé UNE fois (rétention)")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jX")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jX")
	require.Error(t, e1, "HeldFee consommé")
	require.Error(t, e2, "HeldBurn consommé")
	jobR, _ := f.keeper.Job.Get(f.ctx, "jX")
	require.Contains(t, jobR.State, "resolved")

	// (b) 2e échéance (EndBlock h=30) : runAppealResolveTimeout trouve PendingAppealResolve[30,jX] MAIS jX est déjà
	// +resolved -> garde jobIsResolved -> SKIP. AUCUN double-remboursement, AUCUNE re-consommation.
	clientBefore := f.bank.balOf(clientAcc).AmountOf("udndr").Int64()
	ctx30 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(30)
	require.NoError(t, f.keeper.EndBlock(ctx30))
	require.Equal(t, clientBefore, f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "PAS de double-remboursement à la 2e échéance (garde jobIsResolved)")
}
