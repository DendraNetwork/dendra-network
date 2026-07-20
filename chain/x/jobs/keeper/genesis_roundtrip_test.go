package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/types"
)

// EXPORT/IMPORT SANS PERTE.
//
// `GenesisState` s'arrêtait au champ 6 pendant que le module tenait 23 collections : un export/import
// en perdait 17 EN SILENCE — dont `HeldFee`/`HeldBurn` (les coins survivent au compte de module, mais
// plus rien ne dit à qui ils reviennent) et les quatre files d'échéances (les jobs concernés ne se
// résolvent alors JAMAIS, donc leur escrow reste bloqué sans qu'aucune erreur ne soit levée).
//
// Le test écrit de l'état dans CHAQUE collection transportée, exporte, réimporte dans un keeper NEUF,
// et compare. Un champ oublié dans `ExportGenesis` OU dans `InitGenesis` le fait échouer. Sans un tel
// test, une collection peut être ajoutée au keeper sans jamais être branchée au genesis, et rien ne
// le signale : c'est une omission qui ne produit aucune erreur, seulement une perte silencieuse.
func TestGenesisRoundTripLosesNothing(t *testing.T) {
	f := initFixture(t)

	// --- état d'origine, une entrée par collection transportée -------------------------------------
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m1", types.Miner{MinerId: "m1", Stake: 42}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", State: "open", Fee: 7}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 99}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__m1", types.Commit{JobId: "j1", ResultCommit: "1,0"}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "j1", types.Beacon{JobId: "j1", Seed: "s"}))

	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "j1", 500))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "j1", 25))
	require.NoError(t, f.keeper.PendingReveal.Set(f.ctx, collections.Join(int64(11), "j1")))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(12), "j1")))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(13), "j1")))
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(int64(14), "j1")))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "j1", "m1,m2,m3"))
	require.NoError(t, f.keeper.ValidatorVrfPubkey.Set(f.ctx, "val1", "abcdef"))
	require.NoError(t, f.keeper.MinerOptimisticCount.Set(f.ctx, "m1", 3))
	require.NoError(t, f.keeper.AvailFailCount.Set(f.ctx, "m1", 2))
	require.NoError(t, f.keeper.AvailFailWindowStart.Set(f.ctx, "m1", 100))
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "challenge-x"))

	// EXPORT À UNE HAUTEUR, IMPORT À UNE AUTRE. Sans cet écart, le test serait AVEUGLE à la propriété
	// qu'il prétend vérifier : à hauteur d'export ET d'import égales à 0, `restant = échéance - 0` puis
	// `0 + restant` retombe sur la valeur d'origine, et l'ancien code en hauteurs ABSOLUES passerait le
	// test à l'identique. C'est exactement le genre de vert rassurant qui ne prouve rien.
	const exportH, importH = int64(10), int64(5000)
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(exportH)

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	// --- réimport dans un keeper VIERGE, BIEN PLUS LOIN dans la chaîne -----------------------------
	g := initFixture(t)
	g.ctx = sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(importH)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	// VALEUR — le cas qui produisait des fonds orphelins.
	hf, err := g.keeper.HeldFee.Get(g.ctx, "j1")
	require.NoError(t, err, "HeldFee doit survivre : sans lui, les coins retenus n'ont plus de destinataire")
	require.Equal(t, uint64(500), hf)
	hb, err := g.keeper.HeldBurn.Get(g.ctx, "j1")
	require.NoError(t, err)
	require.Equal(t, uint64(25), hb)

	// ÉCHÉANCES — les perdre fige les jobs pour toujours, sans erreur. Et les transporter en hauteur
	// ABSOLUE revient au même : l'EndBlocker matche la hauteur EXACTE, donc un rendez-vous à 13 sur une
	// chaîne repartie à 5000 ne serait jamais atteint. On vérifie le RÉ-ANCRAGE : `importH + restant`,
	// où `restant = échéance - exportH`.
	for name, pair := range map[string]collections.Pair[int64, string]{
		"PendingReveal":        collections.Join(importH+(11-exportH), "j1"),
		"PendingAudit":         collections.Join(importH+(12-exportH), "j1"),
		"PendingAuditResolve":  collections.Join(importH+(13-exportH), "j1"),
		"PendingAppealResolve": collections.Join(importH+(14-exportH), "j1"),
	} {
		var has bool
		switch name {
		case "PendingReveal":
			has, err = g.keeper.PendingReveal.Has(g.ctx, pair)
		case "PendingAudit":
			has, err = g.keeper.PendingAudit.Has(g.ctx, pair)
		case "PendingAuditResolve":
			has, err = g.keeper.PendingAuditResolve.Has(g.ctx, pair)
		case "PendingAppealResolve":
			has, err = g.keeper.PendingAppealResolve.Has(g.ctx, pair)
		}
		require.NoError(t, err)
		require.True(t, has, "%s : rendez-vous non re-ancre sur la hauteur de reprise -> il n'aura jamais lieu (le walk matche la hauteur EXACTE) et l'escrow reste bloque", name)
	}
	// Et la preuve NÉGATIVE : l'ancienne clé absolue ne doit plus exister, sinon on aurait simplement
	// ajouté une entrée au lieu de la déplacer — et l'entrée morte resterait en base pour toujours.
	staleHas, err := g.keeper.PendingAuditResolve.Has(g.ctx, collections.Join(int64(13), "j1"))
	require.NoError(t, err)
	require.False(t, staleHas, "l'echeance ne doit PAS rester a sa hauteur d'origine (elle est derriere la reprise)")

	// AUTORISATION + anti-abus.
	ac, err := g.keeper.AuditCommittee.Get(g.ctx, "j1")
	require.NoError(t, err, "comite ancre perdu -> toute dispute en vol devient inerte")
	require.Equal(t, "m1,m2,m3", ac)
	vk, err := g.keeper.ValidatorVrfPubkey.Get(g.ctx, "val1")
	require.NoError(t, err, "cle VRF perdue -> anti-grinding INACTIF jusqu'a re-ancrage")
	require.Equal(t, "abcdef", vk)
	moc, err := g.keeper.MinerOptimisticCount.Get(g.ctx, "m1")
	require.NoError(t, err, "probation Sybil perdue -> un mineur connu repart a zero")
	require.Equal(t, uint64(3), moc)
	afc, err := g.keeper.AvailFailCount.Get(g.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(2), afc)
	afw, err := g.keeper.AvailFailWindowStart.Get(g.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(100), afw)
	chal, err := g.keeper.AvailChallenge.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, "challenge-x", chal)

	// L'état d'origine survit aussi (non-régression des 6 champs historiques).
	m, err := g.keeper.Miner.Get(g.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(42), m.Stake)
	p, err := g.keeper.Pools.Get(g.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(99), p.Treasury)
}

// COMPATIBILITÉ ASCENDANTE. Un genesis produit par un binaire antérieur n'a AUCUN des champs 7+.
// L'import doit alors se comporter exactement comme avant, pas échouer sur des tranches nil.
func TestGenesisImportToleratesLegacyFile(t *testing.T) {
	f := initFixture(t)
	legacy := types.DefaultGenesis() // champs 7+ tous vides, comme un export d'avant ce lot
	legacy.MinerMap = []types.Miner{{MinerId: "m1", Stake: 1}}
	require.NoError(t, f.keeper.InitGenesis(f.ctx, *legacy))
	m, err := f.keeper.Miner.Get(f.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(1), m.Stake)
}

// ÉCHÉANCE DE DISPUTE : c'est le temps RESTANT qui doit survivre au reset, pas la hauteur.
//
// `DisputeHeight` est une hauteur ABSOLUE de l'ancienne chaîne. Exportée telle quelle, la garde
// `BlockHeight() < DisputeHeight + DisputeWindow` (msg_server_adjudicate.go) reste vraie pendant
// TOUTE la hauteur de l'ancienne chaîne : l'adjudication devient inatteignable et le bond du
// disputeur reste immobilisé. Le job est gelé définitivement — et, comme la perte silencieuse que
// teste le round-trip ci-dessus, sans qu'aucune erreur ne soit jamais levée.
//
// Ce test ÉCHOUE sur le code d'avant : après un import à la hauteur 1, il y lisait encore
// 1 000 000, soit une fenêtre repoussée d'un million de blocs au lieu des 50 restants.
func TestGenesisDisputeDeadlineSurvivesReset(t *testing.T) {
	const (
		disputeH = int64(1_000_000) // dispute ouverte haut dans l'ancienne chaîne
		exportH  = int64(1_000_050) // 50 blocs écoulés au moment de l'export
		importH  = int64(1)         // la nouvelle chaîne repart de zéro
		elapsed  = exportH - disputeH
	)

	f := initFixture(t)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jd", types.Job{
		JobId: "jd", State: "settled+disputed", Disputer: "d1", DisputeHeight: disputeH,
	}))
	// Un job JAMAIS disputé : témoin. Il doit rester à 0, sans décalage parasite.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jn", types.Job{JobId: "jn", State: "settled"}))

	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(exportH)
	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)

	g := initFixture(t)
	g.ctx = sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(importH)
	require.NoError(t, g.keeper.InitGenesis(g.ctx, *exported))

	jd, err := g.keeper.Job.Get(g.ctx, "jd")
	require.NoError(t, err)

	// L'invariant qui compte : l'écoulé depuis l'ouverture est CONSERVÉ, donc le temps restant
	// de la fenêtre aussi — quelle que soit la hauteur à laquelle la nouvelle chaîne démarre.
	require.Equal(t, elapsed, importH-jd.DisputeHeight,
		"ecoule non preserve : la fenetre de dispute n'expire plus au meme moment relatif")

	// Preuve NÉGATIVE — sans elle, le test ne distingue pas « ré-ancré » de « inchangé ».
	require.NotEqual(t, disputeH, jd.DisputeHeight,
		"hauteur absolue de l'ancienne chaine reimportee telle quelle : adjudication inatteignable")

	// Et la garde d'adjudication doit effectivement pouvoir se rouvrir dans un délai borné.
	require.Less(t, jd.DisputeHeight, importH,
		"l'echeance doit etre atteignable depuis la hauteur d'init, pas repoussee d'un million de blocs")

	jn, err := g.keeper.Job.Get(g.ctx, "jn")
	require.NoError(t, err)
	require.Zero(t, jn.DisputeHeight, "un job jamais dispute ne doit pas se voir inventer une echeance")
}
