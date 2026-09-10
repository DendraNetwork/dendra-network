package keeper

import (
	"context"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

// ExpectedWeights returns the weights_sha256 ANCHORED in the registry for `id`, and true when the
// model is registered AND active. Used by x/jobs to BIND the model_id declared in a commit to the
// expected weights artifact: a commit whose weights_hash does not match is refused. An empty
// weights_sha256 (a devnet placeholder) means "no anchor", and x/jobs then requires nothing.
func (k Keeper) ExpectedWeights(ctx context.Context, id string) (string, bool) {
	m, err := k.Models.Get(ctx, id)
	if err != nil || !m.Active {
		return "", false
	}
	return m.WeightsSha256, true
}

// ActiveWithoutAnchor returns the sorted ids of the ACTIVE models whose weights_sha256 is empty.
//
// An active model with no anchor passes through ExpectedWeights as "no anchor", so x/jobs requires NO
// correspondence between the model_id declared in a commit and the artifact actually served. That is a
// legitimate state while the registry is not ENFORCED (a development placeholder); it stops being one
// the moment the chain announces that it enforces the registry. This list is what allows the
// inconsistent combination to be refused when the genesis loads, rather than discovered in use.
func (k Keeper) ActiveWithoutAnchor(ctx context.Context) ([]string, error) {
	var unanchored []string
	if err := k.Models.Walk(ctx, nil, func(id string, m types.Model) (bool, error) {
		if m.Active && strings.TrimSpace(m.WeightsSha256) == "" {
			unanchored = append(unanchored, id)
		}
		return false, nil
	}); err != nil {
		return nil, err
	}
	sort.Strings(unanchored)
	return unanchored, nil
}
