// Package vrf fournit une FONCTION ALÉATOIRE VÉRIFIABLE (VRF) réelle pour Dendra, basée sur la suite
// ECVRF-EDWARDS25519-SHA512-ELL2 (RFC 9381 / IETF draft) implémentée par curve25519-voi — DÉJÀ une
// dépendance (indirecte) du dépôt. Elle REMPLACE le stub sha256 (CR-10) par une primitive où chaque
// détenteur d'une clé produit une sortie pseudo-aléatoire + une PREUVE que quiconque peut vérifier
// avec la clé PUBLIQUE, sans pouvoir l'influencer (anti-grinding cryptographique).
//
// Statut d'intégration (HONNÊTE) : ce package est la PRIMITIVE, vérifiée par ses tests contre la vraie
// bibliothèque. Le câblage dans la sélection de comité / le défi de disponibilité (ancrage d'une
// vrf_pubkey par mineur ; VRF du proposant pour le plein bénéfice multi-validateur) est l'incrément
// suivant documenté (cf. docs/MODE-A-COMPLET.md). La « révélation différée via AppHash » actuelle
// reste un bon intérim anti-grinding ; cette primitive en est la version finale, vérifiable.
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
	// ProofSize : taille (octets) d'une preuve VRF (pi).
	ProofSize = ecvrf.ProofSize // 80
	// OutputSize : taille (octets) de la sortie VRF (beta).
	OutputSize = ecvrf.OutputSize // 64
	// PublicKeySize / PrivateKeySize : tailles des clés Ed25519 sous-jacentes.
	PublicKeySize  = ed25519.PublicKeySize  // 32
	PrivateKeySize = ed25519.PrivateKeySize // 64
)

// GenerateKey produit une paire de clés VRF (Ed25519). `rand` nil -> crypto/rand.
func GenerateKey(rand io.Reader) (ed25519.PublicKey, ed25519.PrivateKey, error) {
	if rand == nil {
		rand = cryptorand.Reader
	}
	return ed25519.GenerateKey(rand)
}

// Prove calcule la preuve VRF (pi, ProofSize octets) pour `alpha` sous la clé secrète `sk`.
// Déterministe : (sk, alpha) -> toujours la même preuve et la même sortie beta.
func Prove(sk ed25519.PrivateKey, alpha []byte) (pi []byte, err error) {
	if len(sk) != PrivateKeySize {
		return nil, fmt.Errorf("vrf: clé privée invalide (%d octets, attendu %d)", len(sk), PrivateKeySize)
	}
	// ecvrf.Prove panique sur clé invalide -> on récupère pour ne jamais faire paniquer la state machine.
	defer func() {
		if r := recover(); r != nil {
			pi, err = nil, fmt.Errorf("vrf: prove a échoué: %v", r)
		}
	}()
	return ecvrf.Prove(sk, alpha), nil
}

// Verify vérifie une preuve VRF. Renvoie (true, beta) si valide, (false, nil) sinon. `beta`
// (OutputSize octets) est la sortie VRF déterministe, utilisable comme graine imprévisible+vérifiable.
func Verify(pk ed25519.PublicKey, alpha, pi []byte) (bool, []byte) {
	if len(pk) != PublicKeySize || len(pi) != ProofSize {
		return false, nil
	}
	return ecvrf.Verify(pk, pi, alpha)
}

// Output extrait la sortie VRF (beta) d'une preuve (sans re-vérifier). N'utiliser que sur une preuve
// déjà vérifiée par Verify (sinon la sortie n'a aucune garantie).
func Output(pi []byte) ([]byte, error) {
	if len(pi) != ProofSize {
		return nil, fmt.Errorf("vrf: taille de preuve invalide (%d, attendu %d)", len(pi), ProofSize)
	}
	return ecvrf.ProofToHash(pi)
}


// AggregateSeedSize : taille (octets) de la graine agrégée produite par AggregateBeacons.
const AggregateSeedSize = sha256.Size // 32

// aggregateDomain sépare le domaine de hachage de l'agrégation (anti collision inter-usage).
var aggregateDomain = []byte("dendra/vrf/aggregate/v1")

// AggregateBeacons combine les sorties VRF (beta) de plusieurs validateurs en UNE graine déterministe,
// imprévisible et infalsifiable : seed = SHA-256( domain ‖ Σ (len(beta_i) ‖ beta_i) ) sur les beta TRIÉS.
//
// Propriétés (testées) :
//   - DÉTERMINISTE + ORDRE-INDÉPENDANTE : l'ordre d'itération des validateurs (non garanti par ABCI)
//     n'altère pas la graine — les beta sont triés lexicographiquement avant hachage.
//   - LENGTH-PREFIXED : chaque beta est préfixé de sa longueur (4 octets big-endian) → pas d'ambiguïté
//     de concaténation (deux découpages distincts ne peuvent pas produire la même entrée hachée).
//   - DOMAIN-SÉPARÉE : le préfixe de domaine empêche toute collision avec un autre usage de SHA-256.
//
// Aucun acteur unique ne contrôle la sortie : biaiser la graine exigerait de forger les preuves VRF
// d'une fraction suffisante du stake (chaque beta provient d'une preuve ECVRF vérifiée en amont).
// Renvoie une erreur si AUCUN beta n'est fourni (le caller se replie alors, p.ex. sur l'AppHash) — ainsi
// on ne renvoie JAMAIS une "graine" constante qui passerait pour de l'aléa.
func AggregateBeacons(betas [][]byte) ([]byte, error) {
	if len(betas) == 0 {
		return nil, fmt.Errorf("vrf: agrégation impossible — aucun beta fourni")
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
