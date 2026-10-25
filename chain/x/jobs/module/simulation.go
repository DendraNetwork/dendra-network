package jobs

import (
	"math/rand"

	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	"github.com/cosmos/cosmos-sdk/x/simulation"

	jobssimulation "github.com/DendraNetwork/dendra-network/chain/x/jobs/simulation"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// GenerateGenesisState creates a randomized GenState of the module.
func (AppModule) GenerateGenesisState(simState *module.SimulationState) {
	accs := make([]string, len(simState.Accounts))
	for i, acc := range simState.Accounts {
		accs[i] = acc.Address.String()
	}
	// The genesis fixture must be derived from the simulation seed, never from crypto/rand.
	//
	// `sample.AccAddress()`, which the scaffold offers for these eight `Creator` fields, calls
	// `ed25519.GenPrivKey()`, that is `crypto/rand`, outside the simulation's seeded randomness. Filling the
	// fixture that way makes two runs of the same seed produce two different genesis states, hence two app
	// hashes, and `TestAppStateDeterminism` goes red, blaming the chain for a defect in its own fixture.
	//
	// A module feeds the simulation through two paths: `WeightedOperations` and this genesis generator.
	// Neutralising the operations alone leaves the divergence in place and suggests "it is not x/jobs", a
	// correct measurement leading to a false conclusion because only half of the module was isolated.
	//
	// The addresses therefore come from the simulation accounts, which are derived from the seed: same
	// seed, same genesis, same app hash. `accs` is built just above for that purpose.
	// The simulation must generate a genesis the chain would accept. Miner identifiers such as
	// "0" and "1" are refused at genesis, and `TestAppImportExport` re-imports what it exported, so a
	// generator producing unreachable state turns a real guard into a red simulation and suggests that
	// the guard is broken when the fixture is. The identifier is derived from the same simulation account
	// that creates the miner, and those accounts come from the seed, so the genesis stays deterministic:
	// same seed, same identifiers, same app hash.
	minerID := func(addr string) string {
		id, err := types.MinerIDForAccount(addr)
		if err != nil {
			panic("simulation genesis: " + err.Error())
		}
		return id
	}
	jobsGenesis := types.GenesisState{
		Params: types.DefaultParams(),
		MinerMap: []types.Miner{{Creator: accs[0%len(accs)],
			MinerId: minerID(accs[0%len(accs)]),
		}, {Creator: accs[1%len(accs)],
			MinerId: minerID(accs[1%len(accs)]),
		}}, JobMap: []types.Job{{Creator: accs[2%len(accs)],
			JobId: "0",
		}, {Creator: accs[3%len(accs)],
			JobId: "1",
		}}, CommitMap: []types.Commit{{Creator: accs[4%len(accs)],
			JobId: "0",
		}, {Creator: accs[5%len(accs)],
			JobId: "1",
		}}, BeaconMap: []types.Beacon{{Creator: accs[6%len(accs)],
			JobId: "0",
		}, {Creator: accs[7%len(accs)],
			JobId: "1",
		}}}
	simState.GenState[types.ModuleName] = simState.Cdc.MustMarshalJSON(&jobsGenesis)
}

// RegisterStoreDecoder makes the diverging pairs of `TestAppImportExport` readable.
//
// Without a decoder, `GetSimulationLog` prints `store A %X => %X`: the keys are there, but in hex, so
// identifying which collection diverged (for instance `miner_registered_height` versus
// `job_pool_freeze_height`) requires instrumenting the test by hand. A check that cannot name its
// failure says there is a problem but not which one.
//
// The decoder is derived from the keeper's schema (see `x/simutil`), so it covers every present and
// future collection with no table to keep up to date.
func (am AppModule) RegisterStoreDecoder(registry simtypes.StoreDecoderRegistry) {
	registry[types.StoreKey] = simutil.NewDecodeStore(am.keeper.Schema)
}

// WeightedOperations returns the operations the simulation may draw for this module.
//
// What is registered here must be able to succeed, and must measure something.
//
// Two families are left out of the registry on purpose, for two distinct reasons:
//
//	(1) The scaffold CRUD (`CreateJob`, `CreateCommit`, `CreatePools`, `CreateBeacon` and their
//	    Update/Delete). Invariant #7 states that a job is only born through `OpenJob`, so these
//	    handlers reject unconditionally (see `msg_server_job.go`). Registering them guarantees that
//	    the simulation aborts on "unable to deliver tx" at the first draw. A fixture that breaks
//	    after a hardening is no reason to soften the code; the fixture is what gets fixed.
//
//	(2) The business-logic stubs the scaffold offers: functions that build an empty message and
//	    return `NoOpMsg(..., "X simulation not implemented")`. Such a stub exercises no chain path;
//	    it consumes an account draw and returns. Its net effect is to dilute the real operations
//	    (the more are registered, the less code the simulation touches) while appearing to cover
//	    fifteen messages. A no-op registered as an operation covers nothing.
//
// Why removal rather than implementation: making these operations produce reachable states (real
// escrow, anchored committee, verdicts) is a design task of its own. Each depends on a state that only
// a sequence of other messages builds, and an operation that targets the wrong state aborts the whole
// simulation. Removal is the only one of the two options that risks nothing for determinism, and it
// makes the missing coverage visible instead of disguising it as a dozen green entries.
//
// What remains registered are the miner registry operations, which really deliver transactions and
// exercise the bond escrow.
func (am AppModule) WeightedOperations(simState module.SimulationState) []simtypes.WeightedOperation {
	operations := make([]simtypes.WeightedOperation, 0)

	const (
		opWeightMsgCreateMiner          = "op_weight_msg_jobs"
		defaultWeightMsgCreateMiner int = 100
	)

	var weightMsgCreateMiner int
	simState.AppParams.GetOrGenerate(opWeightMsgCreateMiner, &weightMsgCreateMiner, nil,
		func(_ *rand.Rand) {
			weightMsgCreateMiner = defaultWeightMsgCreateMiner
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgCreateMiner,
		jobssimulation.SimulateMsgCreateMiner(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgUpdateMiner          = "op_weight_msg_jobs"
		defaultWeightMsgUpdateMiner int = 100
	)

	var weightMsgUpdateMiner int
	simState.AppParams.GetOrGenerate(opWeightMsgUpdateMiner, &weightMsgUpdateMiner, nil,
		func(_ *rand.Rand) {
			weightMsgUpdateMiner = defaultWeightMsgUpdateMiner
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgUpdateMiner,
		jobssimulation.SimulateMsgUpdateMiner(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgDeleteMiner          = "op_weight_msg_jobs"
		defaultWeightMsgDeleteMiner int = 100
	)

	var weightMsgDeleteMiner int
	simState.AppParams.GetOrGenerate(opWeightMsgDeleteMiner, &weightMsgDeleteMiner, nil,
		func(_ *rand.Rand) {
			weightMsgDeleteMiner = defaultWeightMsgDeleteMiner
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgDeleteMiner,
		jobssimulation.SimulateMsgDeleteMiner(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))

	return operations
}

// ProposalMsgs returns msgs used for governance proposals for simulations.
func (am AppModule) ProposalMsgs(simState module.SimulationState) []simtypes.WeightedProposalMsg {
	return []simtypes.WeightedProposalMsg{}
}
