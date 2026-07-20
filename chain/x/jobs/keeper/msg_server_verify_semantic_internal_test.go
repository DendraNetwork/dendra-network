package keeper

import "testing"

// Tests BLANCS (package keeper) des helpers corrigés par l'audit : cosineGE (GO-05, math/big)
// et parseIntVec (GO-15, bornes). Purs (sans keeper/fixture) -> runnables, sans risque.

func TestCosineGE(t *testing.T) {
	cases := []struct {
		name string
		a, b []int64
		tbps int64
		want bool
	}{
		{"identiques (cos=1)", []int64{3, 0, 4}, []int64{3, 0, 4}, 7000, true},
		{"orthogonaux (cos=0)", []int64{1, 0, 0}, []int64{0, 0, 1}, 7000, false},
		{"longueurs differentes (GO-15)", []int64{1, 0}, []int64{1, 0, 0}, 7000, false},
		{"vide", []int64{}, []int64{}, 7000, false},
		{"sous le seuil", []int64{1, 0}, []int64{1, 1}, 7100, false},     // cos=1/sqrt2~0.707 < 0.71
		{"au-dessus du seuil", []int64{1, 0}, []int64{1, 1}, 7000, true}, // 0.707 >= 0.70
	}
	for _, c := range cases {
		if got := cosineGE(c.a, c.b, c.tbps); got != c.want {
			t.Errorf("%s: cosineGE=%v, want %v", c.name, got, c.want)
		}
	}
	// GO-05 : gros vecteurs qui DEBORDERAIENT l'int64 (dot^2*1e8 ~ 2^92*1e8) -> big.Int donne cos=1.
	big := make([]int64, 64)
	for i := range big {
		big[i] = embedMaxMag
	}
	if !cosineGE(big, big, 7000) {
		t.Error("GO-05: gros vecteurs identiques -> cos=1 attendu (pas d'overflow)")
	}
}

func TestParseIntVec(t *testing.T) {
	if v := parseIntVec("1,2,0,3"); v == nil || len(v) != 4 {
		t.Errorf("vecteur valide rejete: %v", v)
	}
	// GO-15 : dimension > embedMaxDim -> nil
	long := make([]byte, 0, 2*(embedMaxDim+1))
	for i := 0; i < embedMaxDim+1; i++ {
		if i > 0 {
			long = append(long, ',')
		}
		long = append(long, '1')
	}
	if parseIntVec(string(long)) != nil {
		t.Error("GO-15: dimension > embedMaxDim devrait etre rejetee")
	}
	// DOC-13 : composante negative ACCEPTÉE (vrais embeddings signés), tant que bornée en magnitude.
	if v := parseIntVec("1,-2,3"); v == nil || v[1] != -2 {
		t.Errorf("DOC-13: composante negative bornee devrait etre acceptee: %v", v)
	}
	// DOC-13 : magnitude negative HORS borne -> nil
	if parseIntVec("1,-99999999999") != nil {
		t.Error("DOC-13: composante < -embedMaxMag devrait etre rejetee")
	}
	// GO-15 : magnitude excessive -> nil
	if parseIntVec("1,99999999999") != nil {
		t.Error("GO-15: composante > embedMaxMag devrait etre rejetee")
	}
	// non numerique -> nil
	if parseIntVec("1,x,3") != nil {
		t.Error("non numerique devrait etre rejete")
	}
}
