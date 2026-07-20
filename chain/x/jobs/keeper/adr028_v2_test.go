package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/types"
)

// ADR-028 v2 — CONFORMITÉ PLEINE : 3 params gouvernables (silence_slash_bps / appeal_window / audit_min_quorum),
// TOUS DORMANTS par défaut (0). Ces tests prouvent (a) que le défaut 0/0/0 reproduit STRICTEMENT le comportement
// v1 ; (b) qu'audit_min_quorum>0 relève le plancher de participation (un quorum qui slashait en v1 ne slashe
// plus) ; (c) que silence_slash_bps>0 ajoute une pénalité de stake AU clawback no-quorum.
//
// NOTE : ce fichier COMPILE après `ignite generate proto-go` (les getters/setters SilenceSlashBps / AppealWindow /
// AuditMinQuorum n'existent dans params.pb.go qu'APRÈS régénération). Réutilise les helpers d'adr028_antievasion_test.go
// (initFixture, aeReg, aeVerdict, aeFundModule, aeCtx6, aeStake). AuditCommitteeSize=5 (DÉCOUPLÉ de CommitteeSize=3)
// -> plancher de repli = ⌈5/2⌉+1 = 4.

// (V2-1) DÉFAUT DORMANT (silence=0, appeal=0, quorum=0) == comportement v1 INCHANGÉ.
//   - Validate() en mode 0 : aucune nouvelle contrainte (e2e actuelle intacte).
//   - Au timeout : 4 juges "0" (= plancher de repli via effectiveSlashFloor) -> slash DUR 80 %, identique au test
//     v1 TestADR028CheatQuorumHardSlashAtTimeout. Aucune pénalité de silence (silence_slash_bps=0).
func TestADR028V2DefaultDormantMatchesV1(t *testing.T) {
	// (a) Validate dormant : mode 0 + nouveaux champs à 0 -> OK, comme v1.
	off := types.DefaultParams()
	require.Equal(t, uint64(0), off.SilenceSlashBps, "défaut dormant")
	require.Equal(t, uint64(0), off.AppealWindow, "défaut dormant")
	require.Equal(t, uint64(0), off.AuditMinQuorum, "défaut dormant")
	require.NoError(t, off.Validate(), "params par défaut (dormants) valides comme v1")

	// (b) Timeout avec params par défaut (mode 1 minimal, sans toucher aux 3 nouveaux) : plancher = repli = 4.
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1) // 4 juges = plancher de repli atteint via effectiveSlashFloor (audit_min_quorum=0)
	aeAnchor(f, t, "jD", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jD", "mJ1", "0")
	aeVerdict(f, t, "jD", "mJ2", "0")
	aeVerdict(f, t, "jD", "mJ3", "0")
	aeVerdict(f, t, "jD", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028v2_dflt"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jD", types.Job{
		JobId: "jD", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jD")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jD")
	require.NoError(t, err)
	require.Contains(t, job.State, "clawed")
	require.Len(t, job.SlashRecords, 1, "défaut : un seul SlashRecord (slash dur), AUCUNE pénalité de silence")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "slash DUR 80 % comme v1")
}

// (V2-2) audit_min_quorum=5 RELÈVE le plancher : 4 juges "0" (qui slashaient DUR au plancher de repli=4) ne
// déclenchent PLUS le slash dur (5 requis) -> repli sur clawback LÉGER (prix seul). Démontre que le param MORD
// (inverse exact du test v1 (1) où 4 juges suffisaient). silence_slash_bps reste 0 -> pas de pénalité additionnelle.
func TestADR028V2MinQuorumRaisesFloor(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1 : Validate exige sample>0 ET audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.AuditMinQuorum = 5 // > 4 juges présents -> plancher NON atteint -> pas de slash dur
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "audit_min_quorum=5 valide en mode 1 (encodage entier, pas de borne haute)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1) // 4 juges "0" : suffisait pour slash dur au repli (plancher 4), INSUFFISANT ici (quorum 5)
	aeAnchor(f, t, "jQ", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jQ", "mJ1", "0")
	aeVerdict(f, t, "jQ", "mJ2", "0")
	aeVerdict(f, t, "jQ", "mJ3", "0")
	aeVerdict(f, t, "jQ", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028v2_quor"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jQ", types.Job{
		JobId: "jQ", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jQ")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake, "quorum relevé -> clawback LÉGER (prix), JAMAIS slash dur 80 %")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix")
}

// (V2-3, RÉVISÉ α-(b) internal audit 2026-07-04) silence_slash_bps=2000 : cas NO-QUORUM AVEC SIGNAL MUET (≥1 verdict
// « 0 » posté, sous le plancher de 4) -> clawback du prix PLUS une pénalité de stake DÉDIÉE de 20 % du stake
// RESTANT. La pénalité exige désormais le SIGNAL (le « 0 » ADR-028 posté sur révélation absente/suspecte) —
// cf. V2-3bis pour le cas abstentions-pures qui, lui, ne coûte QUE le prix.
func TestADR028V2SilenceSlashAddsPenalty(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1 : Validate exige sample>0 ET audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.SilenceSlashBps = 2000 // 20 % : pénalité de silence DÉDIÉE en plus du clawback du prix
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "silence_slash_bps=2000 valide en mode 1 (<=10000)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)                // UN juge présent...
	aeAnchor(f, t, "jS", "mJ1") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jS", "mJ1", "0")    // ...poste « 0 » (signal muet ADR-028) : 1 < plancher 4 -> no-quorum
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_siln"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jS", types.Job{
		JobId: "jS", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jS")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Attendus : clawback du prix (100000) PUIS pénalité = 20 % du stake RESTANT après ce clawback.
	afterPrice := aeStake - 100000
	penalty := afterPrice * 2000 / 10000
	wantStake := afterPrice - penalty

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, wantStake, mP.Stake, "clawback du prix + pénalité de silence 20 %% du stake restant (signal muet présent)")
	require.Less(t, mP.Stake, aeStake-100000, "la pénalité de silence retire PLUS que le seul clawback du prix (v1)")

	job, _ := f.keeper.Job.Get(f.ctx, "jS")
	require.Len(t, job.SlashRecords, 2, "2 SlashRecords RESTITUABLES : clawback du prix + pénalité de silence")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix")
}

// (V2-3bis, α-(b) internal audit 2026-07-04) NO-QUORUM d'ABSTENTIONS PURES (AUCUN verdict posté — juges incertains
// qui s'abstiennent, ou simplement absents) : le primaire ne garde pas un paiement non vérifié (clawback du
// prix, client remboursé) mais son BOND n'est PAS pénalisé — aucun signal de mutisme n'existe. L'ancien
// comportement (pénalité sur tout no-quorum) punissait l'HONNÊTE pour l'incertitude du JUGE (runs 5-11 :
// −20 % de bond composé par incident, zéro slash dur). Le muet RÉEL, lui, provoque des « 0 » -> V2-3.
func TestADR028V2SilenceSlashRequiresMuteSignal(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.SilenceSlashBps = 2000 // armé — mais AUCUN verdict ne sera posté
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake) // aucun juge ne poste : abstentions pures -> no-quorum SANS signal
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_abst"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jA", types.Job{
		JobId: "jA", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jA")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake,
		"abstentions pures : clawback du prix SEUL — la pénalité de silence ne s'applique pas sans signal muet")

	job, _ := f.keeper.Job.Get(f.ctx, "jA")
	require.Len(t, job.SlashRecords, 1, "1 seul SlashRecord (le prix) : pas de pénalité de silence")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix")
}

// (V2-4) ⭐ VETO N=5 PRO-HONNÊTE (internal audit 2026-06-22) — le COUNT, PAS la majorité de stake. 5 juges votent : 3 "0"
// (invalide) + 2 "1" (valide), TOUS à stake ÉGAL -> la MAJORITÉ DE STAKE est "invalide" (3 > 2) : en v1
// (audit_min_quorum=0) ça SLASHAIT DUR. Avec audit_min_quorum=4 (quasi-unanimité ⌈2N/3⌉ à N=5), il faut 4 verdicts
// "invalide" : COUNT 3 < 4 -> PAS de slash dur -> clawback LÉGER restituable. Asymétrie pro-accusé : un honnête
// mal-jugé par une MINORITÉ (3/5) n'est PAS slashé, là où la majorité-stake v1 l'aurait condamné.
func TestADR028V2VetoBlocksStakeMajoritySlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.AuditMinQuorum = 4 // VETO : 4/5 "invalide" requis pour un slash dur (quasi-unanimité)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4", "mJ5"} {
		aeReg(f, t, j, 1) // stake égal -> 3 "invalide" = MAJORITÉ DE STAKE (slasherait en v1)
	}
	aeAnchor(f, t, "jVeto", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jVeto", "mJ1", "0")
	aeVerdict(f, t, "jVeto", "mJ2", "0")
	aeVerdict(f, t, "jVeto", "mJ3", "0") // 3 invalide (majorité de stake) MAIS count 3 < quorum 4
	aeVerdict(f, t, "jVeto", "mJ4", "1")
	aeVerdict(f, t, "jVeto", "mJ5", "1") // 2 valide

	clientAcc := sdk.AccAddress([]byte("client_adr028_veto00"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jVeto", types.Job{
		JobId: "jVeto", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jVeto")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jVeto")
	require.Contains(t, job.State, "clawed")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-100000, mP.Stake, "VETO : 3/5 invalide (majorité-stake) -> clawback LÉGER, JAMAIS slash dur 80 %")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client remboursé du prix (clawback léger)")
}

// (V2-5) ⭐ VETO N=5 — la quasi-unanimité TRANCHE : 4 "0" (invalide) + 1 "1" (valide) sur 5. COUNT invalide = 4 >=
// quorum 4 -> SLASH DUR 80 %. Un SEUL dissident "valide" ne sauve PAS un tricheur quasi-unanimement rejeté (⌈2N/3⌉=4) :
// le veto protège l'honnête (V2-4) SANS laisser échapper le tricheur clair.
func TestADR028V2VetoSlashesAtQuasiUnanimity(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4", "mJ5"} {
		aeReg(f, t, j, 1)
	}
	aeAnchor(f, t, "jUna", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032 : juges CONVOQUÉS (sans ancrage, tally fail-closed)
	aeVerdict(f, t, "jUna", "mJ1", "0")
	aeVerdict(f, t, "jUna", "mJ2", "0")
	aeVerdict(f, t, "jUna", "mJ3", "0")
	aeVerdict(f, t, "jUna", "mJ4", "0") // 4 invalide = quasi-unanimité (>= quorum 4)
	aeVerdict(f, t, "jUna", "mJ5", "1") // 1 dissident valide -> ne bloque PAS le slash

	clientAcc := sdk.AccAddress([]byte("client_adr028_veto11"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jUna", types.Job{
		JobId: "jUna", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jUna")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "VETO : 4/5 invalide (quasi-unanimité) -> slash DUR 80 %, le dissident valide ne sauve pas le tricheur")
}
