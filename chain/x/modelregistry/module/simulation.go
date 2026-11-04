package modelregistry

import (
	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// GenerateGenesisState creates a randomized GenState of the module.
func (AppModule) GenerateGenesisState(simState *module.SimulationState) {
	// No `accs` here: this genesis consists only of `DefaultParams()`. The scaffold left an unused address
	// list; the same orphan `accs` in `x/jobs` revealed that the generator was building its addresses
	// elsewhere (`sample.AccAddress()`, outside the seed). A dead variable that prepares accounts inside a
	// genesis generator invites misuse, so it is removed.
	modelregistryGenesis := types.GenesisState{
		Params: types.DefaultParams(),
	}
	simState.GenState[types.ModuleName] = simState.Cdc.MustMarshalJSON(&modelregistryGenesis)
}

// RegisterStoreDecoder makes the diverging pairs reported by `TestAppImportExport` readable.
// The decoder is derived from the keeper's schema (see `x/simutil`), so there is no table to keep up to
// date and nothing to drift when a collection is added. With the scaffold's empty body, the
// differences were printed as raw hex.
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
