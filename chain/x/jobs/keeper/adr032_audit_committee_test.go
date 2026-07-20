package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/types"
)

// ADR-032 — LE COMITÉ D'AUDIT EST ANCRÉ : seuls les convoqués votent.
//
// Ces tests existent parce que la suite précédente prouvait le CONTRAIRE :
// `TestADR028CheatQuorumHardSlashAtTimeout` faisait littéralement l'attaque (4 juges à stake=1, jamais
// convoqués, slashent un primaire de 80 %) et l'EXIGEAIT verte. Le test ne mesurait pas la sécurité, il
// certifiait la faille. On teste donc ici la propriété elle-même, dans les deux sens : l'attaque échoue,
// et le slash légitime part toujours.

// (A) L'ATTAQUE — 4 identités à min_stake, NON convoquées, votent "0" contre un honnête.
// Attendu : leurs verdicts sont IGNORÉS -> aucun slash dur, stake du primaire INTACT.
// C'est le test qui aurait dû exister depuis le début.
func TestADR032SybilNonMembersCannotSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mHonest", aeStake)
	// L'attaquant s'enregistre 4 fois pour ~rien (le bond est remboursé à l'exit) et vote "0".
	for _, s := range []string{"sybil1", "sybil2", "sybil3", "sybil4"} {
		aeReg(f, t, s, 1)
		aeVerdict(f, t, "jAtk", s, "0")
	}
	// Un comité EST ancré, mais aucun sybil n'en fait partie : ils n'ont pas été tirés.
	aeAnchor(f, t, "jAtk", "mJuror1", "mJuror2", "mJuror3", "mJuror4")

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jAtk", types.Job{
		JobId: "jAtk", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jAtk")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Ce qui se joue ici est la DIFFÉRENCE entre deux issues, pas « rien ne bouge » :
	//   - slash DUR (l'attaque réussie)  = 80 % du stake -> il resterait 2e11
	//   - clawback LÉGER (issue correcte) = le prix du job, restituable -> il reste aeStake-100000
	// Le paiement n'a pas pu être vérifié faute de quorum CONVOQUÉ : le reprendre est le comportement
	// voulu (un mineur ne garde pas un règlement que personne n'a pu vérifier). Ce qui est interdit,
	// c'est que des inconnus déclenchent la peine LOURDE.
	hardSlashLeaves := aeStake - aeStake*p.SlashLeakBps/10000
	mH, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Greater(t, mH.Stake, hardSlashLeaves,
		"des NON-CONVOQUÉS ne doivent JAMAIS déclencher le slash DUR d'un honnête (la faille que ferme l'ADR-032)")
	require.Equal(t, aeStake-100000, mH.Stake,
		"seul le clawback léger (prix du job, restituable) s'applique")
	job, _ := f.keeper.Job.Get(f.ctx, "jAtk")
	require.Len(t, job.SlashRecords, 1, "un enregistrement RESTITUABLE de clawback, pas un slash dur")
	require.Equal(t, uint64(100000), job.SlashRecords[0].Amount,
		"le montant repris est le PRIX DU JOB, pas une fraction du bond")
}

// (B) FAIL-CLOSED — job SANS comité ancré (jobs antérieurs, chemins hors mode-1).
// Attendu : clawback léger possible, mais JAMAIS de slash dur. L'asymétrie est voulue : rater un
// tricheur est borné et -EV (ADR-025), slasher un honnête ne se rattrape pas.
func TestADR032NoAnchoredCommitteeNeverHardSlashes(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jNoAnchor", j, "0")
	}
	// PAS d'aeAnchor : aucune liste ancrée pour ce job.

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jNoAnchor", types.Job{
		JobId: "jNoAnchor", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jNoAnchor")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Même distinction que ci-dessus : le clawback léger EST attendu (paiement non vérifié), et il
	// laisse une trace RESTITUABLE. Ce qui doit être impossible sans comité ancré, c'est le slash dur.
	hardSlashLeaves := aeStake - aeStake*p.SlashLeakBps/10000
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Greater(t, mP.Stake, hardSlashLeaves, "sans comité ancré : AUCUN slash dur (fail-closed)")
	require.Equal(t, aeStake-100000, mP.Stake, "clawback léger seul (prix du job), jamais 80 %")
	job, _ := f.keeper.Job.Get(f.ctx, "jNoAnchor")
	require.Len(t, job.SlashRecords, 1, "trace restituable du clawback")
	require.Equal(t, uint64(100000), job.SlashRecords[0].Amount, "montant = prix du job")
}

// (C) NON-RÉGRESSION — un comité MIXTE : les convoqués votent "0" au quorum, des sybils votent "1"
// pour tenter de diluer. Attendu : les sybils sont ignorés dans les DEUX sens -> le slash légitime part.
// Vérifie que la restriction ne protège pas que l'honnête : elle authentifie l'ensemble votant, point.
func TestADR032NonMembersCannotDiluteLegitimateSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mCheat", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jMix", j, "0")
	}
	// 10 sybils à gros stake votent "1" : sans la restriction, ils renverseraient la majorité de stake.
	for _, s := range []string{"x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10"} {
		aeReg(f, t, s, aeStake)
		aeVerdict(f, t, "jMix", s, "1")
	}
	aeAnchor(f, t, "jMix", "mJ1", "mJ2", "mJ3", "mJ4")

	require.NoError(t, f.keeper.Job.Set(f.ctx, "jMix", types.Job{
		JobId: "jMix", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jMix")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mC, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mC.Stake,
		"le slash légitime doit partir : les non-convoqués ne diluent rien")
}

// (F) LE VETO REDEVIENT UNE QUASI-UNANIMITÉ — le bug de calibration d'ADR-032, corrigé.
//
// Le veto valait « 4 sur un comité de 5 » = 80 % = quasi-unanimité, et c'était TOUTE sa raison d'être :
// un honnête jugé invalide par une MINORITÉ n'est jamais slashé. ADR-032 a porté le comité tiré à 15
// sièges pour la liveness en gardant le quorum absolu à 4 — soit **27 %**. Résultat : 4 verdicts
// « invalide » slashaient même avec 11 jurés votant « valide ». Le seuil n'avait pas bougé ; c'est son
// DÉNOMINATEUR qui avait changé sous lui. Ce test est le témoin de ce cas exact.
func TestADR032MinorityOfAnchoredCommitteeCannotSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	all := []string{}
	for i := 0; i < 15; i++ {
		id := "j" + string(rune('a'+i))
		aeReg(f, t, id, 1_000_000)
		all = append(all, id)
	}
	aeAnchor(f, t, "jMin", all...) // 15 sièges ANCRÉS
	// 4 convoqués crient « invalide », 11 convoqués disent « valide » : une MINORITÉ accuse.
	for _, j := range all[:4] {
		aeVerdict(f, t, "jMin", j, "0")
	}
	for _, j := range all[4:] {
		aeVerdict(f, t, "jMin", j, "1")
	}
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jMin", types.Job{
		JobId: "jMin", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jMin")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	hardSlashLeaves := aeStake - aeStake*p.SlashLeakBps/10000
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Greater(t, mP.Stake, hardSlashLeaves,
		"4 accusateurs sur 15 = 27 % : une MINORITÉ ne doit JAMAIS déclencher le slash dur (⌈2/3⌉ requis)")
}

// (G) NON-RÉGRESSION — la quasi-unanimité slashe toujours. Sans ce test, (F) pourrait être satisfait
// par un correctif qui gèle purement et simplement le slash, ce qui serait une régression silencieuse.
func TestADR032SupermajorityOfAnchoredCommitteeStillSlashes(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mCheat", aeStake)
	all := []string{}
	for i := 0; i < 15; i++ {
		id := "k" + string(rune('a'+i))
		aeReg(f, t, id, 1_000_000)
		all = append(all, id)
	}
	aeAnchor(f, t, "jSup", all...)
	// 10 sur 15 = ⌈2/3⌉ atteint, et ils détiennent la majorité du stake votant -> les DEUX verrous cèdent.
	for _, j := range all[:10] {
		aeVerdict(f, t, "jSup", j, "0")
	}
	for _, j := range all[10:] {
		aeVerdict(f, t, "jSup", j, "1")
	}
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jSup", types.Job{
		JobId: "jSup", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jSup")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mC, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mC.Stake,
		"⌈2/3⌉ des sièges ancrés + majorité de stake : le slash légitime doit toujours partir")
}

// (D) DÉTERMINISME DU TIRAGE — même (graine, jobId, ensemble d'éligibles) => MÊME comité, dans le MÊME
// ordre. Non négociable : deux validateurs qui tireraient des listes différentes ancreraient des chaînes
// différentes, et la chaîne forkerait. On rejoue le tirage via le keeper, l'ordre d'insertion des mineurs
// ne doit rien changer.
func TestADR032DrawIsDeterministic(t *testing.T) {
	ids := []string{"mA", "mB", "mC", "mD", "mE", "mF", "mG", "mH"}

	f1 := initFixture(t)
	for _, id := range ids {
		aeReg(f1, t, id, 1_000_000)
	}
	got1, err := f1.keeper.DrawAuditCommitteeForTest(f1.ctx, "seed-xyz", "job-1", "mA", "")
	require.NoError(t, err)

	// même ensemble, inséré en ORDRE INVERSE
	f2 := initFixture(t)
	for i := len(ids) - 1; i >= 0; i-- {
		aeReg(f2, t, ids[i], 1_000_000)
	}
	got2, err := f2.keeper.DrawAuditCommitteeForTest(f2.ctx, "seed-xyz", "job-1", "mA", "")
	require.NoError(t, err)

	require.Equal(t, got1, got2, "le tirage doit être indépendant de l'ordre d'itération du store")
	require.NotContains(t, got1, "mA", "le primaire ne siège jamais dans le comité qui le juge")

	// une graine différente doit produire un tirage différent (sinon le tirage n'en est pas un)
	got3, err := f1.keeper.DrawAuditCommitteeForTest(f1.ctx, "seed-AUTRE", "job-1", "mA", "")
	require.NoError(t, err)
	require.NotEqual(t, got1, got3, "changer la graine doit changer le comité")
}

// (E) PONDÉRATION PAR LE STAKE — c'est le cœur de la défense : se multiplier en identités à min_stake ne
// doit PAS acheter de sièges. Face à quelques gros stakes, une nuée de poussières doit rester minoritaire.
func TestADR032DrawIsStakeWeightedNotIdentityCount(t *testing.T) {
	f := initFixture(t)
	// 3 mineurs sérieux
	for _, id := range []string{"big1", "big2", "big3"} {
		aeReg(f, t, id, 1_000_000_000_000)
	}
	// 60 identités de poussière (l'attaque par nombre)
	for i := 0; i < 60; i++ {
		aeReg(f, t, "dust"+string(rune('a'+i%26))+string(rune('0'+i/26)), 1)
	}
	members, err := f.keeper.DrawAuditCommitteeForTest(f.ctx, "seed-w", "job-w", "none", "")
	require.NoError(t, err)
	require.NotEmpty(t, members)

	bigs := 0
	for _, m := range members {
		if m == "big1" || m == "big2" || m == "big3" {
			bigs++
		}
	}
	require.Equal(t, 3, bigs,
		"les 3 gros stakes doivent TOUS être tirés malgré 60 identités de poussière : le siège s'achète en capital, pas en nombre")
}
