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

// PendingRevealKey indexes the jobs awaiting the DEFERRED REVEAL of their committee seed
// (anti-grinding). Composite key = (revealHeight, jobId). The EndBlocker freezes the beacon seed once
// that height is reached.
var PendingRevealKey = collections.NewPrefix("pending_reveal/value/")

// AVAILABILITY.
// AvailChallengeKey: the current availability challenge, rolled from the AppHash at each epoch boundary.
var AvailChallengeKey = collections.NewPrefix("avail_challenge")

// AvailableKey: miners that proved their availability. Composite key = (epoch, minerId); paid out, then purged.
var AvailableKey = collections.NewPrefix("available/value/")

// VRF public key anchored per validator (key = bech32 operator account -> vrf_pubkey hex).
var ValidatorVrfKey = collections.NewPrefix("validator_vrf/value/")

// DECENTRALIZED VRF seed aggregated per height (the output of the vote-extensions).
var DecentralizedSeedKey = collections.NewPrefix("decentralized_seed/value/")

// Number of contributors (validators that supplied a valid VRF proof) to the decentralized seed, per
// height. Read by committeeBaseSeed to enforce the committee_min_vrf_contributors floor.
var DecentralizedSeedContributorsKey = collections.NewPrefix("decentralized_seed_contributors/value/")

// ADR-022 (slashable liveness): count of availability failures per miner in the current sliding window
// (key = minerId), plus the epoch the window starts at. Dormant while avail_slash_bps == 0.
var AvailFailCountKey = collections.NewPrefix("avail_fail_count/value/")
var AvailFailWindowStartKey = collections.NewPrefix("avail_fail_window_start/value/")

// Block hash per height, written by the PreBlocker when vote-extensions are enabled. It serves as the
// block-bound VRF alpha and as the input to deterministically re-verify the extensions at aggregation.
var BlockHashKey = collections.NewPrefix("block_hash/value/")

// Share of VOTING POWER (bps) held by the validators that contributed a valid VRF proof to the
// decentralized seed, per height, written at the same site as the seed (PreBlocker/aggregateSeed).
// Read by committeeBaseSeed to enforce a DYNAMIC VRF floor of ⌈2N/3⌉ IN POWER (>= 6667 bps), which is
// the faithful BFT translation: a count of validators would be griefable by dust sybils (inflate N at
// zero stake to force a permanent fallback to the legacy seed), voting power is not.
var DecentralizedSeedContributorPowerKey = collections.NewPrefix("decentralized_seed_contributor_power/value/")

// ADR-025: jobs settled OPTIMISTICALLY and awaiting their AUDIT DRAW. Composite key = (auditHeight, jobId).
// At auditHeight the EndBlocker draws H(seed‖jobId) mod 10000 < audit_sample_bps and opens a dispute on a hit.
var PendingAuditKey = collections.NewPrefix("pending_audit/value/")

// PendingJobExpiryKey indexes the jobs whose escrow must be returned if nobody ever serves them.
// OpenJob moves the client's fee into the module account, and until this key set existed NOTHING
// brought it back out: a primary that simply never commits left the money in the module for good --
// no timeout, no message, no authority path. The refusal to open a job below the miner floor
// (pool_freeze.go) closes the case where the job could never be VERIFIED; this closes the one where
// it is never SERVED at all.
var PendingJobExpiryKey = collections.NewPrefix("pending_job_expiry/value/")

// ADR-025: count of optimistic jobs served per miner (anti-Sybil probation; key = minerId).
var MinerOptimisticCountKey = collections.NewPrefix("miner_optimistic_count/value/")

// AuditSelectedKey (ADR-034 C3) — jobs whose audit DRAW was ALREADY WON (auditDraw < bps) but whose
// opening was DEFERRED for lack of a jury (deferAuditForSmallJury).
//
// Without this marker the retry ran the `auditDraw(seed', jobId)` lottery again against the seed of a
// NEW block: at audit_sample_bps=5000, an already-selected audit was therefore cancelled one time out
// of two, and the payment finalized (releaseHeld) with neither verification nor marker — the cheater's
// escape handed back through the side door, since what the clawback takes away the re-draw gives back.
// A marked job is audited, full stop: the retry skips the lottery and only retries the committee draw.
// The marker is removed when the audit opens (committee anchored) or when it resolves.
//
// EXPORTED AT GENESIS. It is not transient: this key is indexed by jobId, and jobs are carried across
// export/import. Losing the marker at migration would replay the lottery on the retry, reopening the
// one-in-two evasion this KeySet closes.
var AuditSelectedKey = collections.NewPrefix("audit_selected/value/")

// HeldSinceKey — the CREATION height of each held fee (jobId -> height of the settle that wrote
// HeldFee). A STRICT twin of HeldFee: written where HeldFee is written, removed where HeldFee is
// removed. An orphaned date would lie about the age; a held fee without a date would be invisible to
// the alarm. It feeds `held_oldest_age` (HeldSummary query): since audits defer instead of clawing
// back, "held is not lost" is a promise, and this date is what can CONTRADICT it.
var HeldSinceKey = collections.NewPrefix("held_since/value/")

// CommitteeSinceKey — the ANCHORING height of each committee (AuditCommittee key -> height of the
// draw). A STRICT twin of AuditCommittee for the keys that carry SEATS (`<jobId>` and `<jobId>__redo`;
// never `__humandispute`, which summons nobody): written where the committee is anchored
// (anchorCommittee), removed where it is purged (clearAuditCommittee). It feeds
// `oldest_lock_age_blocks` (HeldSummary query): since the exit guard, a summoned juror is HELD until
// its audit resolves — "locked is not seized" is a promise, and this date is what can CONTRADICT it.
var CommitteeSinceKey = collections.NewPrefix("committee_since/value/")

// AuditsOpenedKey (ADR-034 partition) — MONOTONIC counter of OPENED audits: an accepted human dispute,
// or the first SELECTION by the audit lottery, whether it ends in an anchoring or in a deferral for
// lack of a jury. One increment per job, whatever path it takes afterwards.
//
// This is the INDEPENDENT SOURCE of the partition check: the six classes are counted from the job
// states, while `opened` is counted HERE, at the opening — so if the sum does not balance, a state is
// hiding. Without an independent source the sum was a TAUTOLOGY, both sides coming from the same
// enumeration, and a sum that cannot come out wrong verifies nothing (ADR-033).
var AuditsOpenedKey = collections.NewPrefix("audits_opened/value/")

// MinerLastCommitHeightKey (ADR-034) — height of the LAST commit anchored per miner. It turns
// "registered" into "demonstrably ALIVE", the way ADR-032 turned "registered" into "drawn". Two uses:
// (a) admit to the jury only miners that actually produce, otherwise silent identities occupy the
// seats and cause a run of no-quorum outcomes; (b) evict inactive miners, which `silence_slash` cannot
// reach, since it lives in `clawbackPayment` and therefore presupposes a payment — which never arrives
// for someone who produces nothing.
//
// NOT EXPORTED AT GENESIS, deliberately. The ABSENCE of an entry is therefore read as "seen recently",
// a BENIGN default: after an export/import nobody is pruned or excluded from a jury by mistake. A
// severe default would turn a migration into a collective punishment.
var MinerLastCommitHeightKey = collections.NewPrefix("miner_last_commit_height/value/")

// MinerRegisteredHeightKey (ADR-034) — the miner's REGISTRATION height.
//
// Why a SECOND date. With the commit date alone, the absence of an entry (`!ok`) encoded two opposite
// states: "new miner, innocent" and "registered long ago, has NEVER produced anything" — the very
// target of the guard. The benign default applied to both, so the guard only bit on miners that
// produced and THEN stopped: the HONEST population. One signal for two states is the most expensive
// form of defect, because reading it makes the design look sound. Dating the registration makes `!ok`
// readable without weakening anything: h − registered <= window means new, and gets the benefit of the
// doubt; beyond it means registered and sterile, which is the target.
var MinerRegisteredHeightKey = collections.NewPrefix("miner_registered_height/value/")

// ADR-025 liveness: open audits awaiting self-resolution by timeout. Key = (deadlineHeight, jobId).
var PendingAuditResolveKey = collections.NewPrefix("pending_audit_resolve/value/")

// Second, APPEAL deadline: a permissionless late reveal by an honest primary that was merely offline.
// Key = (appealDeadlineHeight, jobId). Dormant: only fed when appeal_window > 0.
var PendingAppealResolveKey = collections.NewPrefix("pending_appeal_resolve/value/")

// ADR-032 — the ANCHORED audit committee: jobId -> ids of the juror members drawn at sampling time,
// joined by ','. This is the ONLY list entitled to vote on that job (see auditVerdictTally). Without
// this anchoring, any registered miner could post a verdict and get an honest miner slashed.
var AuditCommitteeKey = collections.NewPrefix("audit_committee/value/")

// WorkCommitteeKey -- the ANCHORED work committee of a job (jobId -> ordered member ids joined by
// ',', element 0 being the primary). A DISTINCT store from AuditCommittee, and the distinction is the
// point: three consumers read every AuditCommittee entry as a JURY SEAT (hasOpenObligation,
// held_summary, the genesis export), so lodging a WORK committee there would make a miner that merely
// did the work look like a summoned juror -- two meanings in one structure, which is the very shape
// ADR-045 catalogues.
//
// WHY IT EXISTS (ADR-045, entry 12). The draw froze WHO may be drawn and WHEN vitality is evaluated
// (ADR-037), but read the ordering weight LIVE from the miner record. Slashing a MEMBER for an
// unrelated job therefore moved an open job's committee: the member that answered was no longer
// assigned, a stranger was, and the settlement refused for an incomplete committee -- an escrow with
// no path left. The committee is anchored the first time it is DERIVABLE and read from here after.
var WorkCommitteeKey = collections.NewPrefix("work_committee/value/")

// HeldFeeKey — the optimistic payment withheld per jobId until the audit reaches finality.
var HeldFeeKey = collections.NewPrefix("held_fee/value/")

// PendingHeldReleaseKey — jobs whose held-fee release (releaseHeld) FAILED on send and is still DUE.
// Without this queue, "the held fee remains claimable" was FALSE: no handler ever replayed
// `releaseHeld`, so a fee kept back by a failed send became a slow confiscation. The EndBlocker sweeps
// this queue once per epoch and retries. A transient KeySet, not exported: on restart, resolution
// re-schedules whatever is still due.
var PendingHeldReleaseKey = collections.NewPrefix("pending_held_release/value/")

// DEFERRED BURN: the amount to burn, withheld per jobId until finality. It is burned on
// release/vindication, which makes it a tax on SUCCESSFUL work, and returned to the client on
// clawback/slash. Dormant when hold_bps == 0.
var HeldBurnKey = collections.NewPrefix("held_burn/value/")

// ADR-037 — FREEZE OF THE DRAW POOL. jobId -> the height at which THIS job's pool is frozen, written
// ONCE at `OpenJob` and shared by ALL THREE draws (primary, audit, redo). A miner is drawable only if
// its registration height is at or before that height.
//
// Without it, once the seed is public anyone can try `id` values offline (`sha256(seed|id)` is public),
// create a miner, and place itself in slot 0 of an ALREADY OPEN job with a dust stake: measured on the
// exact comparator, 1 stake against 1,000,000 wins 3.5 % of draws at 2^16 tries, 38 % at 2^20, 94 % at
// 2^24. Stake weighting resists the SPLITTING of a stake, never the choice of an identifier.
//
// NOT EXPORTED at genesis: re-armed at `initH` by `InitGenesis`, exactly like `MinerRegisteredHeight`
// and IN THE SAME BLOCK. The two anchors are COMPARED to each other, so they must be written AND
// re-armed by the same code at the same height (ADR-037 §3.b) — otherwise their relative order is not
// a fact of history but an artifact of the migration code.
var JobPoolFreezeHeightKey = collections.NewPrefix("job_pool_freeze_height/value/")
