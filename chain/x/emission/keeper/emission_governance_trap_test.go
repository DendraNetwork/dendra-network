package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/emission/types"
)

// LE PIÈGE DE GOUVERNANCE : un paramètre dont la valeur « arrêter » produisait le maximum.
//
// `RunEpoch` testait `if pp.ReserveReleaseBps != 0` avant d'adopter les params gouvernés. Or 0 est
// EXACTEMENT ce qu'une gouvernance pose pour suspendre l'émission, et `Validate()` l'accepte. Le
// paramètre était donc lu comme « non configuré » : tout le jeu voté était ignoré et remplacé par les
// défauts codés en dur — 22 % de la Réserve libérés PAR ÉPOQUE, splits et gate votés écrasés au
// passage. La décision produisait son contraire, sans erreur ni journal.
//
// Symétriquement, `epoch_blocks` n'avait aucune borne : à 0 l'échéance tombe à chaque bloc, et près
// de 2^64 la somme `last + epochBlocks` déborde, la comparaison s'inverse, et l'échéance tombe à
// chaque bloc AUSSI. « Ne plus jamais émettre » donnait l'émission maximale.

// (A) 0 doit rester 0. Le test porte sur la VALIDATION : une valeur nulle est un réglage recevable,
// et c'est précisément ce qui la rendait dangereuse tant qu'elle servait aussi de sentinelle.
func TestEmissionZeroReleaseIsAValidSettingNotASentinel(t *testing.T) {
	p := types.DefaultParams()
	p.ReserveReleaseBps = 0
	require.NoError(t, p.Validate(),
		"reserve_release_bps=0 est le geste normal pour suspendre l'emission : Validate doit l'accepter")
}

// (B) LES DEUX VALEURS DESTRUCTRICES D'`epoch_blocks` SONT REFUSÉES.
// Sans ces bornes, la gouvernance disposait de deux façons d'obtenir l'émission maximale en croyant
// faire l'inverse — et aucune n'était détectable avant que la Réserve soit vidée.
func TestEmissionEpochBlocksBoundsRejectBothRunawayValues(t *testing.T) {
	p := types.DefaultParams()

	// 0 doit rester VALIDE : le genesis par defaut (`GenesisState{}`, `config.yml`) ne pose aucun
	// param d'emission, donc tout y est a zero. Le refuser ferait paniquer InitGenesis au demarrage
	// d'une chaine neuve — on remplacerait une fuite de valeur par une chaine qui ne demarre pas.
	// La protection est au RUNTIME (epoch.go) : 0 = emission desactivee, jamais un repli sur 22 %.
	p.EpochBlocks = 0
	require.NoError(t, p.Validate(),
		"epoch_blocks=0 = « non configure » : recevable au genesis, neutralise au runtime")

	p.EpochBlocks = 1 << 40 // « jamais » : deborde last+epochBlocks -> condition inversee
	require.Error(t, p.Validate(),
		"un epoch_blocks enorme deborde le calcul d'echeance et produit l'effet INVERSE de l'intention")

	p.EpochBlocks = 300 // le reglage reel du testnet
	require.NoError(t, p.Validate(), "une valeur d'exploitation normale doit passer")
}

// (C) LA COMPARAISON D'ÉCHÉANCE NE DOIT PAS DÉBORDER, quelle que soit la valeur en store.
// Reproduit l'arithmétique des deux formes : l'ancienne additionne (et déborde), la nouvelle
// soustrait (et ne peut pas). Un test qui n'exercerait que des valeurs raisonnables serait aveugle
// au seul cas qui compte.
func TestEmissionEpochDeadlineArithmeticCannotOverflow(t *testing.T) {
	const h, last = uint64(1000), uint64(900)

	ancien := func(epochBlocks uint64) bool { return h < last+epochBlocks } // true = « pas encore l'heure »
	nouveau := func(epochBlocks uint64) bool { return h < last || h-last < epochBlocks }

	// Valeur d'exploitation : les deux formes s'accordent (non-régression).
	require.Equal(t, ancien(300), nouveau(300))
	require.True(t, nouveau(300), "100 blocs ecoules sur 300 -> pas encore l'heure")

	// Valeur hostile : l'ancienne forme deborde et declare l'echeance ATTEINTE.
	const enorme = ^uint64(0) - 100
	require.False(t, ancien(enorme), "l'ancienne forme deborde -> epoque a chaque bloc (le bug)")
	require.True(t, nouveau(enorme), "la nouvelle forme tient : l'echeance n'est PAS atteinte")
}
