package vrf

import (
	"bytes"
	"testing"
)

// Round-trip + déterminisme : une preuve valide se vérifie, et la sortie beta est déterministe.
func TestProveVerifyRoundTrip(t *testing.T) {
	pk, sk, err := GenerateKey(nil)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	alpha := []byte("dendra:committee:height=42")
	pi, err := Prove(sk, alpha)
	if err != nil {
		t.Fatalf("Prove: %v", err)
	}
	if len(pi) != ProofSize {
		t.Fatalf("taille preuve %d != %d", len(pi), ProofSize)
	}
	ok, beta := Verify(pk, alpha, pi)
	if !ok {
		t.Fatal("Verify a rejeté une preuve VALIDE")
	}
	if len(beta) != OutputSize {
		t.Fatalf("taille sortie %d != %d", len(beta), OutputSize)
	}
	// déterminisme : re-prouver donne la même sortie beta.
	pi2, _ := Prove(sk, alpha)
	ok2, beta2 := Verify(pk, alpha, pi2)
	if !ok2 || !bytes.Equal(beta, beta2) {
		t.Fatal("sortie VRF non déterministe pour (sk, alpha) identiques")
	}
	// Output(pi) == beta.
	b3, err := Output(pi)
	if err != nil || !bytes.Equal(b3, beta) {
		t.Fatalf("Output(pi) != beta (err=%v)", err)
	}
}

// Anti-grinding : une preuve produite pour alpha-A ne valide PAS pour alpha-B.
func TestVerifyRejectsTamperedAlpha(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	pi, _ := Prove(sk, []byte("alpha-A"))
	if ok, _ := Verify(pk, []byte("alpha-B"), pi); ok {
		t.Fatal("a accepté une preuve sur un alpha DIFFÉRENT (grinding possible)")
	}
}

// Intégrité : une preuve corrompue est rejetée.
func TestVerifyRejectsTamperedProof(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	alpha := []byte("alpha")
	pi, _ := Prove(sk, alpha)
	pi[0] ^= 0xff
	if ok, _ := Verify(pk, alpha, pi); ok {
		t.Fatal("a accepté une preuve CORROMPUE")
	}
}

// Liaison de clé : une preuve ne valide pas sous une AUTRE clé publique.
func TestVerifyRejectsWrongKey(t *testing.T) {
	_, sk, _ := GenerateKey(nil)
	pk2, _, _ := GenerateKey(nil)
	alpha := []byte("alpha")
	pi, _ := Prove(sk, alpha)
	if ok, _ := Verify(pk2, alpha, pi); ok {
		t.Fatal("a accepté une preuve sous une MAUVAISE clé publique")
	}
}

// Robustesse : entrées de tailles invalides -> rejet propre (pas de panique).
func TestVerifyRejectsBadSizes(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	pi, _ := Prove(sk, []byte("a"))
	if ok, _ := Verify(pk[:10], []byte("a"), pi); ok {
		t.Fatal("clé publique tronquée acceptée")
	}
	if ok, _ := Verify(pk, []byte("a"), pi[:10]); ok {
		t.Fatal("preuve tronquée acceptée")
	}
	if _, err := Prove(sk[:10], []byte("a")); err == nil {
		t.Fatal("clé privée tronquée: Prove aurait dû échouer")
	}
}


// L'agrégation est déterministe ET indépendante de l'ordre d'itération des validateurs.
func TestAggregateBeaconsOrderIndependent(t *testing.T) {
	a := []byte("beta-A-00000000")
	b := []byte("beta-B-11111111")
	c := []byte("beta-C-22222222")
	s1, err := AggregateBeacons([][]byte{a, b, c})
	if err != nil {
		t.Fatalf("agg1: %v", err)
	}
	s2, err := AggregateBeacons([][]byte{c, a, b})
	if err != nil {
		t.Fatalf("agg2: %v", err)
	}
	if !bytes.Equal(s1, s2) {
		t.Fatalf("agrégation dépend de l'ordre: %x != %x", s1, s2)
	}
	if len(s1) != AggregateSeedSize {
		t.Fatalf("taille graine = %d, attendu %d", len(s1), AggregateSeedSize)
	}
}

// Sensible au changement d'un beta ; length-prefix anti-ambiguïté ; ensemble vide = erreur.
func TestAggregateBeaconsSensitivityAndEmpty(t *testing.T) {
	base, _ := AggregateBeacons([][]byte{[]byte("x"), []byte("y")})
	diff, _ := AggregateBeacons([][]byte{[]byte("x"), []byte("z")})
	if bytes.Equal(base, diff) {
		t.Fatalf("graine insensible au changement de beta")
	}
	s1, _ := AggregateBeacons([][]byte{[]byte("ab"), []byte("c")})
	s2, _ := AggregateBeacons([][]byte{[]byte("a"), []byte("bc")})
	if bytes.Equal(s1, s2) {
		t.Fatalf("ambiguïté de concaténation (length-prefix cassé)")
	}
	if _, err := AggregateBeacons(nil); err == nil {
		t.Fatalf("ensemble vide aurait dû échouer (pas de fausse graine)")
	}
}

// Bout-en-bout : 3 VRAIES preuves VRF -> beta vérifiés -> agrégés en une graine reproductible.
func TestAggregateFromRealProofs(t *testing.T) {
	alpha := []byte("dendra:h=100:r=0")
	var betas [][]byte
	for i := 0; i < 3; i++ {
		pk, sk, err := GenerateKey(nil)
		if err != nil {
			t.Fatalf("key: %v", err)
		}
		pi, err := Prove(sk, alpha)
		if err != nil {
			t.Fatalf("prove: %v", err)
		}
		ok, beta := Verify(pk, alpha, pi)
		if !ok {
			t.Fatalf("verify a échoué")
		}
		betas = append(betas, beta)
	}
	s1, err := AggregateBeacons(betas)
	if err != nil {
		t.Fatalf("agg: %v", err)
	}
	s2, _ := AggregateBeacons(betas)
	if !bytes.Equal(s1, s2) {
		t.Fatalf("agrégation non déterministe sur preuves réelles")
	}
}
