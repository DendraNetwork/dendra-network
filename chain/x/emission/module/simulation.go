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
	// address list; the same orphan `accs` in `x/jobs` revealed that the generator was building its
	// addresses elsewhere (`sample.AccAddress()`, outside the seed). A dead variable that prepares
	// accounts inside a genesis generator invites misuse, so it is removed.
	// The state is an explicit zero, without which the simulation does not start.
	// `InitGenesis` refuses a `Reserve` counter the module account does not cover. A nil `State` takes
	// the bootstrap branch and declares `GenesisReserveU` (3.3e12); the simulated bank never credits
	// the module, so the guard refused and the module panicked, and `TestAppImportExport` and the
	// other three simulations all died at `initChain`.
	// Exempting the guard would leave it untested exactly where the export/import round trip is
	// replayed. This genesis is made self-consistent instead: a chain whose Reserve is zero promises
	// nothing, so it has nothing to back, which is the state of a simulated chain nobody funded. The
	// "exhausted reserve" test already covers that case: a zero counter loads without backing.
	emissionGenesis := types.GenesisState{
		Params: types.DefaultParams(),
		State:  &types.EmissionState{},
	}
	simState.GenState[types.ModuleName] = simState.Cdc.MustMarshalJSON(&emissionGenesis)
}

// RegisterStoreDecoder makes the diverging pairs of `TestAppImportExport` readable.
// The decoder is derived from the keeper's schema (see `x/simutil`), so there is no table to keep up
// to date and nothing to drift when a collection is added. An empty body (the scaffold default)
// prints the divergences as raw hex.
func (am AppModule) RegisterStoreDecoder(registry simtypes.StoreDecoderRegistry) {
	registry[types.StoreKey] = simutil.NewDecodeStore(am.keeper.Schema)
}

// WeightedOperations returns all the module's operations with their weights.
func (am AppModule) WeightedOperations(simState module.SimulationState) []simtypes.WeightedOperation {
	operations := make([]simtypes.WeightedOperation, 0)
	return operations
}

// ProposalMsgs returns msgs used for governance proposals for simulations.
func (am AppModule) ProposalMsgs(simState module.SimulationState) []simtypes.WeightedProposalMsg {
	return []simtypes.WeightedProposalMsg{}
}
