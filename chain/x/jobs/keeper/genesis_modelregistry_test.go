package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Enforcing the registry only means something if the registry anchors an artefact.
//
// `enforce_model_registry` requires a commit to cite a registered, active model. The only thing binding
// that `model_id` to an artefact is the `weights_sha256` anchored in the registry: without an anchor,
// `ExpectedWeights` answers "nothing to verify" and the miner can declare one model while serving
// another. A genesis that arms enforcement while leaving active models unanchored announces a
// constraint that does not exist, and nothing signals it, since the commits are accepted.
//
// The guard is a cross-check: an active model without an anchor stays valid while enforcement is off
// (the development genesis). Refusing it outright would trade a missing guard for a chain that does not
// start.

func genesisWithParams(p types.Params) types.GenesisState {
	gs := types.DefaultGenesis()
	gs.Params = p
	return *gs
}

// (1) Registry enforced, active model without an anchor: refused at load, naming the model.
func TestInitGenesisRefusesEnforcedRegistryWithoutAnchor(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-no-anchor"] = true // active, no weights_sha256

	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	err := f.keeper.InitGenesis(f.ctx, genesisWithParams(p))
	require.Error(t, err,
		"genesis loaded while announcing that it enforces the registry with an unanchored active model: "+
			"for that model the model_id <-> artefact binding does not exist and the commit passes anyway")
	require.Contains(t, err.Error(), "llama-no-anchor",
		"the refusal must name the offending model, otherwise it has to be hunted down by hand")
}

// (2) Registry enforced, every active model anchored: accepted.
func TestInitGenesisAcceptsEnforcedRegistryWithAnchors(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-anchored"] = true
	f.modelReg.weights["llama-anchored"] = "deadbeef"
	f.modelReg.active["model-retired"] = false // inactive: no anchor required

	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.InitGenesis(f.ctx, genesisWithParams(p)))
}

// (3) Registry not enforced: unanchored models stay accepted (development genesis). Without this case
// the guard could become an unconditional refusal with nothing turning red.
func TestInitGenesisAcceptsUnanchoredModelsWhenTheRegistryIsNotEnforced(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-no-anchor"] = true

	p := types.DefaultParams()
	require.False(t, p.EnforceModelRegistry, "the default must not enforce the registry")
	require.NoError(t, f.keeper.InitGenesis(f.ctx, genesisWithParams(p)))
}

// (4) Every init path validates its params, not only `validate-genesis`. A hand-built genesis reaches
// `InitGenesis` directly, and the cross bounds of optimistic mode must hold there too.
func TestInitGenesisRefusesInvalidParams(t *testing.T) {
	f := initFixture(t)

	p := types.DefaultParams()
	p.VerificationMode = 1 // armed without any of the bounds the mode requires
	err := f.keeper.InitGenesis(f.ctx, genesisWithParams(p))
	require.Error(t, err,
		"params rejected by `Params.Validate()` were set anyway: the guard then only holds for the "+
			"paths that call `validate-genesis`")
}
