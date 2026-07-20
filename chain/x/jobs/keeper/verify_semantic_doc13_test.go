package keeper

import "testing"

// DOC-13 — la chaine accepte des embeddings SIGNÉS (vrais vecteurs sentence-transformers),
// bornés en magnitude. Test interne (parseIntVec/cosineGE sont non exportés).
func TestParseIntVecSignedDOC13(t *testing.T) {
	if v := parseIntVec("3,-2,1"); v == nil || len(v) != 3 || v[1] != -2 {
		t.Fatalf("vecteur signe rejete a tort: %v", v)
	}
	if parseIntVec("1,9999999") != nil { // > embedMaxMag (1<<20)
		t.Fatal("magnitude positive hors borne acceptee a tort")
	}
	if parseIntVec("1,-9999999") != nil { // < -embedMaxMag
		t.Fatal("magnitude negative hors borne acceptee a tort")
	}
}

// Le cosinus entier clusterise correctement des vecteurs SIGNÉS, et le garde dot<0 empeche
// qu'un cosinus negatif passe le seuil via le carre.
func TestCosineGESignedDOC13(t *testing.T) {
	a := []int64{100, -50, 30}
	b := []int64{101, -49, 31} // ~meme direction -> cos ~1
	if !cosineGE(a, b, 7000) {
		t.Fatal("vecteurs signes proches: devraient passer le seuil 0.70")
	}
	c := []int64{-100, 50, -30} // direction opposee -> dot<0
	if cosineGE(a, c, 7000) {
		t.Fatal("vecteur oppose: ne devrait PAS passer (garde dot<0)")
	}
}
