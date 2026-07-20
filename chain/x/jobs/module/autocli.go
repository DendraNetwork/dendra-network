package jobs

import (
	autocliv1 "cosmossdk.io/api/cosmos/autocli/v1"

	"dendra/x/jobs/types"
)

// AutoCLIOptions implements the autocli.HasAutoCLIConfig interface.
func (am AppModule) AutoCLIOptions() *autocliv1.ModuleOptions {
	return &autocliv1.ModuleOptions{
		Query: &autocliv1.ServiceCommandDescriptor{
			Service: types.Query_serviceDesc.ServiceName,
			RpcCommandOptions: []*autocliv1.RpcCommandOptions{
				{
					RpcMethod: "Params",
					Use:       "params",
					Short:     "Shows the parameters of the module",
				},
				{
					RpcMethod: "GetAvailChallenge",
					Use:       "get-avail-challenge",
					Short:     "Defi de disponibilite courant + epoque (Phase 1b)",
				},
				{
					RpcMethod: "CommitteeSeedHealth",
					Use:       "committee-seed-health",
					Short:     "Sante de l'alea de comite VRF decentralise (source/plancher/derniere graine/contributeurs) -> metrique grinding",
				},
				{
					RpcMethod: "ListMiner",
					Use:       "list-miner",
					Short:     "List all miner",
				},
				{
					RpcMethod:      "GetMiner",
					Use:            "get-miner [id]",
					Short:          "Gets a miner",
					Alias:          []string{"show-miner"},
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}},
				},
				{
					RpcMethod: "ListJob",
					Use:       "list-job",
					Short:     "List all job",
				},
				{
					RpcMethod:      "GetJob",
					Use:            "get-job [id]",
					Short:          "Gets a job",
					Alias:          []string{"show-job"},
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod: "GetPools",
					Use:       "get-pools",
					Short:     "Gets a pools",
					Alias:     []string{"show-pools"},
				},
				{
					RpcMethod: "ListCommit",
					Use:       "list-commit",
					Short:     "List all commit",
				},
				{
					RpcMethod:      "GetCommit",
					Use:            "get-commit [id]",
					Short:          "Gets a commit",
					Alias:          []string{"show-commit"},
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod: "ListBeacon",
					Use:       "list-beacon",
					Short:     "List all beacon",
				},
				{
					RpcMethod:      "GetBeacon",
					Use:            "get-beacon [id]",
					Short:          "Gets a beacon",
					Alias:          []string{"show-beacon"},
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
			},
		},
		Tx: &autocliv1.ServiceCommandDescriptor{
			Service:              types.Msg_serviceDesc.ServiceName,
			EnhanceCustomCommand: true, // only required if you want to use the custom command
			RpcCommandOptions: []*autocliv1.RpcCommandOptions{
				{
					RpcMethod: "UpdateParams",
					Skip:      true, // skipped because authority gated
				},
				{
					RpcMethod:      "ProveAvailability",
					Use:            "prove-availability [miner-id] [challenge] [vrf_proof]",
					Short:          "Prouver sa disponibilite en repondant au defi de l'epoque (Phase 1b)",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}, {ProtoField: "challenge"}, {ProtoField: "vrf_proof", Optional: true}},
				},
				{
					RpcMethod:      "SubmitVrfBeacon",
					Use:            "submit-vrf-beacon [job_id] [vrf_proof]",
					Short:          "Poser la graine de comite d'un job via une preuve VRF (CR-10)",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "vrf_proof"}},
				},
				{
					RpcMethod:      "RotateMinerKeys",
					Use:            "rotate-miner-keys [miner_id]",
					Short:          "Tourner les cles enc/vrf d'un mineur (--new-enc-pubkey / --new-vrf-pubkey ; V6-03)",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}},
				},
				{
					RpcMethod:      "RegisterValidatorVrfKey",
					Use:            "register-validator-vrf-key [vrf_pubkey] [vrf_pop]",
					Short:          "Ancrer la cle pub VRF (hex 32o) + proof-of-possession (preuve VRF sur dendra/vrf-pop/<creator>) du validateur (E4/VE-02)",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "vrf_pubkey"}, {ProtoField: "vrf_pop"}},
				},
				{
					RpcMethod:      "CreateMiner",
					Use:            "create-miner [miner_id] [operator] [region] [stake] [enc_pubkey]",
					Short:          "Create a new miner",
					// vrf_pubkey est un FLAG (--vrf-pubkey) : AutoCLI n'autorise QU'UN positionnel optionnel, en dernier.
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}, {ProtoField: "operator"}, {ProtoField: "region"}, {ProtoField: "stake"}, {ProtoField: "enc_pubkey", Optional: true}},
				},
				{
					RpcMethod:      "UpdateMiner",
					Use:            "update-miner [miner_id] [operator] [region] [stake]",
					Short:          "Update miner",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}, {ProtoField: "operator"}, {ProtoField: "region"}, {ProtoField: "stake"}},
				},
				{
					RpcMethod:      "DeleteMiner",
					Use:            "delete-miner [miner_id]",
					Short:          "Delete miner",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}},
				},
				{
					RpcMethod:      "CreateJob",
					Use:            "create-job [job_id] [client] [miner-id] [fee] [state]",
					Short:          "Create a new job",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "client"}, {ProtoField: "miner_id"}, {ProtoField: "fee"}, {ProtoField: "state"}},
				},
				{
					RpcMethod:      "UpdateJob",
					Use:            "update-job [job_id] [client] [miner-id] [fee] [state]",
					Short:          "Update job",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "client"}, {ProtoField: "miner_id"}, {ProtoField: "fee"}, {ProtoField: "state"}},
				},
				{
					RpcMethod:      "DeleteJob",
					Use:            "delete-job [job_id]",
					Short:          "Delete job",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod:      "CreatePools",
					Use:            "create-pools [miner-paid] [validators] [team] [treasury]",
					Short:          "Create pools",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_paid"}, {ProtoField: "validators"}, {ProtoField: "team"}, {ProtoField: "treasury"}},
				},
				{
					RpcMethod:      "UpdatePools",
					Use:            "update-pools [miner-paid] [validators] [team] [treasury]",
					Short:          "Update pools",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_paid"}, {ProtoField: "validators"}, {ProtoField: "team"}, {ProtoField: "treasury"}},
				},
				{
					RpcMethod: "DeletePools",
					Use:       "delete-pools",
					Short:     "Delete pools",
				},
				{
					RpcMethod:      "SettleJob",
					Use:            "settle-job [job-id]",
					Short:          "Send a settle-job tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod:      "SlashMiner",
					Use:            "slash-miner [miner-id]",
					Short:          "Send a slash-miner tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}},
				},
				{
					RpcMethod:      "ClaimSubsidy",
					Use:            "claim-subsidy [miner-id]",
					Short:          "Send a claim-subsidy tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}},
				},
				{
					RpcMethod:      "ReportDivergence",
					Use:            "report-divergence [job-id] [miner-commit] [correct-commit]",
					Short:          "Send a report-divergence tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "miner_commit"}, {ProtoField: "correct_commit"}},
				},
				{
					RpcMethod:      "RewardTraining",
					Use:            "reward-training [miner-id] [units]",
					Short:          "Send a reward-training tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "miner_id"}, {ProtoField: "units"}},
				},
				{
					RpcMethod:      "PayReal",
					Use:            "pay-real [recipient] [amount]",
					Short:          "Send a pay-real tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "recipient"}, {ProtoField: "amount"}},
				},
				{
					RpcMethod:      "CreateCommit",
					Use:            "create-commit [job_id] [prompt-commit] [result-commit] [kind]",
					Short:          "Create a new commit",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "prompt_commit"}, {ProtoField: "result_commit"}, {ProtoField: "kind"}},
				},
				{
					RpcMethod:      "UpdateCommit",
					Use:            "update-commit [job_id] [prompt-commit] [result-commit] [kind]",
					Short:          "Update commit",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "prompt_commit"}, {ProtoField: "result_commit"}, {ProtoField: "kind"}},
				},
				{
					RpcMethod:      "DeleteCommit",
					Use:            "delete-commit [job_id]",
					Short:          "Delete commit",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod:      "FinalizeJob",
					Use:            "finalize-job [job-id]",
					Short:          "Send a finalize-job tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod:      "SettlePay",
					Use:            "settle-pay [job-id] [amount]",
					Short:          "Send a settle-pay tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "amount"}},
				},
				{
					RpcMethod:      "OpenJob",
					Use:            "open-job [job-id] [fee]",
					Short:          "Send a open-job tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "fee"}},
				},
				{
					RpcMethod:      "Payout",
					Use:            "payout [job-id] [amount]",
					Short:          "Send a payout tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "amount"}},
				},
				{
					RpcMethod:      "VerifySemantic",
					Use:            "verify-semantic [job-id] [threshold-bps]",
					Short:          "Send a verify-semantic tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "threshold_bps"}},
				},
				{
					RpcMethod:      "CreateBeacon",
					Use:            "create-beacon [job_id] [seed]",
					Short:          "Create a new beacon",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "seed"}},
				},
				{
					RpcMethod:      "UpdateBeacon",
					Use:            "update-beacon [job_id] [seed]",
					Short:          "Update beacon",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "seed"}},
				},
				{
					RpcMethod:      "DeleteBeacon",
					Use:            "delete-beacon [job_id]",
					Short:          "Delete beacon",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}},
				},
				{
					RpcMethod:      "SettleSemantic",
					Use:            "settle-semantic [job-id] [reward] [threshold-bps]",
					Short:          "Send a settle-semantic tx",
					PositionalArgs: []*autocliv1.PositionalArgDescriptor{{ProtoField: "job_id"}, {ProtoField: "reward"}, {ProtoField: "threshold_bps"}},
				},
			},
		},
	}
}
