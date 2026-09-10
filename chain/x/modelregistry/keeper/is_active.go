package keeper

import "context"

// IsActive reports whether the model `id` is registered AND active. Used by x/jobs to gate the
// anchoring of a commit on a model that is actually attested on-chain.
func (k Keeper) IsActive(ctx context.Context, id string) bool {
	m, err := k.Models.Get(ctx, id)
	if err != nil {
		return false
	}
	return m.Active
}
