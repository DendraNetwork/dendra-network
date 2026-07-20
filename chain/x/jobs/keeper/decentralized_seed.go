package keeper

import (
	"context"
	"encoding/hex"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// SetDecentralizedSeed enregistre la graine VRF décentralisée (sortie agrégée des vote-extensions)
// pour une hauteur donnée. Appelée par le PreBlocker de l'app (E4, brique 4) — déterministe sur tous
// les nœuds (même commit injecté + mêmes clés ancrées + même agrégation).
func (k Keeper) SetDecentralizedSeed(ctx context.Context, height int64, seed []byte) error {
	return k.DecentralizedSeed.Set(ctx, height, seed)
}

// GetDecentralizedSeed renvoie la graine décentralisée à une hauteur (ok=false si absente).
func (k Keeper) GetDecentralizedSeed(ctx context.Context, height int64) ([]byte, bool) {
	s, err := k.DecentralizedSeed.Get(ctx, height)
	if err != nil {
		return nil, false
	}
	return s, true
}

// SetDecentralizedSeedContributors enregistre le NB de contributeurs (validateurs à preuve VRF valide) de la
// graine décentralisée à une hauteur. Posé par le PreBlocker (aggregateSeed). Sert au plancher anti-régression.
func (k Keeper) SetDecentralizedSeedContributors(ctx context.Context, height int64, n uint64) error {
	return k.DecentralizedSeedContributors.Set(ctx, height, n)
}

// GetDecentralizedSeedContributors renvoie le NB de contributeurs à une hauteur (0/false si absent).
func (k Keeper) GetDecentralizedSeedContributors(ctx context.Context, height int64) (uint64, bool) {
	n, err := k.DecentralizedSeedContributors.Get(ctx, height)
	if err != nil {
		return 0, false
	}
	return n, true
}

// MinVrfContributorPowerBps (LOT SCALING 2026-07-01, durci post-red-team 2026-07-02) — plancher DYNAMIQUE
// ⌈2N/3⌉ exprimé en PUISSANCE DE VOTE (bps de la puissance totale du dernier commit) : la graine décentralisée
// n'est digne de confiance que si les validateurs qui l'ont contribuée pèsent ≥ 2/3 du pouvoir — la même barre
// que le consensus BFT lui-même. Un plancher en CARDINAL serait griefable (sybil de validateurs à stake-poussière
// gonflant N -> ⌈2N/3⌉ inatteignable -> repli legacy permanent = anti-grinding éteint par un tiers) ; en POUVOIR,
// la poussière ne pèse rien. Const (gouvernable = champ proto à grouper à la prochaine régén, PLAN-REGEN §5).
// Ne mord QUE si committee_min_vrf_contributors>0 (l'arming reste gouverné ; dormant = v1 strict).
const MinVrfContributorPowerBps = 6667

// SetDecentralizedSeedContributorPower enregistre la part de puissance (bps) des contributeurs VRF valides
// à une hauteur. Posé par le PreBlocker au MÊME site que la graine (jamais l'un sans l'autre -> pas de staleness).
func (k Keeper) SetDecentralizedSeedContributorPower(ctx context.Context, height int64, bps uint64) error {
	return k.DecentralizedSeedContributorPower.Set(ctx, height, bps)
}

// GetDecentralizedSeedContributorPower renvoie la part de puissance (bps) à une hauteur (ok=false si absente).
func (k Keeper) GetDecentralizedSeedContributorPower(ctx context.Context, height int64) (uint64, bool) {
	bps, err := k.DecentralizedSeedContributorPower.Get(ctx, height)
	if err != nil {
		return 0, false
	}
	return bps, true
}

// DeleteDecentralizedSeedContributorPower purge une hauteur ANCIENNE (fenêtre glissante anti-bloat, cf. V8-N1).
func (k Keeper) DeleteDecentralizedSeedContributorPower(ctx context.Context, height int64) error {
	return k.DecentralizedSeedContributorPower.Remove(ctx, height)
}

// committeeBaseSeed (E4 brique 4b) renvoie la graine de BASE du comité. Si committee_seed_source==1
// (VRF DÉCENTRALISÉE) ET qu'une graine décentralisée existe à la hauteur courante (posée par le
// PreBlocker depuis les vote-extensions agrégées de TOUS les validateurs), on l'utilise (hex) — l'aléa
// de comité devient ainsi infalsifiable et non contrôlé par un acteur unique. Sinon -> `fallback`
// (comportement legacy : height:time à l'open, AppHash à la révélation). Défaut 0 = legacy => e2e intacte.
func (k Keeper) committeeBaseSeed(ctx context.Context, fallback string) string {
	seed, _ := k.committeeBaseSeedSourced(ctx, fallback)
	return seed
}

// committeeBaseSeedSourced — même chose, mais dit AUSSI si la graine rendue est la décentralisée
// (`true`) ou le repli (`false`).
//
// Pourquoi l'appelant a besoin de le savoir. Le repli vaut `AppHash(H-1):h` : le proposant du bloc h
// le connaît AVANT de proposer. Or `ProcessProposal` accepte une proposition SANS injection de
// vote-extensions. Un proposant hostile peut donc choisir, à chaque bloc qu'il propose, entre la
// graine décentralisée (imprévisible) et une graine qu'il a déjà calculée — et ne proposer que
// lorsque le tirage l'arrange. Le journal d'alerte existant décrit l'absence de graine comme un
// accident d'infrastructure ; il n'avait pas prévu l'omission DÉLIBÉRÉE.
//
// La réponse n'est pas de rejeter le bloc (rejeter en boucle arrêterait la chaîne pour une raison
// que l'attaquant contrôle) : c'est de NE RIEN TIRER quand la graine n'est pas décentralisée.
// L'omission cesse alors de rapporter, puisqu'elle ne déplace plus aucun tirage.
func (k Keeper) committeeBaseSeedSourced(ctx context.Context, fallback string) (string, bool) {
	p, err := k.Params.Get(ctx)
	if err != nil || p.CommitteeSeedSource != 1 {
		return fallback, false
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	if seed, ok := k.GetDecentralizedSeed(ctx, h); ok && len(seed) > 0 {
		// PLANCHER de contributeurs (internal audit 2026-06-26 ; DYNAMIQUE ⌈2N/3⌉ EN POUVOIR, lot scaling 2026-07-01/02) :
		// une graine PRÉSENTE mais SOUS-DÉCENTRALISÉE ne doit PAS tirer un comité en mode récompensé (sinon une
		// minorité contrôle l'aléa). Deux barres, TOUTES DEUX gatées sur param>0 (dormant = v1 strict) :
		//   (1) COUNT ≥ param (le plancher statique v1, inchangé) ;
		//   (2) PUISSANCE des contributeurs ≥ ⌈2/3⌉ du pouvoir du commit (MinVrfContributorPowerBps) — la barre
		//       BFT, insensible au sybil-poussière ; absente (graine d'un code antérieur) = échec VISIBLE.
		if min := p.CommitteeMinVrfContributors; min > 0 {
			if n, _ := k.GetDecentralizedSeedContributors(ctx, h); n < min {
				sdkCtx.Logger().Error("SECURITE: graine VRF decentralisee SOUS-DECENTRALISEE (contributeurs < plancher statique) -> repli LEGACY (anti-grinding INACTIF ce tirage) ; trop peu de validateurs ont ancre+fourni leur cle VRF.", "height", h, "contributors", n, "min_required", min)
				return fallback, false
			}
			if bps, ok := k.GetDecentralizedSeedContributorPower(ctx, h); !ok || bps < MinVrfContributorPowerBps {
				sdkCtx.Logger().Error("SECURITE: graine VRF decentralisee SOUS-PONDEREE (puissance des contributeurs < 2/3 du commit) -> repli LEGACY (anti-grinding INACTIF ce tirage) ; des validateurs LOURDS n'ont pas ancre/fourni leur cle VRF.", "height", h, "contributor_power_bps", bps, "min_required_bps", uint64(MinVrfContributorPowerBps))
				return fallback, false
			}
		}
		return hex.EncodeToString(seed), true
	}
	// BOOTSTRAP VRF (internal audit 2026-06-26) — anti-RÉGRESSION SILENCIEUSE : committee_seed_source=1 EXIGE une graine
	// décentralisée. Son ABSENCE = anti-grinding INACTIF pour CE tirage de comité -> on NE retombe JAMAIS en legacy
	// EN SILENCE. Alerte VISIBLE (le repli legacy reste, pour ne PAS bloquer la liveness — pas de halte) : cause
	// typique = aucun validateur n'a encore ancré sa clé VRF (register-validator-vrf-key) OU vote_extensions inactives.
	// Sur un testnet RÉCOMPENSÉ, ceci NE DOIT PAS persister (surveiller cette alerte = précondition d'ouverture).
	sdkCtx.Logger().Error("SECURITE: committee_seed_source=1 mais AUCUNE graine VRF decentralisee a cette hauteur -> repli LEGACY (anti-grinding INACTIF ce tirage). Un validateur a-t-il ancre sa cle VRF + vote_extensions actives ?", "height", h)
	return fallback, false
}

// SetBlockHash (VE-01) enregistre le hash du bloc à une hauteur (posé par le PreBlocker quand les
// vote-extensions sont actives). Sert d'alpha VRF lié au bloc + à la re-vérification déterministe.
func (k Keeper) SetBlockHash(ctx context.Context, height int64, hash []byte) error {
	return k.BlockHash.Set(ctx, height, hash)
}

// GetBlockHash (VE-01) renvoie le hash du bloc à une hauteur (ok=false si absent -> repli sur le chaînage).
func (k Keeper) GetBlockHash(ctx context.Context, height int64) ([]byte, bool) {
	h, err := k.BlockHash.Get(ctx, height)
	if err != nil {
		return nil, false
	}
	return h, true
}

// DeleteBlockHash (V8-N1) purge le hash d'une hauteur ANCIENNE (fenêtre glissante anti-bloat KV).
// No-op si absent. Seuls h et h-1 servent (alpha + repli de chaînage) -> purger loin derrière est sûr.
func (k Keeper) DeleteBlockHash(ctx context.Context, height int64) error {
	return k.BlockHash.Remove(ctx, height)
}

// DeleteDecentralizedSeed (V8-N1) purge une graine d'une hauteur ANCIENNE (fenêtre glissante anti-bloat).
// No-op si absente. La graine n'est lue qu'à la hauteur courante (comité) et à h-1 (repli alpha).
func (k Keeper) DeleteDecentralizedSeed(ctx context.Context, height int64) error {
	return k.DecentralizedSeed.Remove(ctx, height)
}
