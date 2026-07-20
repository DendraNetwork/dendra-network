package keeper

import "context"

// ExpectedWeights — renvoie le weights_sha256 ANCRE au registre pour `id`, et true si le modele est
// enregistre ET actif. Utilise par x/jobs (NEW-MR-03 / audit v5) pour LIER le model_id declare dans un
// commit a l'artefact de poids attendu : un commit dont le weights_hash ne correspond pas est refuse.
// Un weights_sha256 vide (placeholder devnet) signifie "pas d'ancre" -> x/jobs n'exige alors rien.
func (k Keeper) ExpectedWeights(ctx context.Context, id string) (string, bool) {
	m, err := k.Models.Get(ctx, id)
	if err != nil || !m.Active {
		return "", false
	}
	return m.WeightsSha256, true
}
