// emission.go — CŒUR de l'émission depuis la Réserve (TK-02 / ADR-023). Vit dans `package keeper`.
//
// PUR + DÉTERMINISTE : tout en ENTIERS (udndr, bps = 1/10000), AUCUN flottant -> reproductible
// inter-validateurs (exigence consensus). Spec de référence = tokenomics/tokenomics_v5.py
// (`simulate_v5`). À intégrer dans un vrai module `x/emission` scaffoldé par ignite : un hook
// d'ÉPOQUE (BeginBlock tous les EpochBlocks) appelle EpochRelease, crédite les 3 pools
// (travail / disponibilité / sécurité) et décrémente la Réserve via le bank keeper.
//
// INVARIANTS (verrouillés par emission_test.go) :
//   - JAMAIS de mint : Released <= reserve  -> l'offre reste <= 10 M (on ne LIBÈRE que du pré-alloué).
//   - Réserve DÉCROISSANTE : NewReserve <= reserve.
//   - Conservation : Work + Avail + Security == Released.
//   - Demande-gate (ADR-017) : l'inutilisé du flux travail RESTE en Réserve (anti-self-dealing).
//
// PROD : remplacer `uint64` par `cosmossdk.io/math.Int` (cf. audit GO-18) pour des montants
// arbitraires sans risque d'overflow ; ici uint64 suffit pour la Réserve 10^13 udndr.
package keeper

// Params — gouvernables (en bps). Défauts = v5 (ADR-021).
type Params struct {
	ReserveReleaseBps uint64 // 2200 = 22 %/an de la Réserve RESTANTE (courbe décroissante)
	WorkSplitBps      uint64 // 5000 = 50 % du release
	AvailSplitBps     uint64 // 2000 = 20 % ; la SÉCURITÉ = le reste (≈30 %)
	WorkGateBps       uint64 // 15000 = 1,5× la demande NON-RÉCUPÉRABLE (plafond subvention travail)
}

// DefaultParams — valeurs v5.
func DefaultParams() Params {
	return Params{ReserveReleaseBps: 2200, WorkSplitBps: 5000, AvailSplitBps: 2000, WorkGateBps: 15000}
}

// Release — résultat d'une époque.
type Release struct {
	Released   uint64 // total sorti de la Réserve cette époque
	Work       uint64 // flux TRAVAIL (demande-gated)
	Avail      uint64 // flux DISPONIBILITÉ
	Security   uint64 // flux SÉCURITÉ
	NewReserve uint64 // Réserve après libération
}

// mulBps = x * bps / 10000 en entiers (déterministe).
func mulBps(x, bps uint64) uint64 { return x * bps / 10000 }

// EpochRelease libère UNE époque depuis `reserve` (udndr). `nonRecDemand` (udndr) = la demande
// NON-RÉCUPÉRABLE de l'époque (burn + trésorerie + dev) — elle PLAFONNE la subvention travail
// (un mineur ne peut pas se subventionner au-delà de la demande réelle qu'il ne récupère pas).
func EpochRelease(reserve, nonRecDemand uint64, p Params) Release {
	release := mulBps(reserve, p.ReserveReleaseBps) // 22 % du restant
	workPool := mulBps(release, p.WorkSplitBps)      // 50 %
	avail := mulBps(release, p.AvailSplitBps)         // 20 %
	security := release - workPool - avail            // reste EXACT (≈30 %) -> pas de perte d'arrondi

	work := workPool // demande-gate : on borne à WorkGate × demande non-récupérable
	if capWork := mulBps(nonRecDemand, p.WorkGateBps); work > capWork {
		work = capWork
	}
	released := work + avail + security // l'inutilisé (workPool - work) RESTE en Réserve
	return Release{
		Released:   released,
		Work:       work,
		Avail:      avail,
		Security:   security,
		NewReserve: reserve - released,
	}
}
