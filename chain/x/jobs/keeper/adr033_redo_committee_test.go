package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// ADR-033 — LE COMITÉ DE RE-ADJUDICATION EST ANCRÉ : seuls les tirés re-jugent.
//
// Pourquoi ces tests existent. ADR-032 a authentifié l'ensemble votant du TALLY DE VERDICTS et s'est
// arrêté là. `AdjudicateDispute` gardait un « comité FRAIS » AUTO-DÉSIGNÉ : n'importe quel mineur
// enregistré hors comité d'origine pouvait poster `<jobId>__redo__<id>` et entrer dans le tally, avec
// un gate valant `3 IDENTITÉS` et une majorité calculée sur le stake des seuls auto-sélectionnés.
//
// La propriété générale : *toute fonction qui déplace du capital sur la foi d'un vote doit nommer,
// on-chain, qui avait le droit de voter.* Les deux sens de l'abus sont testés ici, parce que ce
// chemin en avait deux — et que le second (encaisser) est plus grave que le premier.

// redoAnchor — convoque explicitement un comité de re-adjudication (patron `aeAnchor`).
func redoAnchor(f *fixture, t *testing.T, jobId string, members ...string) {
	t.Helper()
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId+"__redo", strings.Join(members, ",")))
}

// adr033Setup — montage commun : 3 gros stake (comité d'origine potentiel), un primaire, un disputeur.
func adr033Setup(t *testing.T, disputerSeed string) (*fixture, types.MsgServer, string) {
	t.Helper()
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(disputerSeed)))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // au-delà de la fenêtre
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	return f, srv, disp
}

const adr033Big = uint64(1_000_000_000_000)

// (A) SENS 1 — LE FAUX SLASH. Trois identités à stake=1, NON tirées, postent des re-commits
// divergents contre un primaire honnête.
// Attendu : leurs re-commits sont ignorés, le corps électoral est vide, et l'adjudication REFUSE de
// trancher plutôt que de condamner. Le stake du primaire est INTACT.
func TestADR033SybilRedoCannotSlashHonest(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_a01")

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mHonest", types.Miner{MinerId: "mHonest", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__mHonest", types.Commit{ResultCommit: "1,0,0"}))
	// L'attaquant s'enregistre 3 fois pour ~rien (le bond est remboursé à l'exit) et « re-exécute ».
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// Un comité EST tiré — mais aucun sybil n'en fait partie.
	redoAnchor(f, t, "jAtk", "mJuror1", "mJuror2", "mJuror3")

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jAtk", types.Job{
		JobId: "jAtk", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jAtk"})
	require.Error(t, err, "aucun juge TIRÉ n'a répondu -> l'adjudication ne tranche pas (fail-closed)")

	m, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Equal(t, adr033Big, m.Stake,
		"des NON-TIRÉS ne doivent JAMAIS faire perdre un satoshi de bond à un honnête")
	job, _ := f.keeper.Job.Get(f.ctx, "jAtk")
	require.NotContains(t, job.State, "resolved", "le job reste ouvert : le timeout d'audit tranchera")
}

// (B) SENS 2 — L'AUTO-VINDICATION, le sens qui RAPPORTE. Un tricheur RÉELLEMENT slashé monte
// 3 sybils qui recopient son commit d'origine. Sous l'ancien code : majorité stricte du « stake
// frais » (100 % à lui) -> restitution du slash DEPUIS LA TRÉSORERIE + bond + récompense au
// disputeur, c'est-à-dire à lui-même. C'était un vol, pas un grief.
// Attendu : rien ne sort de la Trésorerie.
func TestADR033SybilRedoCannotVindicateCheater(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_b01")

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mCheat", types.Miner{MinerId: "mCheat", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jVind__mCheat", types.Commit{ResultCommit: "0,1,0"}))
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		// ils recopient EXACTEMENT le commit du tricheur : « la majorité me donne raison »
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jVind__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jVind", "mJuror1", "mJuror2", "mJuror3") // aucun sybil convoqué

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jVind", types.Job{
		JobId: "jVind", State: "settled+disputed", Disputer: disp, DisputeBond: 1000, DisputeHeight: 1,
		SlashRecords: []types.SlashRecord{{MinerId: "mCheat", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jVind"})
	require.Error(t, err, "corps électoral vide -> pas d'adjudication, donc pas de restitution")

	m, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, adr033Big, m.Stake, "le slash NE doit PAS être annulé par des identités auto-désignées")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(5000), pools.Treasury, "la Trésorerie ne finance pas l'auto-vindication")
}

// (C) FAIL-CLOSED — aucune ancre du tout (dispute ouverte avant l'upgrade, ou tirage impossible).
// Attendu : même avec un comité frais « plausible » (3 mineurs réels, unanimes), rien ne bouge.
func TestADR033NoAnchoredRedoCommitteeIsInert(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_c01")

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__mP", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// PAS de redoAnchor.

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jNo", types.Job{
		JobId: "jNo", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jNo"})
	require.Error(t, err, "sans ancrage : aucune décision, dans aucun sens")
	m, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, adr033Big, m.Stake, "fail-closed : on préfère rater un tricheur que condamner un honnête")
}

// (D) LE SLASH LÉGITIME PART TOUJOURS. La garde doit borner l'abus, pas l'usage : un comité
// RÉELLEMENT tiré, unanime contre un primaire divergent, slashe.
// Sans ce test, (A)/(B)/(C) seraient satisfaits par un code qui ne fait plus rien du tout.
func TestADR033AnchoredRedoCommitteeStillSlashes(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_d01")

	// MONTAGE : 3 GROS stake -> ils forment le comité d'ORIGINE (selectCommittee est pondéré stake),
	// 3 PETITS -> ils sont donc HORS origine et éligibles au comité frais. Avec seulement 4 mineurs,
	// le tirage d'origine happerait les « frais », qui sont exclus du tally — le test échouerait en
	// décrivant une absence de juges, pas la propriété qu'il prétend mesurer.
	for _, id := range []string{"mCheat", "mB", "mC"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__mCheat", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jOk", "mD", "mE", "mF") // CEUX-LÀ ont été tirés

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jOk", types.Job{
		JobId: "jOk", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jOk"})
	require.NoError(t, err)
	m, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	p, _ := f.keeper.Params.Get(f.ctx)
	require.Equal(t, adr033Big-adr033Big*p.SlashLeakBps/10000, m.Stake,
		"comité TIRÉ et unanime -> le slash dur tombe (la garde borne l'abus, pas l'usage)")
}

// (F) LIVENESS — UNE DISPUTE A TOUJOURS UNE ÉCHÉANCE. Le durcissement d'ADR-033 rend possible qu'AUCUN
// juge tiré ne réponde (vivier = N-5, ou juges silencieux). Sans échéance, le job resterait `+disputed`
// pour toujours — et `audit_sampling.go` affirme le contraire. On prouve donc que le timeout tranche.
func TestADR033DisputeAlwaysGetsADeadline(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_f01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jLive", types.Job{
		JobId: "jLive", State: "open+paid", MinerId: "mP", Fee: 100000,
	}))
	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jLive"})
	require.NoError(t, err)

	// Personne ne re-commit, personne ne juge. Le seul recours est l'échéance.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jLive")
	require.Contains(t, job.State, "resolved",
		"une dispute sans juge doit se clore à l'échéance, pas rester ouverte indéfiniment")
}

// (G) COMITÉ CONVOQUÉ MAIS MUET — le bond revient au disputeur, et n'est jamais orphelin.
//
// À distinguer strictement de (H) : ici un comité d'audit A ÉTÉ ancré, donc le disputeur avait une
// raison légitime de contester et ce sont les juges qui n'ont pas répondu. La panne est celle du
// réseau, et l'invariant ① (« ne jamais facturer une panne d'infra à l'honnête ») vaut aussi pour un
// disputeur. Ce que ce chemin ne doit surtout pas faire, c'est laisser le bond au compte de module
// sans ligne comptable disant à qui il revient — le défaut qui fabrique des fonds orphelins.
func TestADR033SilentAnchoredCommitteeRefundsDisputeBond(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_g01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jBond", types.Job{
		JobId: "jBond", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	// LE JOB A ÉTÉ ÉCHANTILLONNÉ : un comité d'audit est ancré. Aucun de ses membres ne votera.
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jBond", "mJ1,mJ2,mJ3,mJ4,mJ5"))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jBond"})
	require.NoError(t, err)
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"le bond est bien escrowé à l'ouverture")

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	require.Equal(t, int64(5000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"comité convoqué mais muet -> bond RENDU (la panne est celle du réseau)")
	job, _ := f.keeper.Job.Get(f.ctx, "jBond")
	require.Equal(t, uint64(0), job.DisputeBond, "bond remis à 0 -> restitution non rejouable")

	// Le comité ancré n'a rien vérifié : ADR-028 reprend alors le PRIX du job (paiement non vérifié).
	// C'est la différence de fond avec (H), où rien n'est repris parce que personne n'a été convoqué.
	m, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, adr033Big-100000, m.Stake,
		"comité convoqué mais muet -> le prix du job est repris (hold_bps=0 ici, donc sur le stake)")
	// COMPOSITION EXACTE de la Trésorerie : le prix repris, et RIEN d'autre. 101000 signifierait que le
	// bond y a été versé en plus — c'est-à-dire qu'on aurait puni le disputeur d'une panne des juges.
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(100000), pools.Treasury,
		"le prix repris uniquement ; le bond n'est PAS en Trésorerie (il est retourné au disputeur)")
}

// (H) LE GRIEF GRATUIT — le risque que crée l'ajout d'une échéance aux disputes humaines.
//
// Leur donner une échéance les route vers `resolveDisputedAudit`, conçu pour les jobs ÉCHANTILLONNÉS. Or son tally lit `AuditCommittee[jobId]`, que seul l'échantillonnage VRF écrit :
// pour un job jamais audité il renvoie STRUCTURELLEMENT 0 votant, donc la branche « paiement non
// vérifié » reprenait la fee d'un mineur honnête déjà payé. N'importe qui pouvait donc, pour le prix du
// gas, faire annuler le paiement de n'importe quel job réglé — et le primaire n'avait aucun moyen de se
// défendre, puisque aucun verdict ne pouvait compter.
//
// Attendu désormais : dispute NON honorée, mineur intact, bond en Trésorerie (anti-grief).
func TestADR033UnfoundedDisputeCannotClawbackHonestPayment(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_h01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	p.HoldBps = 10000
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mHonnete", types.Miner{MinerId: "mHonnete", Stake: adr033Big}))
	// Job REGLE en optimiste, JAMAIS echantillonne : aucun AuditCommittee[jobId] n'existe.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jGrief", types.Job{
		JobId: "jGrief", State: "open+paid+optimistic", MinerId: "mHonnete", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jGrief", 80000))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jGrief"})
	require.NoError(t, err)

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jGrief")
	require.Contains(t, job.State, "resolved", "la dispute doit se clore (liveness)")
	require.NotContains(t, job.State, "clawed",
		"AUCUN clawback : personne n'a ete convoque, donc rien n'a ete prouve contre le primaire")
	m, _ := f.keeper.Miner.Get(f.ctx, "mHonnete")
	require.Equal(t, adr033Big, m.Stake, "le stake de l'honnete ne bouge pas")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(1000), pools.Treasury,
		"bond du disputeur -> Tresorerie : une dispute infondee doit COUTER, sinon le grief est gratuit")
	require.Equal(t, uint64(0), job.DisputeBond, "bond consomme -> ni re-confisque, ni rembourse")
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"le disputeur ne recupere PAS son bond (contrairement au cas du comite convoque mais muet)")
}

// (J) LE PROPOSANT NE CHOISIT PAS LES AUDITS. `ProcessProposal` accepte une proposition SANS
// injection de vote-extensions ; sans injection il n'y a pas de graine décentralisée, et le repli
// vaut `AppHash(H-1):h` — connu du proposant AVANT qu'il propose. Il pouvait donc calculer le tirage
// hors chaîne et n'injecter que lorsqu'il l'arrangeait. Attendu : sans graine décentralisée, AUCUN
// tirage n'a lieu et les jobs dus sont REPORTÉS — omettre l'injection ne déplace plus rien.
func TestADR033NoDecentralizedSeedMeansNoAuditDraw(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 % : SANS la garde, ce job serait tiré à coup sûr
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.CommitteeSeedSource = 1 // VRF armée... mais aucune graine posée à cette hauteur
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jSeedGrind", types.Job{
		JobId: "jSeedGrind", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jSeedGrind")))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jSeedGrind")
	require.NotContains(t, job.State, "disputed",
		"a 100 %% d'echantillonnage le job SERAIT tire : la garde doit l'en empecher tant que la graine est devinable")

	// REPORTÉ, pas perdu : la garde ne doit pas devenir un moyen d'annuler les audits.
	// On lit le pas depuis le code plutôt que de l'écrire en dur : sinon le test mesure le RÉGLAGE
	// (« h+1 ») et non la propriété (« reporté »), et il casse dès qu'on ajuste le pas pour une
	// raison de coût — ce qui est exactement ce qui s'est produit.
	moved, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h+keeper.AuditDeferStrideForTest, "jSeedGrind"))
	require.NoError(t, err)
	require.True(t, moved, "le job doit etre REPORTE, pas abandonne")
	stale, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h, "jSeedGrind"))
	require.NoError(t, err)
	require.False(t, stale, "l'ancienne echeance doit etre retiree (sinon doublon a chaque bloc)")
}

// (K) ON N'ACHÈTE PAS SA SORTIE D'AUDIT. Le vecteur le plus rentable trouvé à ce jour, et il naissait
// de la COMPOSITION de deux corrections : le tirage sautait un job déjà `+disputed` mais retirait
// quand même son échéance, et `DisputeVerdict` n'interdit pas au primaire de contester son propre job.
// Un mineur contestait donc son job pendant la fenêtre d'un bloc qui précède l'audit, l'audit
// n'arrivait jamais, et le job retombait dans la branche « dispute infondée » qui LIBÈRE la rétention.
//
// Le calcul rendait la chose pire à mesure que le mineur grossit : le bond est un coût FIXE, le slash
// est PROPORTIONNEL au stake. Une garde anti-triche dont le prix de contournement ne dépend pas de ce
// qu'on protège est une garde qu'on achète.
func TestADR033DisputingYourOwnJobDoesNotCancelTheAudit(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 % : ce job DOIT être audité
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.DisputeBond = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	// Le mineur est aussi celui qui conteste : rien ne l'interdit.
	mineur, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("le_mineur_lui_meme_")))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mTricheur",
		types.Miner{MinerId: "mTricheur", Stake: adr033Big, Operator: mineur}))
	for _, j := range []string{"mJ1", "mJ2", "mJ3"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, j, types.Miner{MinerId: j, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jEvade", types.Job{
		JobId: "jEvade", State: "open+paid+optimistic", MinerId: "mTricheur", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jEvade")))

	// L'ÉVASION : il conteste son propre job AVANT la hauteur d'audit.
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: mineur, JobId: "jEvade"})
	require.NoError(t, err, "rien n'interdit de contester son propre job — c'est le point de depart")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	// L'audit doit avoir eu lieu MALGRÉ la contestation : un comité ancré le prouve.
	_, anchored := f.keeper.AuditCommittee.Get(f.ctx, "jEvade")
	require.NoError(t, anchored,
		"contester son propre job ne doit PAS annuler l'audit : sinon la garde anti-triche s'achete au prix du bond")

	// Et la dispute humaine ne doit pas avoir ete ecrasee : son bond garderait un proprietaire.
	job, _ := f.keeper.Job.Get(f.ctx, "jEvade")
	require.Equal(t, mineur, job.Disputer,
		"l'audit s'AJOUTE a la dispute humaine, il ne la remplace pas (sinon le bond perd son proprietaire)")
	require.NotContains(t, job.State[len("open+paid+optimistic"):], "disputed+disputed",
		"l'etat ne doit pas porter deux fois le marqueur")
}

// (L) HAUT-1 — UN TIERS NE PEUT PLUS VERROUILLER UN ESCROW. `SettleJob` réglait un job sur la seule
// foi du signataire : n'importe qui posait `+settled`, et tous les vrais chemins refusaient ensuite
// « job deja regle » → escrow gelé, mineur jamais payé. Réservé désormais à l'autorité.
func TestHaut1SettleJobRejectsNonAuthority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	quidam, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("un_quidam_quelconque")))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jLock", types.Job{JobId: "jLock", Fee: 10000, State: "open"}))

	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: quidam, JobId: "jLock"})
	require.Error(t, err, "un tiers ne doit PAS pouvoir regler (donc geler) un job")

	job, _ := f.keeper.Job.Get(f.ctx, "jLock")
	require.NotContains(t, job.State, "settled",
		"l'escrow ne doit pas etre verrouille par un appel non autorise")
}

// (M) HAUT-2 — COMITÉ REDO CONVOQUÉ MAIS MUET : LE BOND EST RENDU, PAS CONFISQUÉ. C'est le cas
// NOMINAL d'ADR-033 : dispute humaine sur un job jamais échantillonné, comité redo ancré à l'ouverture,
// aucun juré ne re-commit. Le timeout lisait la mauvaise ancre (`<jobId>` au lieu de `<jobId>__redo`)
// et facturait la panne des juges au disputeur honnête.
func TestHaut2SilentRedoCommitteeRefundsBondNotConfiscates(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputeur_haut2_01_")))
	require.NoError(t, err)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 5
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	dispBz, _ := f.addressCodec.StringToBytes(disp)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	// un vivier assez grand pour que anchorRedoCommittee tire un comité
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	for _, id := range []string{"mA", "mB", "mC", "mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.SetBlockHash(f.ctx, sdk.UnwrapSDKContext(f.ctx).BlockHeight(), []byte{0x01, 0x02, 0x03, 0x04}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jMute", types.Job{
		JobId: "jMute", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jMute"})
	require.NoError(t, err)
	// le comité redo DOIT avoir été ancré
	_, redoOK := f.keeper.AuditCommittee.Get(f.ctx, "jMute__redo")
	require.NoError(t, redoOK, "DisputeVerdict doit ancrer le comité redo")

	// aucun juré ne re-commit. Échéance.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	require.Equal(t, int64(5000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"comité redo convoqué mais muet -> bond RENDU (la panne est celle du réseau, pas la faute du disputeur)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury,
		"le bond ne doit PAS partir en Tresorerie : ce n'est pas une dispute infondee, un comite A ete convoque")
}

// (N) HAUT-3 — 3 RÉPONDANTS SUR 15 SIÈGES NE DÉCIDENT PLUS. Le gate valait `len(fresh) >= 3` quel que
// soit le nombre de sièges tirés : 20 % prononçaient un slash dur. Quorum désormais relatif à ⌈2/3⌉
// des sièges ancrés.
func TestHaut3RedoQuorumIsRelativeToAnchoredSeats(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputeur_haut3_01_")

	// 15 sièges ancrés, mais seulement 3 re-commits divergents contre un primaire.
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mCible", types.Miner{MinerId: "mCible", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__mCible", types.Commit{ResultCommit: "1,0,0"}))
	seats := make([]string, 0, 15)
	for i := 0; i < 15; i++ {
		id := "seat" + string(rune('a'+i))
		seats = append(seats, id)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	redoAnchor(f, t, "jQ", seats...) // 15 sièges ancrés
	for _, id := range seats[:3] {   // 3 seulement re-commitent : 3 < ⌈2/3×15⌉=10
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jQ", types.Job{
		JobId: "jQ", State: "open+paid+optimistic+disputed", MinerId: "mCible", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jQ"})
	require.Error(t, err, "3 repondants sur 15 sieges sont sous le quorum ⌈2/3⌉ -> adjudication refusee")

	m, _ := f.keeper.Miner.Get(f.ctx, "mCible")
	require.Equal(t, adr033Big, m.Stake, "une minorite de sieges ne doit pas slasher")
}

// (E) LE TIRAGE EXCLUT LES PARTIES. Le primaire ne se juge pas, le disputeur ne juge pas sa propre
// accusation, et le comité d'ORIGINE est écarté (indépendance).
func TestADR033DrawExcludesPartiesAndOrigin(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_e01")

	// Le disputeur est AUSSI un mineur : c'est le cas qui compte (il pourrait s'auto-convoquer).
	for _, id := range []string{"mPrim", "mNeutral1", "mNeutral2", "mNeutral3", "mNeutral4"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Miner.Set(f.ctx, disp, types.Miner{MinerId: disp, Stake: adr033Big * 100}))

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jDraw", types.Job{
		JobId: "jDraw", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// Source d'aléa NON PRÉVISIBLE requise : sans elle le tirage est volontairement refusé, sinon
	// l'accusateur — qui choisit le job ET la hauteur de diffusion — pourrait énumérer les hauteurs
	// hors chaîne jusqu'à obtenir un comité de complices.
	require.NoError(t, f.keeper.SetBlockHash(f.ctx,
		sdk.UnwrapSDKContext(f.ctx).BlockHeight(), []byte{0xde, 0xad, 0xbe, 0xef, 0x01, 0x02}))

	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jDraw"})
	require.NoError(t, err)

	raw, err := f.keeper.AuditCommittee.Get(f.ctx, "jDraw__redo")
	require.NoError(t, err, "DisputeVerdict doit ANCRER le comité de re-adjudication à l'ouverture")
	members := strings.Split(raw, ",")
	require.NotEmpty(t, members)
	for _, id := range members {
		require.NotEqual(t, "mPrim", id, "le primaire ne se juge pas")
		require.NotEqual(t, disp, id, "le disputeur ne juge pas sa propre accusation (malgré 100× le stake)")
	}
}

// (I) GRAINE DEVINABLE -> AUCUN TIRAGE. Le repli d'origine valait `"dispute:<hauteur>"` : entièrement
// prévisible, alors que l'accusateur choisit le job ET la hauteur à laquelle il diffuse sa tx. Il
// pouvait donc énumérer les hauteurs hors chaîne jusqu'à ce que le tirage asseye ses complices, puis
// diffuser à la bonne. Sans source d'aléa non prévisible, on refuse d'ancrer : une adjudication
// indisponible (et dont le timeout est désormais non punitif) vaut mieux qu'un jury choisi par
// l'accusation.
func TestADR033NoUnpredictableSeedMeansNoDraw(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_i01")
	for _, id := range []string{"mPrim", "mA", "mB", "mC", "mD", "mE"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jSeed", types.Job{
		JobId: "jSeed", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// AUCUN SetBlockHash, et committee_seed_source=0 par défaut -> aucune graine imprévisible.
	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jSeed"})
	require.NoError(t, err, "la dispute s'ouvre quand même (liveness) : c'est le TIRAGE qui est refusé")

	_, gErr := f.keeper.AuditCommittee.Get(f.ctx, "jSeed__redo")
	require.Error(t, gErr,
		"aucun comité ne doit être ancré sur une graine que l'accusateur peut deviner")
}
