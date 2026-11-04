package keeper

import (
	"context"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

// ExpectedWeights returns the weights_sha256 anchored in the registry for `id`, and true when the
// model is registered and active. x/jobs uses it to bind the model_id declared in a commit to the
// expected weights artifact: a commit whose weights_hash does not match is refused. An empty
// weights_sha256 (a devnet placeholder) means "no anchor", and x/jobs then requires nothing.
func (k Keeper) ExpectedWeights(ctx context.Context, id string) (string, bool) {
	m, err := k.Models.Get(ctx, id)
	if err != nil || !m.Active {
		return "", false
	}
	return m.WeightsSha256, true
}

// ActiveWithoutAnchor returns the sorted ids of the active models whose weights_sha256 is empty.
//
// An active model with no anchor passes through ExpectedWeights as "no anchor", so x/jobs requires no
// correspondence between the model_id declared in a commit and the artifact actually served. That is
// a legitimate state while the registry is not enforced (a development placeholder), and it stops
// being one when the chain announces that it enforces the registry. This list lets that inconsistent
// combination be refused when the genesis loads, instead of being discovered in use.
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
