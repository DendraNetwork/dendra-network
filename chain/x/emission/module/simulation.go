package emission

import (
	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// GenerateGenesisState creates a randomized GenState of the module.
func (AppModule) GenerateGenesisState(simState *module.SimulationState) {
	// No `accs` here: this genesis is made of `DefaultParams()` only. The scaffold left an unused
	// address list dangling — that same orphan `accs` is what, in `x/jobs`, revealed that the generator
	// was building its addresses ELSEWHERE (`sample.AccAddress()`, outside the seed). A dead variable
	// that prepares accounts inside a genesis generator is an invitation; it is removed rather than
	// left waiting for a use.
	// AND AN EXPLICIT ZERO STATE -- without it the simulation no longer STARTS.
	// `InitGenesis` refuses a `Reserve` counter the module account does not cover. A nil `State` takes
	// the BOOTSTRAP branch and declares `GenesisReserveU` (3.3e12); the simulated bank never credits the
	// module, so the guard refused and the module PANICS (module.go:117): `TestAppImportExport` and the
	// three other simulations all died at `initChain`.
	// The answer is not to exempt the guard -- it would then go untested exactly where the export/import
	// round trip is replayed -- but to make this genesis SELF-CONSISTENT: a chain whose Reserve is zero
	// promises nothing, so it has nothing to back, and that is precisely the state of a simulated chain
	// nobody funded. The case is already held by the "exhausted reserve" test: a zero counter loads
	// without backing.
	emissionGenesis := types.GenesisState{
		Params: types.DefaultParams(),
		State:  &types.EmissionState{},
	}
	simState.GenState[types.ModuleName] = simState.Cdc.MustMarshalJSON(&emissionGenesis)
}

// RegisterStoreDecoder makes the diverging pairs of `TestAppImportExport` READABLE.
// The decoder is derived from the keeper's SCHEMA (cf. `x/simutil`): no table to keep up to date, hence
// nothing that can drift when a collection is added. An empty body (the scaffold default) prints the
// divergences as raw hex.
func (am AppModule) RegisterStoreDecoder(registry simtypes.StoreDecoderRegistry) {
	registry[types.StoreKey] = simutil.NewDecodeStore(am.keeper.Schema)
}

// WeightedOperations returns the all the gov module operations with their respective weights.
func (am AppModule) WeightedOperations(simState module.SimulationState) []simtypes.WeightedOperation {
	operations := make([]simtypes.WeightedOperation, 0)
	return operations
}

// ProposalMsgs returns msgs used for governance proposals for simulations.
func (am AppModule) ProposalMsgs(simState module.SimulationState) []simtypes.WeightedProposalMsg {
	return []simtypes.WeightedProposalMsg{}
}
