package keeper

import (
	"fmt"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/address"
	corestore "cosmossdk.io/core/store"
	"github.com/cosmos/cosmos-sdk/codec"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

type Keeper struct {
	storeService corestore.KVStoreService
	cdc          codec.Codec
	addressCodec address.Codec
	// Address capable of executing a MsgUpdateParams message.
	// Typically, this should be the x/gov module account.
	authority []byte

	Schema collections.Schema
	Params collections.Item[types.Params]

	bankKeeper          types.BankKeeper
	emissionKeeper      types.EmissionKeeper
	modelRegistryKeeper types.ModelRegistryKeeper
	Miner               collections.Map[string, types.Miner]
	Job                 collections.Map[string, types.Job]
	Pools               collections.Item[types.Pools]
	Commit              collections.Map[string, types.Commit]
	Beacon              collections.Map[string, types.Beacon]
	// PendingReveal indexes the jobs awaiting committee reveal (deferred reveal).
	// Key = (revealHeight, jobId); the EndBlocker freezes the beacon seed at revealHeight.
	PendingReveal collections.KeySet[collections.Pair[int64, string]]
	// Availability: current challenge (rolled every epoch) plus the presences proven per epoch.
	AvailChallenge collections.Item[string]
	Available      collections.KeySet[collections.Pair[int64, string]]
	// VRF public key per validator (bech32 operator account -> vrf_pubkey hex). Consulted by the
	// ABCI++ vote-extension handlers; dormant while vote extensions are OFF.
	ValidatorVrfPubkey collections.Map[string, string]
	// ADR-032: ANCHORED audit committee (jobId -> IDs joined by ','). Written at sampling time by
	// runOptimisticAudit; ONLY its members count in auditVerdictTally. Without this anchor the tally
	// authenticated voters merely as "registered miner", so 4 identities at min_stake could slash 80%
	// of an HONEST miner's stake.
	AuditCommittee collections.Map[string, string]
	// Decentralized seed aggregated from the VRF vote extensions, per block height.
	DecentralizedSeed collections.Map[int64, []byte]
	// Number of contributors to the decentralized seed per height, feeding the
	// committee_min_vrf_contributors floor: an under-decentralized seed raises an alert and falls back
	// instead of degrading silently.
	DecentralizedSeedContributors collections.Map[int64, uint64]
	// Block hash per height (written by the PreBlocker when vote extensions are active).
	BlockHash collections.Map[int64, []byte]
	// ADR-025: OPTIMISTICALLY settled jobs awaiting the audit draw. Key = (auditHeight, jobId).
	// The EndBlocker draws H(seed‖jobId) mod 10000 < audit_sample_bps -> +disputed if audited.
	// Dormant in verification mode 0.
	PendingAudit collections.KeySet[collections.Pair[int64, string]]
	// ADR-025: count of optimistic jobs served per miner (anti-Sybil probation; key = minerId).
	MinerOptimisticCount collections.Map[string, uint64]
	// ADR-034: jobs whose audit draw is ALREADY won but whose opening is deferred (jury too small).
	// Prevents a retry from replaying the auditDraw lottery and RELEASING the payment unverified.
	AuditSelected collections.KeySet[string]
	// Jobs whose held-fee release failed on send and remains DUE (replayed by the EndBlocker).
	PendingHeldRelease collections.KeySet[string]
	// MONOTONIC count of opened audits, an INDEPENDENT source for the partition test — without it,
	// "sum of the classes == opened" would be a tautology. One increment per job.
	AuditsOpened collections.Item[uint64]
	// Creation height of each held fee (STRICT twin of HeldFee) -> feeds held_oldest_age. Under
	// deferral, "held is not lost" is only falsifiable through this date.
	HeldSince collections.Map[string, uint64]
	// Anchoring height of each seated committee (twin of AuditCommittee, keys jobId and __redo) ->
	// feeds oldest_lock_age_blocks. "Locked is not seized" is only falsifiable through this date.
	CommitteeSince collections.Map[string, uint64]
	// ADR-034: height of the LAST anchored commit, per miner -> on-chain proof of VITALITY.
	// Feeds (a) audit-jury eligibility and (b) the eviction of inactive miners.
	// Absent = "seen recently" (benign default: not exported at genesis, cf. keys.go).
	MinerLastCommitHeight collections.Map[string, uint64]
	// ADR-034: REGISTRATION height. Without it, "never committed" and "brand new" were the same signal
	// (a missing entry) and the vitality guard missed its target.
	MinerRegisteredHeight collections.Map[string, uint64]
	// ADR-037: POOL FREEZE height of each job, written at `OpenJob`. TWIN of `MinerRegisteredHeight` —
	// the two are COMPARED (`registered <= freeze`), so they are written and re-armed by the same code
	// at the same height (§3.b). Absent = ERROR: neither permissive (which would reopen identifier
	// grinding) nor restrictive (which would empty the committee silently).
	JobPoolFreezeHeight collections.Map[string, uint64]
	// Open audits awaiting self-resolution by timeout. Key = (deadlineHeight, jobId).
	PendingAuditResolve collections.KeySet[collections.Pair[int64, string]]
	// Second, appeal deadline (late reveal). Key = (appealDeadlineHeight, jobId). Dormant if appeal_window==0.
	PendingAppealResolve collections.KeySet[collections.Pair[int64, string]]
	// HELD fee (windowed hold) per jobId, awaiting audit finality (released to the primary when
	// vindicated or not audited; refunded to the client on clawback). Dormant if hold_bps==0.
	HeldFee collections.Map[string, uint64]
	// DEFERRED burn held per jobId (burned at finality, returned to the client on clawback).
	// Dormant if hold_bps==0.
	HeldBurn collections.Map[string, uint64]
	// Slashable liveness, sliding window: per miner, a 64-bit BITMASK of the absences over the last
	// epochs (bit0 = most recent) plus the last epoch processed. Dormant while avail_slash_bps==0.
	AvailFailCount       collections.Map[string, uint64]
	AvailFailWindowStart collections.Map[string, uint64]
	// Share of voting power (bps) held by the valid VRF contributors to the decentralized seed, per
	// height (written by the PreBlocker along with the seed). Feeds the dynamic ⌈2N/3⌉ floor IN POWER
	// (anti dust-Sybil) of committeeBaseSeed. Absent = static floor only (dormant).
	DecentralizedSeedContributorPower collections.Map[int64, uint64]
}

func NewKeeper(
	storeService corestore.KVStoreService,
	cdc codec.Codec,
	addressCodec address.Codec,
	authority []byte,

	bankKeeper types.BankKeeper,
	emissionKeeper types.EmissionKeeper,
	modelRegistryKeeper types.ModelRegistryKeeper,
) Keeper {
	if _, err := addressCodec.BytesToString(authority); err != nil {
		panic(fmt.Sprintf("invalid authority address %s: %s", authority, err))
	}

	sb := collections.NewSchemaBuilder(storeService)

	k := Keeper{
		storeService: storeService,
		cdc:          cdc,
		addressCodec: addressCodec,
		authority:    authority,

		bankKeeper:          bankKeeper,
		emissionKeeper:      emissionKeeper,
		modelRegistryKeeper: modelRegistryKeeper,
		Params:              collections.NewItem(sb, types.ParamsKey, "params", codec.CollValue[types.Params](cdc)),
		Miner:               collections.NewMap(sb, types.MinerKey, "miner", collections.StringKey, codec.CollValue[types.Miner](cdc)), Job: collections.NewMap(sb, types.JobKey, "job", collections.StringKey, codec.CollValue[types.Job](cdc)), Pools: collections.NewItem(sb, types.PoolsKey, "pools", codec.CollValue[types.Pools](cdc)), Commit: collections.NewMap(sb, types.CommitKey, "commit", collections.StringKey, codec.CollValue[types.Commit](cdc)), Beacon: collections.NewMap(sb, types.BeaconKey, "beacon", collections.StringKey, codec.CollValue[types.Beacon](cdc)), PendingReveal: collections.NewKeySet(sb, types.PendingRevealKey, "pending_reveal", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), AvailChallenge: collections.NewItem(sb, types.AvailChallengeKey, "avail_challenge", collections.StringValue), Available: collections.NewKeySet(sb, types.AvailableKey, "available", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), ValidatorVrfPubkey: collections.NewMap(sb, types.ValidatorVrfKey, "validator_vrf", collections.StringKey, collections.StringValue), AuditCommittee: collections.NewMap(sb, types.AuditCommitteeKey, "audit_committee", collections.StringKey, collections.StringValue), DecentralizedSeed: collections.NewMap(sb, types.DecentralizedSeedKey, "decentralized_seed", collections.Int64Key, collections.BytesValue), DecentralizedSeedContributors: collections.NewMap(sb, types.DecentralizedSeedContributorsKey, "decentralized_seed_contributors", collections.Int64Key, collections.Uint64Value), BlockHash: collections.NewMap(sb, types.BlockHashKey, "block_hash", collections.Int64Key, collections.BytesValue), PendingAudit: collections.NewKeySet(sb, types.PendingAuditKey, "pending_audit", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), MinerOptimisticCount: collections.NewMap(sb, types.MinerOptimisticCountKey, "miner_optimistic_count", collections.StringKey, collections.Uint64Value), AuditSelected: collections.NewKeySet(sb, types.AuditSelectedKey, "audit_selected", collections.StringKey), PendingHeldRelease: collections.NewKeySet(sb, types.PendingHeldReleaseKey, "pending_held_release", collections.StringKey), AuditsOpened: collections.NewItem(sb, types.AuditsOpenedKey, "audits_opened", collections.Uint64Value), HeldSince: collections.NewMap(sb, types.HeldSinceKey, "held_since", collections.StringKey, collections.Uint64Value), CommitteeSince: collections.NewMap(sb, types.CommitteeSinceKey, "committee_since", collections.StringKey, collections.Uint64Value), MinerLastCommitHeight: collections.NewMap(sb, types.MinerLastCommitHeightKey, "miner_last_commit_height", collections.StringKey, collections.Uint64Value), MinerRegisteredHeight: collections.NewMap(sb, types.MinerRegisteredHeightKey, "miner_registered_height", collections.StringKey, collections.Uint64Value), PendingAuditResolve: collections.NewKeySet(sb, types.PendingAuditResolveKey, "pending_audit_resolve", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), PendingAppealResolve: collections.NewKeySet(sb, types.PendingAppealResolveKey, "pending_appeal_resolve", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), HeldFee: collections.NewMap(sb, types.HeldFeeKey, "held_fee", collections.StringKey, collections.Uint64Value), HeldBurn: collections.NewMap(sb, types.HeldBurnKey, "held_burn", collections.StringKey, collections.Uint64Value), AvailFailCount: collections.NewMap(sb, types.AvailFailCountKey, "avail_fail_count", collections.StringKey, collections.Uint64Value), AvailFailWindowStart: collections.NewMap(sb, types.AvailFailWindowStartKey, "avail_fail_window_start", collections.StringKey, collections.Uint64Value), DecentralizedSeedContributorPower: collections.NewMap(sb, types.DecentralizedSeedContributorPowerKey, "decentralized_seed_contributor_power", collections.Int64Key, collections.Uint64Value), JobPoolFreezeHeight: collections.NewMap(sb, types.JobPoolFreezeHeightKey, "job_pool_freeze_height", collections.StringKey, collections.Uint64Value)}

	schema, err := sb.Build()
	if err != nil {
		panic(err)
	}
	k.Schema = schema

	return k
}

// GetAuthority returns the module's authority.
func (k Keeper) GetAuthority() []byte {
	return k.authority
}
