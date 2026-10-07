package keeper

import "testing"

// The chain accepts SIGNED embeddings — real sentence-transformer vectors — bounded in magnitude.
// This test lives inside the package because parseIntVec and cosineGE are unexported.
func TestParseIntVecSignedDOC13(t *testing.T) {
	if v := parseIntVec("3,-2,1"); v == nil || len(v) != 3 || v[1] != -2 {
		t.Fatalf("signed vector wrongly rejected: %v", v)
	}
	if parseIntVec("1,9999999") != nil { // above embedMaxMag (1<<20)
		t.Fatal("out-of-bounds positive magnitude wrongly accepted")
	}
	if parseIntVec("1,-9999999") != nil { // below -embedMaxMag
		t.Fatal("out-of-bounds negative magnitude wrongly accepted")
	}
}

// The integer cosine clusters SIGNED vectors correctly, and the dot<0 guard prevents a negative
// cosine from clearing the threshold through the squaring step.
func TestCosineGESignedDOC13(t *testing.T) {
	a := []int64{100, -50, 30}
	b := []int64{101, -49, 31} // nearly the same direction -> cosine close to 1
	if !cosineGE(a, b, 7000) {
		t.Fatal("close signed vectors should clear the 0.70 threshold")
	}
	c := []int64{-100, 50, -30} // opposite direction -> dot < 0
	if cosineGE(a, c, 7000) {
		t.Fatal("an opposite vector must NOT pass: the dot<0 guard is missing")
	}
}
