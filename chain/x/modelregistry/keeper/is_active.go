package keeper

import "context"

// IsActive — vrai si le modèle `id` est enregistré ET actif. Utilisé par x/jobs (incrément C)
// pour gater l'ancrage d'un commit sur un modèle réellement attesté on-chain.
func (k Keeper) IsActive(ctx context.Context, id string) bool {
	m, err := k.Models.Get(ctx, id)
	if err != nil {
		return false
	}
	return m.Active
}
