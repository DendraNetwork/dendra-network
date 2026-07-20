package keeper

import "testing"

// helper : toutes les empreintes de préfixe (i=0..len) en hex.
func prefixHexAll(jobId, minerId string, toks []string) []string {
	out := make([]string, 0, len(toks)+1)
	for i := 0; i <= len(toks); i++ {
		out = append(out, tokenPrefixHashAt(jobId, minerId, toks, i))
	}
	return out
}

// INT-1 v1 brique 1 — la racine est déterministe, sensible au moindre token, et liée au mineur.
func TestTokenChainRoot(t *testing.T) {
	a := tokenChainRoot("j1", "m1", []string{"Le", "chat", "dort"})
	b := tokenChainRoot("j1", "m1", []string{"Le", "chat", "dort"})
	if a != b {
		t.Fatalf("racine non deterministe: %s != %s", a, b)
	}
	if c := tokenChainRoot("j1", "m1", []string{"Le", "chien", "dort"}); a == c {
		t.Fatalf("collision: sequences differentes -> meme racine")
	}
	if d := tokenChainRoot("j1", "m2", []string{"Le", "chat", "dort"}); a == d {
		t.Fatalf("racine independante du mineur (devrait differer via le domaine)")
	}
}

// INT-1 v1 brique 1 — la bisection localise le 1er token litigieux : préfixe accordé (graine + "Le"),
// divergence à "chat"/"chien" -> indice 2.
func TestFirstDivergentPrefix(t *testing.T) {
	hsA := prefixHexAll("j1", "m1", []string{"Le", "chat", "dort"})
	hsB := prefixHexAll("j1", "m1", []string{"Le", "chien", "dort"})
	if hsA[0] != hsB[0] || hsA[1] != hsB[1] {
		t.Fatalf("les prefixes accordes devraient matcher (graine + apres 'Le')")
	}
	if idx := firstDivergentPrefix(hsA, hsB); idx != 2 {
		t.Fatalf("divergence attendue a l'indice 2, obtenu %d", idx)
	}
	if idx := firstDivergentPrefix(hsA, hsA); idx != -1 {
		t.Fatalf("sequences identiques -> -1 attendu, obtenu %d", idx)
	}
}
