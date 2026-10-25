package keeper

import (
	"context"
	"fmt"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
// It sets the Params, then the allowed models: pinning at genesis, without a governance round.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	// No init path may install an unvalidated genesis, and `validate-genesis` is not an init path.
	//
	// `GenesisState.Validate()` is what `dendrad genesis validate-genesis` runs, but nothing forces a
	// chain to be started through that tool: a hand-made, imported or third-party genesis reaches this
	// function directly. Two states would then install silently, and neither can be left afterwards:
	//   - `audit_judge_model` containing whitespace. `Params.Validate` refuses it; once installed, the
	//     judge worker reads it and passes it as `--model-id` on every verdict, so with the registry
	//     enforced `IsActive` is false for that string and no audit ever concludes.
	//   - a model with an empty or duplicate id. Once installed, `ExportGenesis` re-emits it, so the
	//     chain can no longer produce a genesis that its own validator accepts.
	//
	// It refuses exactly what the validator refuses and nothing more: a model with no `weights_sha256`
	// still loads, on purpose, for the reason given below.
	if err := genState.Validate(); err != nil {
		return fmt.Errorf("invalid modelregistry genesis (refused at load time): %w", err)
	}
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}
	// The two write paths do not apply the same guard, and the gap is reported.
	//
	// `RegisterModel` (msg_server_model.go) refuses a model without `weights_sha256`, since that digest
	// binds a `model_id` declared in a commit to a real artifact. Genesis requires nothing. A model
	// written active with an empty digest therefore passes through `ExpectedWeights` as "no anchor", and
	// `x/jobs` then requires no correspondence at all: the miner announces the model and serves whatever
	// it likes.
	//
	// This path deliberately does not refuse. A development genesis does place models without a digest
	// (a devnet placeholder), and refusing would panic the start of a new chain, trading a missing guard
	// for a chain that does not boot. What was wrong was the silence: nothing said the binding was off.
	//
	// Precondition for an incentivized launch: this alert must no longer appear. While it does, the
	// property "the judge is constrained by the model" does not hold on-chain for those models.
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
			"SECURITY: active model(s) at genesis without weights_sha256 -> no model_id<->artifact binding: a miner can declare this model and serve something else. RegisterModel refuses this case; genesis does not. Fix it before any incentivized launch.",
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
