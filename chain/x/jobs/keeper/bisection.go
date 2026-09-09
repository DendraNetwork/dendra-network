package keeper

import (
	"crypto/sha256"
	"encoding/hex"
)

// Token bisection (docs/DISPUTE-FRAUDPROOF.md §9) — the hash-chain PRIMITIVE.
//
// Foundation of the bisection game; not yet wired into a handler (the interactive
// OpenBisection/BisectStep/ArbitrateStep game comes separately). Pure and deterministic (sha256), so
// identical on every validator. Confidentiality: only HASHES circulate, never the tokens in clear.
//
// Precondition measured by `services/measure_determinism.py`: greedy decoding must be
// reproducible within a hardware class, so that two honest miners produce the SAME chain.

// bisectDomain separates the token hashes from every other sha256 use (domain separation).
const bisectDomain = "dendra/bisect/v1"

// tokenPrefixHashes builds the hash chain over the PREFIXES of a (job, miner) token sequence:
//
//	h[0]   = H(domain | jobId | 0x00 | minerId)   (seed, binding the chain to the job AND the miner)
//	h[i+1] = H(h[i] | 0x00 | token_i)
//
// Returns h[0..len(tokens)] (len+1 entries); h[i] COMMITS to the first i tokens. The ROOT is h[len].
func tokenPrefixHashes(jobId, minerId string, tokens []string) [][]byte {
	seed := sha256.Sum256([]byte(bisectDomain + "|" + jobId + "\x00" + minerId))
	out := make([][]byte, 0, len(tokens)+1)
	cur := seed[:]
	out = append(out, cur)
	for _, tok := range tokens {
		buf := make([]byte, 0, len(cur)+1+len(tok))
		buf = append(buf, cur...)
		buf = append(buf, 0)
		buf = append(buf, []byte(tok)...)
		n := sha256.Sum256(buf)
		cur = n[:]
		out = append(out, cur)
	}
	return out
}

// tokenChainRoot returns the ROOT of the hash chain, committing to the WHOLE sequence. This is what a
// miner anchors in its commit (`token_chain_root`) so that it can be challenged by bisection.
func tokenChainRoot(jobId, minerId string, tokens []string) string {
	hs := tokenPrefixHashes(jobId, minerId, tokens)
	return hex.EncodeToString(hs[len(hs)-1])
}

// tokenPrefixHashAt returns the prefix hash at index i (0..len). This is what a party REVEALS at each
// bisection round; the verifier narrows [lo,hi] depending on whether the prefixes agree at the midpoint.
func tokenPrefixHashAt(jobId, minerId string, tokens []string, i int) string {
	hs := tokenPrefixHashes(jobId, minerId, tokens)
	if i < 0 || i >= len(hs) {
		return ""
	}
	return hex.EncodeToString(hs[i])
}

// firstDivergentPrefix is the heart of the bisection in direct form (for tests and off-chain proofs):
// given two prefix-hash lists that agree at the start and diverge later, it returns the first index of
// disagreement, that is the disputed token to arbitrate; -1 when they are identical up to the shorter
// length. The ON-CHAIN game reaches the same index in O(log n) rounds WITHOUT posting the whole list:
// it only compares the midpoint hash at each round.
func firstDivergentPrefix(a, b []string) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return i
		}
	}
	if len(a) != len(b) {
		return n
	}
	return -1
}
