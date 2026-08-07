package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
// It sets the Params, then the allowed models: pinning at genesis, without a governance round.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}
	// A GUARD GAP BETWEEN THE TWO WRITE PATHS, made VISIBLE.
	//
	// `RegisterModel` (msg_server_model.go) REFUSES a model without `weights_sha256`: that digest is
	// what binds a `model_id` declared in a commit to a real artifact. Genesis requires nothing. A model
	// written ACTIVE with an EMPTY digest therefore passes through `ExpectedWeights` as "no anchor", and
	// `x/jobs` then requires NO correspondence at all: the miner announces the model and serves whatever
	// it likes.
	//
	// This path deliberately does not REFUSE. A development genesis does place models without a digest
	// (an assumed devnet placeholder), and refusing would panic the start-up of a new chain — replacing
	// a missing guard with a chain that does not boot. What was genuinely wrong was the SILENCE: nothing
	// said the binding was disarmed.
	//
	// PRECONDITION FOR AN INCENTIVIZED LAUNCH: this alert must no longer appear. While it does, the
	// property "the judge is constrained by the model" is not held on-chain for those models.
	var unanchored []string
	for _, m := range genState.Models {
		if err := k.Models.Set(ctx, m.Id, m); err != nil {
			return err
		}
		if m.Active && strings.TrimSpace(m.WeightsSha256) == "" {
			unanchored = append(unanchored, m.Id)
		}
	}
	if len(unanchored) > 0 {
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"SECURITY: ACTIVE model(s) at genesis WITHOUT weights_sha256 -> no model_id<->artifact binding: a miner can declare this model and serve something else. RegisterModel refuses this case; genesis does not. Fix BEFORE any incentivized launch.",
			"models", strings.Join(unanchored, ","), "count", len(unanchored))
	}
	return nil
}

// ExportGenesis returns the module's exported genesis.
func (k Keeper) ExportGenesis(ctx context.Context) (*types.GenesisState, error) {
	var err error

	genesis := types.DefaultGenesis()
	genesis.Params, err = k.Params.Get(ctx)
	if err != nil {
		return nil, err
	}

	// Export the registered models, so the genesis round-trip is complete.
	if err = k.Models.Walk(ctx, nil, func(_ string, m types.Model) (bool, error) {
		genesis.Models = append(genesis.Models, m)
		return false, nil
	}); err != nil {
		return nil, err
	}

	return genesis, nil
}
