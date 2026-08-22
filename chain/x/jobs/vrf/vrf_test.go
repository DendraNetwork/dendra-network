package vrf

import (
	"bytes"
	"testing"
)

// Round trip and determinism: a valid proof verifies, and the beta output is deterministic.
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
		t.Fatalf("proof size %d != %d", len(pi), ProofSize)
	}
	ok, beta := Verify(pk, alpha, pi)
	if !ok {
		t.Fatal("Verify rejected a VALID proof")
	}
	if len(beta) != OutputSize {
		t.Fatalf("output size %d != %d", len(beta), OutputSize)
	}
	// determinism: proving again yields the same beta output.
	pi2, _ := Prove(sk, alpha)
	ok2, beta2 := Verify(pk, alpha, pi2)
	if !ok2 || !bytes.Equal(beta, beta2) {
		t.Fatal("VRF output not deterministic for the same (sk, alpha)")
	}
	// Output(pi) == beta.
	b3, err := Output(pi)
	if err != nil || !bytes.Equal(b3, beta) {
		t.Fatalf("Output(pi) != beta (err=%v)", err)
	}
}

// Anti-grinding: a proof produced for alpha-A does NOT validate for alpha-B.
func TestVerifyRejectsTamperedAlpha(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	pi, _ := Prove(sk, []byte("alpha-A"))
	if ok, _ := Verify(pk, []byte("alpha-B"), pi); ok {
		t.Fatal("accepted a proof under a DIFFERENT alpha (grinding possible)")
	}
}

// Integrity: a tampered proof is rejected.
func TestVerifyRejectsTamperedProof(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	alpha := []byte("alpha")
	pi, _ := Prove(sk, alpha)
	pi[0] ^= 0xff
	if ok, _ := Verify(pk, alpha, pi); ok {
		t.Fatal("accepted a TAMPERED proof")
	}
}

// Key binding: a proof does not validate under a DIFFERENT public key.
func TestVerifyRejectsWrongKey(t *testing.T) {
	_, sk, _ := GenerateKey(nil)
	pk2, _, _ := GenerateKey(nil)
	alpha := []byte("alpha")
	pi, _ := Prove(sk, alpha)
	if ok, _ := Verify(pk2, alpha, pi); ok {
		t.Fatal("accepted a proof under the WRONG public key")
	}
}

// Robustness: inputs of invalid sizes are rejected cleanly, without panicking.
func TestVerifyRejectsBadSizes(t *testing.T) {
	pk, sk, _ := GenerateKey(nil)
	pi, _ := Prove(sk, []byte("a"))
	if ok, _ := Verify(pk[:10], []byte("a"), pi); ok {
		t.Fatal("truncated public key accepted")
	}
	if ok, _ := Verify(pk, []byte("a"), pi[:10]); ok {
		t.Fatal("truncated proof accepted")
	}
	if _, err := Prove(sk[:10], []byte("a")); err == nil {
		t.Fatal("truncated private key: Prove should have failed")
	}
}

// Aggregation is deterministic AND independent of the validators' iteration order.
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
		t.Fatalf("aggregation depends on order: %x != %x", s1, s2)
	}
	if len(s1) != AggregateSeedSize {
		t.Fatalf("seed size = %d, expected %d", len(s1), AggregateSeedSize)
	}
}

// Sensitive to any change of a beta; the length prefix removes concatenation ambiguity; an empty set
// is an error, never a seed.
func TestAggregateBeaconsSensitivityAndEmpty(t *testing.T) {
	base, _ := AggregateBeacons([][]byte{[]byte("x"), []byte("y")})
	diff, _ := AggregateBeacons([][]byte{[]byte("x"), []byte("z")})
	if bytes.Equal(base, diff) {
		t.Fatalf("seed insensitive to a change of beta")
	}
	s1, _ := AggregateBeacons([][]byte{[]byte("ab"), []byte("c")})
	s2, _ := AggregateBeacons([][]byte{[]byte("a"), []byte("bc")})
	if bytes.Equal(s1, s2) {
		t.Fatalf("concatenation ambiguity (length prefix broken)")
	}
	if _, err := AggregateBeacons(nil); err == nil {
		t.Fatalf("an empty set must fail (no fabricated seed)")
	}
}

// End to end: 3 REAL VRF proofs -> verified betas -> aggregated into a reproducible seed.
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
			t.Fatalf("verify failed")
		}
		betas = append(betas, beta)
	}
	s1, err := AggregateBeacons(betas)
	if err != nil {
		t.Fatalf("agg: %v", err)
	}
	s2, _ := AggregateBeacons(betas)
	if !bytes.Equal(s1, s2) {
		t.Fatalf("aggregation not deterministic over real proofs")
	}
}
