package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/types"
)

// ADR-028 — ANTI-ÉVASION. Le primaire optimiste est PAYÉ d'abord ; à l'échéance d'audit il ne garde JAMAIS un
// paiement non vérifié. Timeout (EndBlock) : triche AU PLANCHER de participation (⌈AuditCommitteeSize/2⌉+1 votants)
// -> slash DUR + clawback ; valide -> vindiqué ; sinon -> clawback LÉGER restituable. Le primaire MUET est capté
// en quorum-triche (judge_worker poste "0" sur révélation manquante). Le PLANCHER SYMÉTRIQUE empêche 2 juges/sybils
// « majorité des présents » de slasher un honnête OU de vindiquer un tricheur (F1). AuditCommitteeSize=5 (DÉCOUPLÉ de
// CommitteeSize=3) -> plancher = ⌈5/2⌉+1 = 4.

const aeStake = uint64(1_000_000_000_000)

func aeCtx6(f *fixture) sdk.Context {
	return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
}

func aeFundModule(f *fixture) {
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
}

func aeReg(f *fixture, t *testing.T, id string, stake uint64) {
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: stake}))
}

func aeVerdict(f *fixture, t *testing.T, jobId, judge, v string) {
	require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__verdict__"+judge, types.Commit{ResultCommit: v}))
}

// aeAnchor — ADR-032 : ANCRE le comité d'audit habilité à voter sur `jobId`.
//
// Sans cet ancrage, le tally est FAIL-CLOSED (aucun slash dur possible). C'est volontaire : avant
// ADR-032, n'importe quel mineur enregistré pouvait voter, et 4 identités à `min_stake` suffisaient à
// faire slasher 80 % du stake d'un HONNÊTE. Un test qui veut observer un slash légitime doit donc
// convoquer explicitement ses juges — comme la chaîne le fait au tirage.
func aeAnchor(f *fixture, t *testing.T, jobId string, members ...string) {
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId, strings.Join(members, ",")))
}

// (1) TRICHE AU PLANCHER (4 juges CONVOQUÉS "0") -> SLASH DUR 80 % + clawback. C'est le sort d'un primaire
// muet/garbage (le comité poste "0"). Déclenché par le TIMEOUT, sans appel à AdjudicateDispute. (plancher = 4 à N=5)
//
// ⛔ RÉÉCRIT (ADR-032). Dans sa forme précédente, ce test enregistrait 4 juges à `stake=1` SANS
// aucune convocation et EXIGEAIT qu'ils slashent le primaire de 80 %. Il décrivait donc, ligne pour ligne,
// l'attaque par identités qui a été trouvée en audit — et il était VERT à chaque run : il ne testait pas la
// sécurité, il **certifiait la faille**. Ce que le test doit garantir, c'est qu'un slash légitime part
// toujours **quand le comité ANCRÉ vote** ; le refus des non-convoqués est couvert par le test d'attaque.
func TestADR028CheatQuorumHardSlashAtTimeout(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1) // 4 juges -> plancher de participation atteint (AuditCommitteeSize=5 -> plancher 4)
	aeAnchor(f, t, "jC", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : ils sont CONVOQUÉS -> leurs verdicts comptent
	aeVerdict(f, t, "jC", "mJ1", "0")
	aeVerdict(f, t, "jC", "mJ2", "0")
	aeVerdict(f, t, "jC", "mJ3", "0")
	aeVerdict(f, t, "jC", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028_cheat0"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins()) // pour mesurer le remboursement exact
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jC", types.Job{
		JobId: "jC", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jC")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jC")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved")
	require.Contains(t, job.State, "clawed")
	require.Len(t, job.SlashRecords, 1, "slash enregistré (restituable)")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "slash DUR 80 %")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix du job")
}

// (2) PAS DE QUORUM (comité absent, 0 verdict) -> CLAWBACK LÉGER : reprend SEULEMENT le prix du job, restituable.
func TestADR028NoQuorumLightClawback(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	clientAcc := sdk.AccAddress([]byte("client_adr028_noquor"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jN", types.Job{
		JobId: "jN", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jN")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jN")
	require.Contains(t, job.State, "resolved")
	require.Contains(t, job.State, "clawed")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake, "clawback LÉGER = prix du job seulement, PAS 80 %")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé")
}

// (3) QUORUM=VALIDE (4 juges "1" AU PLANCHER de vindication) -> vindiqué, AUCUN slash ni clawback, stake intact.
// La vindication exige le MÊME plancher que le slash (symétrie F1) : 4 juges à N=5.
func TestADR028ValidQuorumVindicated(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1) // 4 juges "1" -> plancher de vindication atteint (symétrie F1)
	aeAnchor(f, t, "jV", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jV", "mJ1", "1")
	aeVerdict(f, t, "jV", "mJ2", "1")
	aeVerdict(f, t, "jV", "mJ3", "1")
	aeVerdict(f, t, "jV", "mJ4", "1")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jV", types.Job{
		JobId: "jV", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jV")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jV")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed", "verdict valide -> pas de clawback")
	require.Len(t, job.SlashRecords, 0)
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "primaire honnête : stake intact")
}

// (4) GRIEF 2-SYBILS / SOUS LE PLANCHER : 2 juges "0" (< plancher 4) ne déclenchent PAS le slash dur ->
// clawback léger seulement. C'EST la protection demandée par le internal audit (2 « majorité des présents » ≠ slash).
func TestADR028BelowFloorNoHardSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jG", "mS1", "mS2") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jG", "mS1", "0") // 2 « sybils » votent invalide, mais < plancher de participation (4)
	aeVerdict(f, t, "jG", "mS2", "0")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jG", types.Job{
		JobId: "jG", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jG")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake, "2 juges < plancher 4 -> clawback léger, JAMAIS slash dur 80 %")
}

// (4b) F1 FERMÉ — DUAL DU GRIEF : 2 « sybils » votent "1" (valide) pour VINDIQUER un primaire, mais < plancher 4 ->
// PAS de vindication. La branche default (clawback léger) s'applique : le primaire NE GARDE PAS un paiement non
// vérifié (prix repris du stake + remboursé). Avant F1, 2 sièges = majorité du comité -> ils vindiquaient et le
// tricheur conservait son paiement (dual exact de l'asymétrie). Le plancher SYMÉTRIQUE empêche les deux dérives.
func TestADR028BelowFloorNoVindication(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jF", "mS1", "mS2") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jF", "mS1", "1") // 2 « sybils » votent VALIDE pour vindiquer, mais < plancher de participation (4)
	aeVerdict(f, t, "jF", "mS2", "1")
	clientAcc := sdk.AccAddress([]byte("client_adr028_f1clos"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jF", types.Job{
		JobId: "jF", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jF")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jF")
	require.Contains(t, job.State, "resolved")
	require.Contains(t, job.State, "clawed", "2 juges < plancher -> PAS de vindication, clawback léger (F1 fermé)")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake, "2 sybils < plancher 4 -> ne vindiquent PAS : prix repris (le tricheur ne garde rien)")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix")
}

// (5) Mode non-optimiste (dispute humaine) : comportement INCHANGÉ (innocence par défaut, pas de clawback).
func TestADR028NonOptimisticUnchanged(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	aeReg(f, t, "mP", aeStake)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jH", types.Job{
		JobId: "jH", State: "open+disputed", MinerId: "mP", // pas de marqueur "optimistic" -> dispute humaine
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jH")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jH")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed", "dispute non-optimiste -> innocence par défaut inchangée")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "aucun slash sur une dispute humaine non honorée")
}

// (6) Params.Validate() — bornes croisées en mode optimiste (le slash doit pouvoir aboutir).
func TestADR028ParamsValidateCrossChecks(t *testing.T) {
	base := types.DefaultParams()
	base.VerificationMode = 1
	base.AuditSampleBps = 1000
	base.DisputeWindow = 5
	base.AuditResolveTimeout = 10
	require.NoError(t, base.Validate(), "config mode1 cohérente")

	b1 := base
	b1.AuditResolveTimeout = 5 // == window
	require.Error(t, b1.Validate(), "timeout <= dispute_window doit être rejeté")

	b2 := base
	b2.AuditSampleBps = 0
	require.Error(t, b2.Validate(), "audit_sample_bps=0 en mode1 rejeté")

	b3 := base
	b3.DisputeWindow = 0
	require.Error(t, b3.Validate(), "dispute_window=0 en mode1 rejeté")

	off := types.DefaultParams() // mode 0 : aucune contrainte croisée (dormant)
	off.VerificationMode = 0
	off.AuditResolveTimeout = 0
	off.DisputeWindow = 0
	require.NoError(t, off.Validate(), "mode 0 inchangé")
}

// (7) FEE-HOLD v2 (décision internal audit 2026-06-21 (1)+(ii)) — au PLEIN-HOLD, un clawback (sous-plancher) rembourse le
// client depuis TOUTE la fee retenue : rétention minerNet (HeldFee) + cut REVERSÉ de Pools + burn DIFFÉRÉ RENDU.
// => client récupère la fee ENTIÈRE, BOND INTACT (« jamais le bond » STRICT), Demand du job reversé, burn PAS brûlé.
// Reproduit l'état que settleOptimistic pose en plein-hold (HeldFee=minerNet, HeldBurn=burn, cut dans Pools).
func TestADR028FeeHoldClawbackBondIntact(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	cut := fee * p.ProtocolFeeBps / 10000            // 15000
	burn := fee * p.FeeBurnBps / 10000               // 5000
	minerNet := fee - cut - burn                     // 80000
	validators := cut * p.ValidatorRewardBps / 10000 // 7500
	team := cut * p.TeamFeeBps / 10000               // 3000
	treasury := cut - validators - team              // 4500

	// primaire avec le Demand crédité au settle (treasury+team) ; 2 sybils < plancher -> clawbackPayment.
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Demand: treasury + team}))
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jHold", "mS1", "mS2") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jHold", "mS1", "0")
	aeVerdict(f, t, "jHold", "mS2", "0")
	clientAcc := sdk.AccAddress([]byte("client_adr028_feehld"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jHold", types.Job{
		JobId: "jHold", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jHold", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jHold", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jHold")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "BOND INTACT : retenu (minerNet+cut+burn)=fee -> bond pas touché (décision C)")
	require.Equal(t, uint64(0), mP.Demand, "Demand du job reversé")
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé fee ENTIÈRE (minerNet+cut+burn)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Validators+pools.Team+pools.Treasury, "cut reversé de Pools au client")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jHold")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jHold")
	require.Error(t, e1, "rétention minerNet consommée")
	require.Error(t, e2, "burn différé consommé (rendu au client, PAS brûlé)")
}

// (8) FEE-HOLD v2 — VINDIQUÉ : la fee retenue est LIBÉRÉE à l'opérateur du primaire (paiement optimiste confirmé).
func TestADR028FeeHoldReleasedOnVindication(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_adr028hold0"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)
	aeAnchor(f, t, "jRel", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jRel", "mJ1", "1")
	aeVerdict(f, t, "jRel", "mJ2", "1")
	aeVerdict(f, t, "jRel", "mJ3", "1")
	aeVerdict(f, t, "jRel", "mJ4", "1")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jRel", types.Job{
		JobId: "jRel", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jRel", uint64(80000))) // rétention minerNet
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jRel", uint64(5000))) // burn DIFFÉRÉ (brûlé à finalité)
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jRel")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jRel")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed")
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "rétention minerNet LIBÉRÉE à l'opérateur (le burn 5000 est BRÛLÉ à finalité, PAS versé)")
	_, hErr := f.keeper.HeldFee.Get(f.ctx, "jRel")
	require.Error(t, hErr, "rétention consommée")
	_, bErr := f.keeper.HeldBurn.Get(f.ctx, "jRel")
	require.Error(t, bErr, "burn différé brûlé à finalité (HeldBurn retiré)")
}

// (9) FEE-HOLD v2 PARTIE B — APPEL RÉUSSI : primaire muet au 1er timeout (pas de quorum) -> DÉFÉRÉ (aucun clawback,
// rétention GARDÉE) + 2e échéance ; révélation TARDIVE -> comité frais VALIDE -> vindiqué à l'échéance d'appel
// (rétention libérée au primaire honnête-hors-ligne, burn brûlé). C'est la protection de l'honnête déconnecté.
func TestADR028AppealLateRevealVindicated(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.AuditSampleBps = 1000
	p.DisputeWindow = 3
	p.AuditResolveTimeout = 10
	p.AppealWindow = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	burn := fee * p.FeeBurnBps / 10000                 // 5000
	minerNet := fee - fee*p.ProtocolFeeBps/10000 - burn // 80000
	opAcc := sdk.AccAddress([]byte("operator_appeal_vind"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jAp", types.Job{
		JobId: "jAp", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jAp", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jAp", burn))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jAp")))

	// 1er timeout (h=6) : aucun verdict -> DÉFÉRÉ (job inchangé, rétention gardée, rien versé)
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
	job, _ := f.keeper.Job.Get(f.ctx, "jAp")
	require.NotContains(t, job.State, "resolved", "1er timeout -> DÉFÉRÉ, PAS résolu")
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "rien versé au 1er timeout")
	_, stillHeld := f.keeper.HeldFee.Get(f.ctx, "jAp")
	require.NoError(t, stillHeld, "rétention TOUJOURS là après déférement")

	// révélation tardive : le comité frais poste VALIDE (4 juges = plancher à AuditCommitteeSize=5)
	aeAnchor(f, t, "jAp", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jAp", j, "1")
	}
	// 2e échéance (h=11=6+appeal_window) : appel -> re-tally VALIDE -> vindiqué
	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("ah11")})
	require.NoError(t, f.keeper.EndBlock(ctx11))
	job2, _ := f.keeper.Job.Get(f.ctx, "jAp")
	require.Contains(t, job2.State, "resolved", "échéance d'appel -> résolu")
	require.NotContains(t, job2.State, "clawed", "révélation tardive vindiquée -> PAS de clawback")
	require.Equal(t, int64(minerNet), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "appel réussi -> rétention libérée à l'honnête-hors-ligne")
	_, e := f.keeper.HeldBurn.Get(f.ctx, "jAp")
	require.Error(t, e, "burn brûlé à finalité")
}

// (10) FEE-HOLD v2 PARTIE B — APPEL EXPIRÉ : le primaire reste muet (aucune révélation tardive) -> à l'échéance
// d'appel, clawback FINAL (client remboursé depuis la rétention, bond intact). Liveness : pas de gel.
func TestADR028AppealExpiryClawback(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.AuditSampleBps = 1000
	p.DisputeWindow = 3
	p.AuditResolveTimeout = 10
	p.AppealWindow = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	cut := fee * p.ProtocolFeeBps / 10000
	burn := fee * p.FeeBurnBps / 10000
	minerNet := fee - cut - burn
	validators := cut * p.ValidatorRewardBps / 10000
	team := cut * p.TeamFeeBps / 10000
	treasury := cut - validators - team
	clientAcc := sdk.AccAddress([]byte("client_appeal_expiry"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Demand: treasury + team}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jEx", types.Job{
		JobId: "jEx", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jEx", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jEx", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jEx")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f))) // 1er timeout -> déféré
	jobD, _ := f.keeper.Job.Get(f.ctx, "jEx")
	require.NotContains(t, jobD.State, "resolved", "déféré")

	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("ah11")})
	require.NoError(t, f.keeper.EndBlock(ctx11)) // appel expiré -> clawback FINAL
	job2, _ := f.keeper.Job.Get(f.ctx, "jEx")
	require.Contains(t, job2.State, "resolved")
	require.Contains(t, job2.State, "clawed")
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé fee ENTIÈRE à l'expiration de l'appel")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "bond INTACT (remboursement depuis la rétention, plein-hold)")
}
