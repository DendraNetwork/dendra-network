package keeper

import "testing"

// prefixHexAll returns every prefix digest (i=0..len) in hex.
func prefixHexAll(jobId, minerId string, toks []string) []string {
	out := make([]string, 0, len(toks)+1)
	for i := 0; i <= len(toks); i++ {
		out = append(out, tokenPrefixHashAt(jobId, minerId, toks, i))
	}
	return out
}

// The token-chain root must be deterministic, sensitive to a single token, and bound to the miner.
func TestTokenChainRoot(t *testing.T) {
	a := tokenChainRoot("j1", "m1", []string{"The", "cat", "sleeps"})
	b := tokenChainRoot("j1", "m1", []string{"The", "cat", "sleeps"})
	if a != b {
		t.Fatalf("root is not deterministic: %s != %s", a, b)
	}
	if c := tokenChainRoot("j1", "m1", []string{"The", "dog", "sleeps"}); a == c {
		t.Fatalf("collision: different sequences produced the same root")
	}
	if d := tokenChainRoot("j1", "m2", []string{"The", "cat", "sleeps"}); a == d {
		t.Fatalf("root is independent of the miner (the domain separator must make it differ)")
	}
}

// Bisection must locate the first disputed token: the prefixes agree (seed + "The"), then diverge at
// "cat"/"dog" -> index 2.
func TestFirstDivergentPrefix(t *testing.T) {
	hsA := prefixHexAll("j1", "m1", []string{"The", "cat", "sleeps"})
	hsB := prefixHexAll("j1", "m1", []string{"The", "dog", "sleeps"})
	if hsA[0] != hsB[0] || hsA[1] != hsB[1] {
		t.Fatalf("agreed prefixes should match (seed + after 'The')")
	}
	if idx := firstDivergentPrefix(hsA, hsB); idx != 2 {
		t.Fatalf("expected divergence at index 2, got %d", idx)
	}
	if idx := firstDivergentPrefix(hsA, hsA); idx != -1 {
		t.Fatalf("identical sequences -> expected -1, got %d", idx)
	}
}
