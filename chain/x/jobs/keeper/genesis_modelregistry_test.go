package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ENFORCING THE REGISTRY PRESUPPOSES THAT THE REGISTRY ANCHORS SOMETHING.
//
// THE COMBINATION THESE CASES MAKE IMPOSSIBLE. `enforce_model_registry` requires a commit to cite a
// registered, active model. But the only thing binding that `model_id` to an ARTEFACT is the
// `weights_sha256` anchored in the registry: without an anchor, `ExpectedWeights` answers "nothing to
// verify" and the miner declares one model while serving whatever it likes. A genesis that arms
// enforcement while leaving ACTIVE models unanchored therefore announces a constraint that does not
// exist — and nothing signals it, since the commits are accepted.
//
// The guard is a CROSS-CHECK, not a hardening of the registry: an active model without an anchor stays
// valid as long as enforcement is not armed (the case of the development genesis). Refusing outright
// would have replaced a missing guard with a chain that does not start.

func genesisWithParams(p types.Params) types.GenesisState {
	gs := types.DefaultGenesis()
	gs.Params = p
	return *gs
}

// (1) REGISTRY ENFORCED + ACTIVE MODEL WITHOUT AN ANCHOR -> refused at load, NAMING the model.
func TestInitGenesisRefusesEnforcedRegistryWithoutAnchor(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-no-anchor"] = true // active, no weights_sha256

	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	err := f.keeper.InitGenesis(f.ctx, genesisWithParams(p))
	require.Error(t, err,
		"genesis loaded while announcing that it ENFORCES the registry with an unanchored active model: "+
			"for that model the model_id <-> artefact binding does not exist and the commit passes anyway")
	require.Contains(t, err.Error(), "llama-no-anchor",
		"the refusal must name the offending model, otherwise it has to be hunted down by hand")
}

// (2) REGISTRY ENFORCED + EVERY ACTIVE MODEL ANCHORED -> accepted.
func TestInitGenesisAcceptsEnforcedRegistryWithAnchors(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-ancre"] = true
	f.modelReg.weights["llama-ancre"] = "deadbeef"
	f.modelReg.active["modele-retire"] = false // inactive: no anchor required

	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.InitGenesis(f.ctx, genesisWithParams(p)))
}

// (3) REGISTRY NOT ENFORCED -> unanchored models stay accepted (development genesis).
// Without this case, the guard could become an unconditional refusal with nothing turning red.
func TestInitGenesisAcceptsUnanchoredModelsWhenTheRegistryIsNotEnforced(t *testing.T) {
	f := initFixture(t)
	f.modelReg.active["llama-no-anchor"] = true

	p := types.DefaultParams()
	require.False(t, p.EnforceModelRegistry, "the default must not enforce the registry")
	require.NoError(t, f.keeper.InitGenesis(f.ctx, genesisWithParams(p)))
}

// (4) EVERY INIT PATH VALIDATES ITS PARAMS — `validate-genesis` is not the only safeguard.
// A hand-built genesis reaches `InitGenesis` directly; the cross bounds of optimistic mode must bite
// there too, otherwise they only hold for those who invoke the tool.
func TestInitGenesisRefusesInvalidParams(t *testing.T) {
	f := initFixture(t)

	p := types.DefaultParams()
	p.VerificationMode = 1 // armed without any of the bounds the mode requires
	err := f.keeper.InitGenesis(f.ctx, genesisWithParams(p))
	require.Error(t, err,
		"params rejected by `Params.Validate()` were set anyway: the guard then only holds for the "+
			"paths that call `validate-genesis`")
}
