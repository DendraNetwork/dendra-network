package keeper

import (
	"strconv"
	"testing"
)

// TestSelectCommitteeStakeWeightedGO04 — GO-04 : la sélection de comité est PONDÉRÉE PAR LE STAKE.
// Propriétés vérifiées sur le cœur pur `selectCommittee` (sans keeper).
func TestSelectCommitteeStakeWeightedGO04(t *testing.T) {
	miners := []minerWeight{
		{"whale", 8000},
		{"m1", 1000}, {"m2", 1000}, {"m3", 1000}, {"m4", 1000}, {"m5", 1000},
	}
	const size = 2
	const N = 3000

	counts := map[string]int{}
	for i := 0; i < N; i++ {
		seed := "beacon" + strconv.Itoa(i) + "|job"
		for id := range selectCommittee(seed, miners, size) {
			counts[id]++
		}
	}
	// (1) PONDÉRATION : le whale (stake 8×) est sélectionné BEAUCOUP plus souvent qu'un petit mineur.
	if counts["whale"] <= counts["m1"] {
		t.Fatalf("pondération KO : whale=%d <= m1=%d (le stake ne pèse pas)", counts["whale"], counts["m1"])
	}
	// (1b) anti-centralisation : un petit mineur reste sélectionné parfois (pas d'éviction totale).
	if counts["m1"] == 0 {
		t.Fatalf("un petit mineur n'est JAMAIS sélectionné -> centralisation excessive")
	}

	// (2) DÉTERMINISME : même graine -> même comité (consensus reproductible inter-validateurs).
	a := selectCommittee("X|job", miners, size)
	b := selectCommittee("X|job", miners, size)
	if len(a) != size || len(b) != size {
		t.Fatalf("taille comité incorrecte : %d / %d (attendu %d)", len(a), len(b), size)
	}
	for id := range a {
		if !b[id] {
			t.Fatalf("non déterministe : %v != %v", a, b)
		}
	}
}

// TestSelectCommitteeZeroStakeRelegatedGO04 — GO-04 : un mineur au stake NUL (entièrement slashé) ne
// doit jamais évincer des mineurs au bond positif ; il ne complète que s'il n'y a pas assez de bond.
func TestSelectCommitteeZeroStakeRelegatedGO04(t *testing.T) {
	miners := []minerWeight{
		{"zero", 0},
		{"m1", 1000}, {"m2", 1000}, {"m3", 1000}, {"m4", 1000},
	}
	const size = 2
	for i := 0; i < 2000; i++ {
		seed := "s" + strconv.Itoa(i)
		if selectCommittee(seed, miners, size)["zero"] {
			t.Fatalf("graine %s : un mineur stake=0 sélectionné malgré 4 mineurs au bond positif", seed)
		}
	}
	// mais si size >= nb de mineurs au bond positif, le stake-0 complète (pas de blocage du comité).
	if !selectCommittee("s", miners, len(miners))["zero"] {
		t.Fatalf("avec size=tous, le stake-0 doit compléter le comité")
	}
}
