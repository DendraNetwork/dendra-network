package keeper

import (
	"fmt"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/address"
	corestore "cosmossdk.io/core/store"
	"github.com/cosmos/cosmos-sdk/codec"

	"dendra/x/jobs/types"
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
	Job            collections.Map[string, types.Job]
	Pools          collections.Item[types.Pools]
	Commit         collections.Map[string, types.Commit]
	Beacon         collections.Map[string, types.Beacon]
	// PendingReveal indexe les jobs en attente de révélation de comité (révélation différée H6).
	// Clé = (revealHeight, jobId) ; l'EndBlocker fige la graine du beacon à revealHeight.
	PendingReveal collections.KeySet[collections.Pair[int64, string]]
	// Phase 1b -- disponibilité : défi courant (roulé chaque époque) + présences prouvées par époque.
	AvailChallenge collections.Item[string]
	Available      collections.KeySet[collections.Pair[int64, string]]
	// E4 : cle publique VRF par validateur (compte operateur bech32 -> vrf_pubkey hex). Consultee par
	// les handlers ABCI++ vote-extensions ; dormante tant que les vote-extensions sont OFF.
	ValidatorVrfPubkey collections.Map[string, string]
	// ADR-032 : comité d'audit ANCRÉ (jobId -> IDs joints par ','). Écrit à l'échantillonnage par
	// runOptimisticAudit ; SEULS ses membres comptent dans auditVerdictTally. Avant cet ancrage, le tally
	// n'authentifiait les votants que comme « mineur enregistré » -> 4 identités à min_stake pouvaient
	// faire slasher 80 % du stake d'un mineur HONNÊTE (faille critique — cf. ADR-032).
	AuditCommittee collections.Map[string, string]
	// E4 : graine décentralisée agrégée des vote-extensions VRF, par hauteur de bloc (brique 4).
	DecentralizedSeed collections.Map[int64, []byte]
	// Bootstrap VRF (internal audit 2026-06-26) : NB de contributeurs de la graine décentralisée par hauteur, pour le
	// plancher committee_min_vrf_contributors (anti-régression silencieuse : graine sous-décentralisée -> alerte+repli).
	DecentralizedSeedContributors collections.Map[int64, uint64]
	// VE-01 : hash du bloc par hauteur (posé par le PreBlocker quand les vote-extensions sont actives).
	BlockHash collections.Map[int64, []byte]
	// ADR-025 (M3) : jobs réglés OPTIMISTE en attente de tirage d'audit. Clé = (auditHeight, jobId).
	// L'EndBlocker tire H(seed‖jobId) mod 10000 < audit_sample_bps -> +disputed si audité. Dormant (mode 0).
	PendingAudit collections.KeySet[collections.Pair[int64, string]]
	// ADR-025 M6 : compteur de jobs optimistes servis par mineur (probation anti-Sybil ; clé = minerId).
	MinerOptimisticCount collections.Map[string, uint64]
	// ADR-025 liveness : audits ouverts en attente d'auto-résolution par timeout. Clé = (deadlineHeight, jobId).
	PendingAuditResolve collections.KeySet[collections.Pair[int64, string]]
	// PLAN-V2-FEE-HOLD §B : 2e échéance d'appel (révélation tardive). Clé = (appealDeadlineHeight, jobId). Dormant si appeal_window==0.
	PendingAppealResolve collections.KeySet[collections.Pair[int64, string]]
	// PLAN-V2-FEE-HOLD §A : fee RETENUE (rétention fenêtrée) par jobId, en attente de finalité d'audit
	// (libérée au primaire si vindiqué/non-audité ; rembourse le client si clawback/triche). Dormant si hold_bps==0.
	HeldFee collections.Map[string, uint64]
	// internal audit 2026-06-21 (ii) : burn DIFFÉRÉ retenu par jobId (brûlé à finalité, rendu au client si clawback). Dormant si hold_bps==0.
	HeldBurn collections.Map[string, uint64]
	// ADR-022 PLEIN (liveness slashable, internal audit 2026-06-27 ; SLIDING-WINDOW lot scaling 2026-07-01) : par mineur,
	// BITMASK 64 bits des absences aux dernières époques (bit0 = plus récente) + dernière époque traitée.
	// (Réutilise les 2 collections du tumbling v1 — mêmes types uint64, sémantique changée AVANT tout armement live.)
	// Dormant tant que avail_slash_bps==0.
	AvailFailCount       collections.Map[string, uint64]
	AvailFailWindowStart collections.Map[string, uint64]
	// LOT SCALING (2026-07-01, durci 2026-07-02) : part de puissance de vote (bps) des contributeurs VRF valides
	// de la graine décentralisée, par hauteur (posée par le PreBlocker avec la graine). Plancher dynamique ⌈2N/3⌉
	// EN POUVOIR (anti sybil-poussière) de committeeBaseSeed. Absente = plancher statique seul (dormant).
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
		Params:         collections.NewItem(sb, types.ParamsKey, "params", codec.CollValue[types.Params](cdc)),
		Miner:      collections.NewMap(sb, types.MinerKey, "miner", collections.StringKey, codec.CollValue[types.Miner](cdc)), Job: collections.NewMap(sb, types.JobKey, "job", collections.StringKey, codec.CollValue[types.Job](cdc)), Pools: collections.NewItem(sb, types.PoolsKey, "pools", codec.CollValue[types.Pools](cdc)), Commit: collections.NewMap(sb, types.CommitKey, "commit", collections.StringKey, codec.CollValue[types.Commit](cdc)), Beacon: collections.NewMap(sb, types.BeaconKey, "beacon", collections.StringKey, codec.CollValue[types.Beacon](cdc)), PendingReveal: collections.NewKeySet(sb, types.PendingRevealKey, "pending_reveal", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), AvailChallenge: collections.NewItem(sb, types.AvailChallengeKey, "avail_challenge", collections.StringValue), Available: collections.NewKeySet(sb, types.AvailableKey, "available", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), ValidatorVrfPubkey: collections.NewMap(sb, types.ValidatorVrfKey, "validator_vrf", collections.StringKey, collections.StringValue), AuditCommittee: collections.NewMap(sb, types.AuditCommitteeKey, "audit_committee", collections.StringKey, collections.StringValue), DecentralizedSeed: collections.NewMap(sb, types.DecentralizedSeedKey, "decentralized_seed", collections.Int64Key, collections.BytesValue), DecentralizedSeedContributors: collections.NewMap(sb, types.DecentralizedSeedContributorsKey, "decentralized_seed_contributors", collections.Int64Key, collections.Uint64Value), BlockHash: collections.NewMap(sb, types.BlockHashKey, "block_hash", collections.Int64Key, collections.BytesValue), PendingAudit: collections.NewKeySet(sb, types.PendingAuditKey, "pending_audit", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), MinerOptimisticCount: collections.NewMap(sb, types.MinerOptimisticCountKey, "miner_optimistic_count", collections.StringKey, collections.Uint64Value), PendingAuditResolve: collections.NewKeySet(sb, types.PendingAuditResolveKey, "pending_audit_resolve", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), PendingAppealResolve: collections.NewKeySet(sb, types.PendingAppealResolveKey, "pending_appeal_resolve", collections.PairKeyCodec(collections.Int64Key, collections.StringKey)), HeldFee: collections.NewMap(sb, types.HeldFeeKey, "held_fee", collections.StringKey, collections.Uint64Value), HeldBurn: collections.NewMap(sb, types.HeldBurnKey, "held_burn", collections.StringKey, collections.Uint64Value), AvailFailCount: collections.NewMap(sb, types.AvailFailCountKey, "avail_fail_count", collections.StringKey, collections.Uint64Value), AvailFailWindowStart: collections.NewMap(sb, types.AvailFailWindowStartKey, "avail_fail_window_start", collections.StringKey, collections.Uint64Value), DecentralizedSeedContributorPower: collections.NewMap(sb, types.DecentralizedSeedContributorPowerKey, "decentralized_seed_contributor_power", collections.Int64Key, collections.Uint64Value)}

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
