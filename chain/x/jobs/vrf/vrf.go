// Package vrf provides a real VERIFIABLE RANDOM FUNCTION for Dendra, based on the
// ECVRF-EDWARDS25519-SHA512-ELL2 suite (RFC 9381) as implemented by curve25519-voi, which is ALREADY an
// (indirect) dependency of the repository. It REPLACES the sha256 stub (CR-10) with a primitive where
// each key holder produces a pseudo-random output plus a PROOF anyone can verify with the PUBLIC key,
// without being able to influence it (cryptographic anti-grinding).
//
// Integration status, stated plainly: this package is the PRIMITIVE, verified by its tests against the
// library itself. Wiring it into committee selection and the availability challenge (anchoring one
// vrf_pubkey per miner; the proposer's VRF for the full multi-validator benefit) is the next documented
// increment (cf. docs/MODE-A-COMPLET.md). The current "deferred reveal via AppHash" remains a sound
// anti-grinding interim; this primitive is its final, verifiable form.
package vrf

import (
	"bytes"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"sort"

	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"
	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519/extra/ecvrf"
)

const (
	// ProofSize: size in bytes of a VRF proof (pi).
	ProofSize = ecvrf.ProofSize // 80
	// OutputSize: size in bytes of the VRF output (beta).
	OutputSize = ecvrf.OutputSize // 64
	// PublicKeySize / PrivateKeySize: sizes of the underlying Ed25519 keys.
	PublicKeySize  = ed25519.PublicKeySize  // 32
	PrivateKeySize = ed25519.PrivateKeySize // 64
)

// GenerateKey produces a VRF key pair (Ed25519). A nil `rand` means crypto/rand.
func GenerateKey(rand io.Reader) (ed25519.PublicKey, ed25519.PrivateKey, error) {
	if rand == nil {
		rand = cryptorand.Reader
	}
	return ed25519.GenerateKey(rand)
}

// Prove computes the VRF proof (pi, ProofSize bytes) for `alpha` under the secret key `sk`.
// Deterministic: (sk, alpha) always yields the same proof and the same beta output.
func Prove(sk ed25519.PrivateKey, alpha []byte) (pi []byte, err error) {
	if len(sk) != PrivateKeySize {
		return nil, fmt.Errorf("vrf: invalid private key (%d bytes, expected %d)", len(sk), PrivateKeySize)
	}
	// ecvrf.Prove panics on an invalid key -> recover, so the state machine never panics.
	defer func() {
		if r := recover(); r != nil {
			pi, err = nil, fmt.Errorf("vrf: prove failed: %v", r)
		}
	}()
	return ecvrf.Prove(sk, alpha), nil
}

// Verify checks a VRF proof. Returns (true, beta) when valid, (false, nil) otherwise. `beta`
// (OutputSize bytes) is the deterministic VRF output, usable as an unpredictable, verifiable seed.
func Verify(pk ed25519.PublicKey, alpha, pi []byte) (bool, []byte) {
	if len(pk) != PublicKeySize || len(pi) != ProofSize {
		return false, nil
	}
	return ecvrf.Verify(pk, pi, alpha)
}

// Output extracts the VRF output (beta) from a proof, without re-verifying. Use it only on a proof
// already checked by Verify; otherwise the output carries no guarantee.
func Output(pi []byte) ([]byte, error) {
	if len(pi) != ProofSize {
		return nil, fmt.Errorf("vrf: invalid proof size (%d, expected %d)", len(pi), ProofSize)
	}
	return ecvrf.ProofToHash(pi)
}

// AggregateSeedSize: size in bytes of the aggregated seed produced by AggregateBeacons.
const AggregateSeedSize = sha256.Size // 32

// aggregateDomain separates the hashing domain of the aggregation (cross-use collision resistance).
var aggregateDomain = []byte("dendra/vrf/aggregate/v1")

// AggregateBeacons combines the VRF outputs (beta) of several validators into ONE deterministic,
// unpredictable and unforgeable seed: seed = SHA-256( domain || Σ (len(beta_i) || beta_i) ) over the
// SORTED betas.
//
// Properties (tested):
//   - DETERMINISTIC AND ORDER-INDEPENDENT: the validator iteration order (which ABCI does not
//     guarantee) does not alter the seed — the betas are sorted lexicographically before hashing.
//   - LENGTH-PREFIXED: each beta is prefixed with its length (4 bytes, big endian), so concatenation is
//     unambiguous (two distinct splits cannot produce the same hashed input).
//   - DOMAIN-SEPARATED: the domain prefix prevents any collision with another use of SHA-256.
//
// No single actor controls the output: biasing the seed would require forging the VRF proofs of a
// sufficient fraction of the stake (each beta comes from an ECVRF proof verified upstream).
// Returns an error when NO beta is supplied, so the caller falls back explicitly (e.g. on the AppHash):
// a constant "seed" passing for randomness is never returned.
func AggregateBeacons(betas [][]byte) ([]byte, error) {
	if len(betas) == 0 {
		return nil, fmt.Errorf("vrf: aggregation impossible - no beta supplied")
	}
	cp := make([][]byte, len(betas))
	copy(cp, betas)
	sort.Slice(cp, func(i, j int) bool { return bytes.Compare(cp[i], cp[j]) < 0 })
	h := sha256.New()
	h.Write(aggregateDomain)
	var l [4]byte
	for _, b := range cp {
		binary.BigEndian.PutUint32(l[:], uint32(len(b)))
		h.Write(l[:])
		h.Write(b)
	}
	return h.Sum(nil), nil
}
