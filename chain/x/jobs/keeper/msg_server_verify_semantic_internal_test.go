package keeper

import "testing"

// White-box tests (package keeper) of the two semantic-verification helpers: `cosineGE`, whose
// comparison runs on math/big so that large vectors cannot overflow, and `parseIntVec`, whose bounds
// decide what an untrusted embedding is allowed to be. Both are pure, so they need no fixture.

func TestCosineGE(t *testing.T) {
	cases := []struct {
		name string
		a, b []int64
		tbps int64
		want bool
	}{
		{"identical (cos=1)", []int64{3, 0, 4}, []int64{3, 0, 4}, 7000, true},
		{"orthogonal (cos=0)", []int64{1, 0, 0}, []int64{0, 0, 1}, 7000, false},
		{"different lengths", []int64{1, 0}, []int64{1, 0, 0}, 7000, false},
		{"empty", []int64{}, []int64{}, 7000, false},
		{"below threshold", []int64{1, 0}, []int64{1, 1}, 7100, false},      // cos=1/sqrt2~0.707 < 0.71
		{"at or above threshold", []int64{1, 0}, []int64{1, 1}, 7000, true}, // 0.707 >= 0.70
	}
	for _, c := range cases {
		if got := cosineGE(c.a, c.b, c.tbps); got != c.want {
			t.Errorf("%s: cosineGE=%v, want %v", c.name, got, c.want)
		}
	}
	// Large vectors whose squared dot product WOULD OVERFLOW int64 (dot^2*1e8 ~ 2^92*1e8): the
	// big.Int path must still report cos=1. An overflow here would silently invert a verdict.
	big := make([]int64, 64)
	for i := range big {
		big[i] = embedMaxMag
	}
	if !cosineGE(big, big, 7000) {
		t.Error("identical large vectors must give cos=1 (no overflow)")
	}
}

func TestParseIntVec(t *testing.T) {
	if v := parseIntVec("1,2,0,3"); v == nil || len(v) != 4 {
		t.Errorf("valid vector rejected: %v", v)
	}
	// Dimension above embedMaxDim -> nil: an unbounded vector is an unbounded allocation.
	long := make([]byte, 0, 2*(embedMaxDim+1))
	for i := 0; i < embedMaxDim+1; i++ {
		if i > 0 {
			long = append(long, ',')
		}
		long = append(long, '1')
	}
	if parseIntVec(string(long)) != nil {
		t.Error("a dimension above embedMaxDim must be rejected")
	}
	// A NEGATIVE component is ACCEPTED (real embeddings are signed) as long as its magnitude is bounded.
	if v := parseIntVec("1,-2,3"); v == nil || v[1] != -2 {
		t.Errorf("a bounded negative component must be accepted: %v", v)
	}
	// Negative magnitude out of bounds -> nil.
	if parseIntVec("1,-99999999999") != nil {
		t.Error("a component below -embedMaxMag must be rejected")
	}
	// Excessive magnitude -> nil.
	if parseIntVec("1,99999999999") != nil {
		t.Error("a component above embedMaxMag must be rejected")
	}
	// Non-numeric -> nil.
	if parseIntVec("1,x,3") != nil {
		t.Error("a non-numeric component must be rejected")
	}
}
