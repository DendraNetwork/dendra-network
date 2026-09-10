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
	// THE GENESIS FIXTURE MUST BE DERIVED FROM THE SIMULATION SEED, NOT FROM crypto/rand.
	//
	// `sample.AccAddress()` — what the scaffold offers for these eight `Creator` fields — calls
	// `ed25519.GenPrivKey()`, that is `crypto/rand`, NOT the simulation's SEEDED randomness. Filling the
	// fixture that way makes two runs of the SAME seed produce two different genesis states, hence two
	// app-hashes, and `TestAppStateDeterminism` goes red. The test is right to fail; what it accuses is
	// not the chain, it is its own fixture.
	//
	// AN ISOLATION IS WORTH ONLY WHAT IT COVERS. A module feeds the simulation through TWO doors:
	// `WeightedOperations` and this genesis generator. Neutralising the operations alone leaves the
	// divergence in place and invites the conclusion "it is not x/jobs" — a correct measurement leading
	// to a false conclusion, because only one HALF of the module is out of the way.
	//
	// The addresses therefore come from the simulation accounts, which are derived from the seed: same
	// seed, same genesis, same app-hash. `accs` is built just above for that purpose.
	// ADR-039 — THE SIMULATION MUST GENERATE A GENESIS THE CHAIN WOULD ACCEPT. Miner identifiers such as
	// "0" and "1" are refused by the genesis door, and `TestAppImportExport` re-imports what it exported,
	// so a generator producing unreachable state turns a real guard into a red simulation and invites the
	// wrong conclusion — that the guard is broken rather than the fixture. The identifier is derived from
	// the SAME simulation account that creates the miner, and those accounts come from the seed, so the
	// genesis stays deterministic: same seed, same identifiers, same app-hash.
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

// RegisterStoreDecoder makes the diverging pairs of `TestAppImportExport` READABLE.
//
// An empty body here is paid for at the worst possible moment: without a decoder, `GetSimulationLog`
// prints `store A %X => %X`. The keys are there, but in hexadecimal, so identifying which collection
// diverged (for instance `miner_registered_height` versus `job_pool_freeze_height`) requires
// instrumenting the test by hand. A check that cannot name its failure is half mute: it says there is
// a problem, not which one, so it does not save the time a check exists to save.
//
// The decoder is derived from the keeper's SCHEMA (see `x/simutil`), so by construction it covers
// every present and future collection, with no table to keep up to date.
func (am AppModule) RegisterStoreDecoder(registry simtypes.StoreDecoderRegistry) {
	registry[types.StoreKey] = simutil.NewDecodeStore(am.keeper.Schema)
}

// WeightedOperations — the operations the simulation may draw for this module.
//
// WHAT IS REGISTERED HERE MUST BE ABLE TO SUCCEED, AND MUST MEASURE SOMETHING.
//
// Two families are deliberately absent from the registry, for two distinct reasons:
//
//	(1) THE SCAFFOLD CRUD (`CreateJob`, `CreateCommit`, `CreatePools`, `CreateBeacon` and their
//	    Update/Delete). Invariant #7 states that a job is only born through `OpenJob`, so these
//	    handlers reject unconditionally (see `msg_server_job.go`). Registering them guarantees that
//	    the simulation aborts on "unable to deliver tx" at the first draw. A fixture that breaks
//	    after a hardening is not a licence to soften the code; the FIXTURE is what gets fixed.
//
//	(2) THE BUSINESS-LOGIC STUBS the scaffold offers — functions that build an empty message and
//	    return `NoOpMsg(..., "X simulation not implemented")`. Such a stub exercises NO chain path:
//	    it consumes an account draw and returns. Its net effect is to dilute the real operations —
//	    the more of them are registered, the less code the simulation touches — while giving the
//	    appearance of covering fifteen messages. A no-op registered as an operation is a coverage
//	    line that covers nothing.
//
// WHY REMOVAL RATHER THAN IMPLEMENTATION. Making these operations produce REACHABLE states (real
// escrow, anchored committee, verdicts) is a design task in its own right: each depends on a state
// that only a SEQUENCE of other messages builds, and an operation that targets the wrong state aborts
// the whole simulation. Removal is the only one of the two options that risks nothing for determinism,
// and it makes the absence of coverage VISIBLE rather than disguised as a dozen green entries.
//
// What remains registered are the MINER registry operations, which really deliver transactions and
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
