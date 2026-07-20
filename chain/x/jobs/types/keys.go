package types

import "cosmossdk.io/collections"

const (
	// ModuleName defines the module name
	ModuleName = "jobs"

	// StoreKey defines the primary module store key
	StoreKey = ModuleName

	// GovModuleName duplicates the gov module's name to avoid a dependency with x/gov.
	// It should be synced with the gov module's name if it is ever changed.
	// See: https://github.com/cosmos/cosmos-sdk/blob/v0.52.0-beta.2/x/gov/types/keys.go#L9
	GovModuleName = "gov"
)

// ParamsKey is the prefix to retrieve all Params
var ParamsKey = collections.NewPrefix("p_jobs")

var (
	PoolsKey = collections.NewPrefix("pools/value/")
)

// PendingRevealKey indexe les jobs en attente de RÉVÉLATION DIFFÉRÉE de comité (anti-grinding H6).
// Clé composite = (revealHeight, jobId). L'EndBlocker fige la graine du beacon quand la hauteur est atteinte.
var PendingRevealKey = collections.NewPrefix("pending_reveal/value/")

// Phase 1b -- DISPONIBILITÉ.
// AvailChallengeKey : défi de disponibilité courant (roulé depuis l'AppHash à chaque frontière d'époque).
var AvailChallengeKey = collections.NewPrefix("avail_challenge")

// AvailableKey : mineurs ayant prouvé leur disponibilité. Clé composite = (epoch, minerId) ; versé puis purgé.
var AvailableKey = collections.NewPrefix("available/value/")

// E4 : cle publique VRF ancree par validateur (cle = compte operateur bech32 -> vrf_pubkey hex).
var ValidatorVrfKey = collections.NewPrefix("validator_vrf/value/")

// E4 : graine VRF DÉCENTRALISÉE agrégée par hauteur (sortie des vote-extensions ; brique 4).
var DecentralizedSeedKey = collections.NewPrefix("decentralized_seed/value/")

// Bootstrap VRF (internal audit 2026-06-26) : NB de contributeurs (validateurs ayant fourni une preuve VRF valide)
// de la graine décentralisée, par hauteur. Lu par committeeBaseSeed pour le plancher committee_min_vrf_contributors.
var DecentralizedSeedContributorsKey = collections.NewPrefix("decentralized_seed_contributors/value/")

// ADR-022 PLEIN (liveness slashable, internal audit 2026-06-27) : compteur d'échecs de disponibilité par mineur dans la
// fenêtre glissante courante (clé = minerId) + époque de début de la fenêtre. Dormant tant que avail_slash_bps==0.
var AvailFailCountKey = collections.NewPrefix("avail_fail_count/value/")
var AvailFailWindowStartKey = collections.NewPrefix("avail_fail_window_start/value/")

// VE-01 (audit v7) : hash du bloc par hauteur, posé par le PreBlocker quand les vote-extensions sont
// actives. Sert d'alpha VRF lié au bloc + à RE-vérifier déterministe les extensions lors de l'agrégation.
var BlockHashKey = collections.NewPrefix("block_hash/value/")

// LOT SCALING (2026-07-01, durci post-red-team 2026-07-02) : part de PUISSANCE DE VOTE (bps) des validateurs
// ayant contribué une preuve VRF valide à la graine décentralisée, par hauteur (même site d'écriture que la
// graine, PreBlocker/aggregateSeed). Lu par committeeBaseSeed : plancher VRF DYNAMIQUE ⌈2N/3⌉ EN POUVOIR
// (≥6667 bps), la traduction BFT fidèle — un cardinal de validateurs serait griefable par sybil-poussière
// (gonfler N à stake nul → repli legacy permanent), la puissance ne l'est pas.
var DecentralizedSeedContributorPowerKey = collections.NewPrefix("decentralized_seed_contributor_power/value/")

// ADR-025 (M3) : jobs réglés OPTIMISTE en attente de TIRAGE D'AUDIT. Clé composite = (auditHeight, jobId).
// À auditHeight, l'EndBlocker tire H(seed‖jobId) mod 10000 < audit_sample_bps et ouvre une dispute si audité.
var PendingAuditKey = collections.NewPrefix("pending_audit/value/")

// ADR-025 M6 : compteur de jobs optimistes servis par mineur (probation anti-Sybil ; clé = minerId).
var MinerOptimisticCountKey = collections.NewPrefix("miner_optimistic_count/value/")

// ADR-025 liveness : audits ouverts en attente d'auto-résolution par timeout. Clé = (deadlineHeight, jobId).
var PendingAuditResolveKey = collections.NewPrefix("pending_audit_resolve/value/")

// PLAN-V2-FEE-HOLD §B : 2e échéance d'APPEL (révélation tardive permissionless du primaire honnête-hors-ligne).
// Clé = (appealDeadlineHeight, jobId). Dormant : alimenté seulement si appeal_window>0.
var PendingAppealResolveKey = collections.NewPrefix("pending_appeal_resolve/value/")

// PLAN-V2-FEE-HOLD §A : fee RETENUE (rétention fenêtrée du paiement optimiste) par jobId, jusqu'à finalité d'audit.
// ADR-032 — comité d'audit ANCRÉ : jobId -> IDs des membres-juges tirés au sort à l'échantillonnage,
// joints par ','. C'est la SEULE liste habilitée à voter sur ce job (cf. auditVerdictTally). Sans cet
// ancrage, n'importe quel mineur enregistré pouvait poster un verdict et faire slasher un honnête.
var AuditCommitteeKey = collections.NewPrefix("audit_committee/value/")

var HeldFeeKey = collections.NewPrefix("held_fee/value/")

// internal audit 2026-06-21 (ii) — burn DIFFÉRÉ : montant à brûler RETENU par jobId jusqu'à finalité (brûlé sur
// release/vindication = taxe sur travail RÉUSSI ; rendu au client sur clawback/slash). Dormant si hold_bps==0.
var HeldBurnKey = collections.NewPrefix("held_burn/value/")
