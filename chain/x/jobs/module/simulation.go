package jobs

import (
	"math/rand"

	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	"github.com/cosmos/cosmos-sdk/x/simulation"

	"dendra/testutil/sample"
	jobssimulation "dendra/x/jobs/simulation"
	"dendra/x/jobs/types"
)

// GenerateGenesisState creates a randomized GenState of the module.
func (AppModule) GenerateGenesisState(simState *module.SimulationState) {
	accs := make([]string, len(simState.Accounts))
	for i, acc := range simState.Accounts {
		accs[i] = acc.Address.String()
	}
	jobsGenesis := types.GenesisState{
		Params: types.DefaultParams(),
		MinerMap: []types.Miner{{Creator: sample.AccAddress(),
			MinerId: "0",
		}, {Creator: sample.AccAddress(),
			MinerId: "1",
		}}, JobMap: []types.Job{{Creator: sample.AccAddress(),
			JobId: "0",
		}, {Creator: sample.AccAddress(),
			JobId: "1",
		}}, CommitMap: []types.Commit{{Creator: sample.AccAddress(),
			JobId: "0",
		}, {Creator: sample.AccAddress(),
			JobId: "1",
		}}, BeaconMap: []types.Beacon{{Creator: sample.AccAddress(),
			JobId: "0",
		}, {Creator: sample.AccAddress(),
			JobId: "1",
		}}}
	simState.GenState[types.ModuleName] = simState.Cdc.MustMarshalJSON(&jobsGenesis)
}

// RegisterStoreDecoder registers a decoder.
func (am AppModule) RegisterStoreDecoder(_ simtypes.StoreDecoderRegistry) {}

// WeightedOperations returns the all the gov module operations with their respective weights.
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
	const (
		opWeightMsgCreateJob          = "op_weight_msg_jobs"
		defaultWeightMsgCreateJob int = 100
	)

	var weightMsgCreateJob int
	simState.AppParams.GetOrGenerate(opWeightMsgCreateJob, &weightMsgCreateJob, nil,
		func(_ *rand.Rand) {
			weightMsgCreateJob = defaultWeightMsgCreateJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgCreateJob,
		jobssimulation.SimulateMsgCreateJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgUpdateJob          = "op_weight_msg_jobs"
		defaultWeightMsgUpdateJob int = 100
	)

	var weightMsgUpdateJob int
	simState.AppParams.GetOrGenerate(opWeightMsgUpdateJob, &weightMsgUpdateJob, nil,
		func(_ *rand.Rand) {
			weightMsgUpdateJob = defaultWeightMsgUpdateJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgUpdateJob,
		jobssimulation.SimulateMsgUpdateJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgDeleteJob          = "op_weight_msg_jobs"
		defaultWeightMsgDeleteJob int = 100
	)

	var weightMsgDeleteJob int
	simState.AppParams.GetOrGenerate(opWeightMsgDeleteJob, &weightMsgDeleteJob, nil,
		func(_ *rand.Rand) {
			weightMsgDeleteJob = defaultWeightMsgDeleteJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgDeleteJob,
		jobssimulation.SimulateMsgDeleteJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgCreatePools          = "op_weight_msg_jobs"
		defaultWeightMsgCreatePools int = 100
	)

	var weightMsgCreatePools int
	simState.AppParams.GetOrGenerate(opWeightMsgCreatePools, &weightMsgCreatePools, nil,
		func(_ *rand.Rand) {
			weightMsgCreatePools = defaultWeightMsgCreatePools
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgCreatePools,
		jobssimulation.SimulateMsgCreatePools(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgUpdatePools          = "op_weight_msg_jobs"
		defaultWeightMsgUpdatePools int = 100
	)

	var weightMsgUpdatePools int
	simState.AppParams.GetOrGenerate(opWeightMsgUpdatePools, &weightMsgUpdatePools, nil,
		func(_ *rand.Rand) {
			weightMsgUpdatePools = defaultWeightMsgUpdatePools
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgUpdatePools,
		jobssimulation.SimulateMsgUpdatePools(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgDeletePools          = "op_weight_msg_jobs"
		defaultWeightMsgDeletePools int = 100
	)

	var weightMsgDeletePools int
	simState.AppParams.GetOrGenerate(opWeightMsgDeletePools, &weightMsgDeletePools, nil,
		func(_ *rand.Rand) {
			weightMsgDeletePools = defaultWeightMsgDeletePools
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgDeletePools,
		jobssimulation.SimulateMsgDeletePools(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgSettleJob          = "op_weight_msg_jobs"
		defaultWeightMsgSettleJob int = 100
	)

	var weightMsgSettleJob int
	simState.AppParams.GetOrGenerate(opWeightMsgSettleJob, &weightMsgSettleJob, nil,
		func(_ *rand.Rand) {
			weightMsgSettleJob = defaultWeightMsgSettleJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgSettleJob,
		jobssimulation.SimulateMsgSettleJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgSlashMiner          = "op_weight_msg_jobs"
		defaultWeightMsgSlashMiner int = 100
	)

	var weightMsgSlashMiner int
	simState.AppParams.GetOrGenerate(opWeightMsgSlashMiner, &weightMsgSlashMiner, nil,
		func(_ *rand.Rand) {
			weightMsgSlashMiner = defaultWeightMsgSlashMiner
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgSlashMiner,
		jobssimulation.SimulateMsgSlashMiner(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgClaimSubsidy          = "op_weight_msg_jobs"
		defaultWeightMsgClaimSubsidy int = 100
	)

	var weightMsgClaimSubsidy int
	simState.AppParams.GetOrGenerate(opWeightMsgClaimSubsidy, &weightMsgClaimSubsidy, nil,
		func(_ *rand.Rand) {
			weightMsgClaimSubsidy = defaultWeightMsgClaimSubsidy
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgClaimSubsidy,
		jobssimulation.SimulateMsgClaimSubsidy(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgReportDivergence          = "op_weight_msg_jobs"
		defaultWeightMsgReportDivergence int = 100
	)

	var weightMsgReportDivergence int
	simState.AppParams.GetOrGenerate(opWeightMsgReportDivergence, &weightMsgReportDivergence, nil,
		func(_ *rand.Rand) {
			weightMsgReportDivergence = defaultWeightMsgReportDivergence
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgReportDivergence,
		jobssimulation.SimulateMsgReportDivergence(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgRewardTraining          = "op_weight_msg_jobs"
		defaultWeightMsgRewardTraining int = 100
	)

	var weightMsgRewardTraining int
	simState.AppParams.GetOrGenerate(opWeightMsgRewardTraining, &weightMsgRewardTraining, nil,
		func(_ *rand.Rand) {
			weightMsgRewardTraining = defaultWeightMsgRewardTraining
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgRewardTraining,
		jobssimulation.SimulateMsgRewardTraining(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgPayReal          = "op_weight_msg_jobs"
		defaultWeightMsgPayReal int = 100
	)

	var weightMsgPayReal int
	simState.AppParams.GetOrGenerate(opWeightMsgPayReal, &weightMsgPayReal, nil,
		func(_ *rand.Rand) {
			weightMsgPayReal = defaultWeightMsgPayReal
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgPayReal,
		jobssimulation.SimulateMsgPayReal(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgCreateCommit          = "op_weight_msg_jobs"
		defaultWeightMsgCreateCommit int = 100
	)

	var weightMsgCreateCommit int
	simState.AppParams.GetOrGenerate(opWeightMsgCreateCommit, &weightMsgCreateCommit, nil,
		func(_ *rand.Rand) {
			weightMsgCreateCommit = defaultWeightMsgCreateCommit
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgCreateCommit,
		jobssimulation.SimulateMsgCreateCommit(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgUpdateCommit          = "op_weight_msg_jobs"
		defaultWeightMsgUpdateCommit int = 100
	)

	var weightMsgUpdateCommit int
	simState.AppParams.GetOrGenerate(opWeightMsgUpdateCommit, &weightMsgUpdateCommit, nil,
		func(_ *rand.Rand) {
			weightMsgUpdateCommit = defaultWeightMsgUpdateCommit
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgUpdateCommit,
		jobssimulation.SimulateMsgUpdateCommit(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgDeleteCommit          = "op_weight_msg_jobs"
		defaultWeightMsgDeleteCommit int = 100
	)

	var weightMsgDeleteCommit int
	simState.AppParams.GetOrGenerate(opWeightMsgDeleteCommit, &weightMsgDeleteCommit, nil,
		func(_ *rand.Rand) {
			weightMsgDeleteCommit = defaultWeightMsgDeleteCommit
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgDeleteCommit,
		jobssimulation.SimulateMsgDeleteCommit(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgFinalizeJob          = "op_weight_msg_jobs"
		defaultWeightMsgFinalizeJob int = 100
	)

	var weightMsgFinalizeJob int
	simState.AppParams.GetOrGenerate(opWeightMsgFinalizeJob, &weightMsgFinalizeJob, nil,
		func(_ *rand.Rand) {
			weightMsgFinalizeJob = defaultWeightMsgFinalizeJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgFinalizeJob,
		jobssimulation.SimulateMsgFinalizeJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgSettlePay          = "op_weight_msg_jobs"
		defaultWeightMsgSettlePay int = 100
	)

	var weightMsgSettlePay int
	simState.AppParams.GetOrGenerate(opWeightMsgSettlePay, &weightMsgSettlePay, nil,
		func(_ *rand.Rand) {
			weightMsgSettlePay = defaultWeightMsgSettlePay
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgSettlePay,
		jobssimulation.SimulateMsgSettlePay(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgOpenJob          = "op_weight_msg_jobs"
		defaultWeightMsgOpenJob int = 100
	)

	var weightMsgOpenJob int
	simState.AppParams.GetOrGenerate(opWeightMsgOpenJob, &weightMsgOpenJob, nil,
		func(_ *rand.Rand) {
			weightMsgOpenJob = defaultWeightMsgOpenJob
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgOpenJob,
		jobssimulation.SimulateMsgOpenJob(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgPayout          = "op_weight_msg_jobs"
		defaultWeightMsgPayout int = 100
	)

	var weightMsgPayout int
	simState.AppParams.GetOrGenerate(opWeightMsgPayout, &weightMsgPayout, nil,
		func(_ *rand.Rand) {
			weightMsgPayout = defaultWeightMsgPayout
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgPayout,
		jobssimulation.SimulateMsgPayout(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgVerifySemantic          = "op_weight_msg_jobs"
		defaultWeightMsgVerifySemantic int = 100
	)

	var weightMsgVerifySemantic int
	simState.AppParams.GetOrGenerate(opWeightMsgVerifySemantic, &weightMsgVerifySemantic, nil,
		func(_ *rand.Rand) {
			weightMsgVerifySemantic = defaultWeightMsgVerifySemantic
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgVerifySemantic,
		jobssimulation.SimulateMsgVerifySemantic(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgCreateBeacon          = "op_weight_msg_jobs"
		defaultWeightMsgCreateBeacon int = 100
	)

	var weightMsgCreateBeacon int
	simState.AppParams.GetOrGenerate(opWeightMsgCreateBeacon, &weightMsgCreateBeacon, nil,
		func(_ *rand.Rand) {
			weightMsgCreateBeacon = defaultWeightMsgCreateBeacon
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgCreateBeacon,
		jobssimulation.SimulateMsgCreateBeacon(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgUpdateBeacon          = "op_weight_msg_jobs"
		defaultWeightMsgUpdateBeacon int = 100
	)

	var weightMsgUpdateBeacon int
	simState.AppParams.GetOrGenerate(opWeightMsgUpdateBeacon, &weightMsgUpdateBeacon, nil,
		func(_ *rand.Rand) {
			weightMsgUpdateBeacon = defaultWeightMsgUpdateBeacon
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgUpdateBeacon,
		jobssimulation.SimulateMsgUpdateBeacon(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgDeleteBeacon          = "op_weight_msg_jobs"
		defaultWeightMsgDeleteBeacon int = 100
	)

	var weightMsgDeleteBeacon int
	simState.AppParams.GetOrGenerate(opWeightMsgDeleteBeacon, &weightMsgDeleteBeacon, nil,
		func(_ *rand.Rand) {
			weightMsgDeleteBeacon = defaultWeightMsgDeleteBeacon
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgDeleteBeacon,
		jobssimulation.SimulateMsgDeleteBeacon(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))
	const (
		opWeightMsgSettleSemantic          = "op_weight_msg_jobs"
		defaultWeightMsgSettleSemantic int = 100
	)

	var weightMsgSettleSemantic int
	simState.AppParams.GetOrGenerate(opWeightMsgSettleSemantic, &weightMsgSettleSemantic, nil,
		func(_ *rand.Rand) {
			weightMsgSettleSemantic = defaultWeightMsgSettleSemantic
		},
	)
	operations = append(operations, simulation.NewWeightedOperation(
		weightMsgSettleSemantic,
		jobssimulation.SimulateMsgSettleSemantic(am.authKeeper, am.bankKeeper, am.keeper, simState.TxConfig),
	))

	return operations
}

// ProposalMsgs returns msgs used for governance proposals for simulations.
func (am AppModule) ProposalMsgs(simState module.SimulationState) []simtypes.WeightedProposalMsg {
	return []simtypes.WeightedProposalMsg{}
}
