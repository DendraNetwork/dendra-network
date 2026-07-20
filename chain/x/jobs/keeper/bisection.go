package keeper

import (
	"crypto/sha256"
	"encoding/hex"
)

// INT-1 v1 (bisection sur tokens — docs/DISPUTE-FRAUDPROOF.md §9) — BRIQUE 1 : la PRIMITIVE hash-chain.
//
// Foundation du futur jeu de bisection (PAS encore câblée dans un handler ; le jeu interactif
// OpenBisection/BisectStep/ArbitrateStep = lot suivant). Pure + déterministe (sha256) → identique sur tous
// les validateurs. Confidentialité : seuls des HASH circulent (jamais les tokens en clair).
//
// Précondition mesurée par `services/measure_determinism.py` : le décodage greedy doit être
// reproductible intra-hw_class pour que deux honnêtes produisent la MÊME chaîne (mesuré 1.000 sur RTX 3070).

// bisectDomain — séparation de domaine pour les hash de tokens (anti-collision avec d'autres usages sha256).
const bisectDomain = "dendra/bisect/v1"

// tokenPrefixHashes — chaîne de hash des PRÉFIXES de la séquence de tokens d'un (job, mineur) :
//
//	h[0]   = H(domaine | jobId | 0x00 | minerId)                  (graine, lie la chaîne au job ET au mineur)
//	h[i+1] = H(h[i] | 0x00 | token_i)
//
// Renvoie h[0..len(tokens)] (len+1 entrées) ; h[i] ENGAGE les i premiers tokens. La RACINE = h[len].
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

// tokenChainRoot — RACINE de la hash-chain (engage TOUTE la séquence). C'est ce qu'un mineur ancrerait dans
// son commit (champ futur `token_chain_root`) pour pouvoir être mis au défi par bisection.
func tokenChainRoot(jobId, minerId string, tokens []string) string {
	hs := tokenPrefixHashes(jobId, minerId, tokens)
	return hex.EncodeToString(hs[len(hs)-1])
}

// tokenPrefixHashAt — hash du préfixe à l'indice i (0..len). C'est ce qu'une partie RÉVÈLE à chaque tour de
// bisection (le vérificateur resserre [lo,hi] selon que les préfixes s'accordent ou non à l'indice médian).
func tokenPrefixHashAt(jobId, minerId string, tokens []string, i int) string {
	hs := tokenPrefixHashes(jobId, minerId, tokens)
	if i < 0 || i >= len(hs) {
		return ""
	}
	return hex.EncodeToString(hs[i])
}

// firstDivergentPrefix — CŒUR de la bisection (forme directe, pour test/preuve off-chain) : étant donné deux
// listes de prefix-hashes qui s'accordent au début et divergent ensuite, renvoie le 1er indice de désaccord
// (= le token litigieux à arbitrer). -1 si identiques jusqu'au min. Le jeu ON-CHAIN obtient le même indice
// en O(log n) tours SANS poster toute la liste (il ne compare que le hash médian à chaque tour).
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
