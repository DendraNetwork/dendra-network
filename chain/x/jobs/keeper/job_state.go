package keeper

import "strings"

// jobIsPaid — prédicat anti-rejeu de RÈGLEMENT, PARTAGÉ par tous les chemins (machine d'état en chaîne
// de caractères, en attendant un enum proto — audit v2 GO-08 / NEW-GO-35).
//
// Sans prédicat commun, un job réglé par UN chemin n'était pas détecté par un autre : `settle_pay`
// marque `+settled` mais `payout`/`settle_semantic` ne testaient que `+paid` → DOUBLE-PAIEMENT ; et
// `settle_job` testait `== "settled"` (strict) → un job `open+paid` passait → inflation de `Demand`.
// On unifie : un job est « déjà réglé » dès qu'il porte `paid` OU `settled`, quel que soit le chemin.
//
// `verified` / `finalized` = VERDICT (vérification / finalisation), PAS paiement → volontairement HORS
// de ce prédicat (un job vérifié/finalisé peut encore être payé une fois).
func jobIsPaid(state string) bool {
	return strings.Contains(state, "paid") || strings.Contains(state, "settled")
}

// jobIsDisputed (INT-1 v0) — le verdict du job a-t-il déjà été contesté ? (anti-rejeu de dispute :
// une dispute par job). Marqueur ajouté à l'état (`...+disputed`), comme `paid`/`settled`.
func jobIsDisputed(state string) bool {
	return strings.Contains(state, "disputed")
}

// jobIsResolved (INT-1 v0 inc.2) — la dispute du job a-t-elle déjà été résolue ? (anti-rejeu de résolution)
func jobIsResolved(state string) bool {
	return strings.Contains(state, "resolved")
}

// jobIsClawed (ADR-028 anti-évasion) — le paiement optimiste a-t-il été REPRIS (clawback) parce que le
// primaire est resté SILENCIEUX (n'a jamais ancré sa révélation `<jobId>__reveal__<primId>` avant l'échéance) ?
// Un primaire payé qui ne révèle pas est le SUSPECT (il a déjà encaissé) : silence = triche par défaut,
// pas innocence. Marqueur informatif posé À CÔTÉ de "+resolved" (l'anti-rejeu reste porté par "resolved").
func jobIsClawed(state string) bool {
	return strings.Contains(state, "clawed")
}

// jobIsOptimistic (ADR-025) — le job a-t-il été réglé en mode OPTIMISTE k=1 ? (marqueur "optimistic" posé
// par settleOptimistic). Un tel job a un PRIMAIRE PAYÉ sans slash initial -> l'audit le juge par
// comparaison au comité frais (slash dur si divergence), contrairement au k=3 redondant (déjà tranché).
func jobIsOptimistic(state string) bool {
	return strings.Contains(state, "optimistic")
}
