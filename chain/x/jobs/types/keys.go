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

// PendingRevealKey indexes the jobs awaiting the deferred reveal of their committee seed
// (anti-grinding). Composite key = (revealHeight, jobId). The EndBlocker freezes the beacon seed once
// that height is reached.
var PendingRevealKey = collections.NewPrefix("pending_reveal/value/")

// Availability.
// AvailChallengeKey: the current availability challenge, rolled from the AppHash at each epoch boundary.
var AvailChallengeKey = collections.NewPrefix("avail_challenge")

// AvailableKey: miners that proved their availability. Composite key = (epoch, minerId); paid out, then purged.
var AvailableKey = collections.NewPrefix("available/value/")

// VRF public key anchored per validator (key = bech32 operator account -> vrf_pubkey hex).
var ValidatorVrfKey = collections.NewPrefix("validator_vrf/value/")

// Decentralized VRF seed aggregated per height (the output of the vote extensions).
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

// Share of voting power (bps) held by the validators that contributed a valid VRF proof to the
// decentralized seed, per height, written at the same site as the seed (PreBlocker/aggregateSeed).
// Read by committeeBaseSeed to enforce a dynamic VRF floor of ⌈2N/3⌉ in power (>= 6667 bps), the
// faithful BFT translation: a count of validators could be griefed by dust sybils (inflate N at zero
// stake to force a permanent fallback to the legacy seed), and voting power cannot.
var DecentralizedSeedContributorPowerKey = collections.NewPrefix("decentralized_seed_contributor_power/value/")

// ADR-025: jobs settled optimistically and awaiting their audit draw. Composite key = (auditHeight, jobId).
// At auditHeight the EndBlocker draws H(seed || jobId) mod 10000 < audit_sample_bps and opens a dispute on a hit.
var PendingAuditKey = collections.NewPrefix("pending_audit/value/")

// PendingJobExpiryKey indexes the jobs whose escrow must be returned if nobody ever serves them.
// OpenJob moves the client's fee into the module account, and before this key set nothing brought it
// back out: a primary that never commits left the money in the module for good, with no timeout, no
// message and no authority path. The refusal to open a job below the miner floor (pool_freeze.go)
// closes the case where the job could never be verified; this closes the one where it is never served.
var PendingJobExpiryKey = collections.NewPrefix("pending_job_expiry/value/")

// ADR-025: count of optimistic jobs served per miner (anti-Sybil probation; key = minerId).
var MinerOptimisticCountKey = collections.NewPrefix("miner_optimistic_count/value/")

// AuditSelectedKey (ADR-034 C3) marks jobs whose audit draw was already won (auditDraw < bps) but whose
// opening was deferred for lack of a jury (deferAuditForSmallJury).
//
// Without this marker the retry ran the `auditDraw(seed', jobId)` lottery again against the seed of a
// new block: at audit_sample_bps=5000, an already-selected audit was cancelled one time out of two,
// and the payment was finalized (releaseHeld) with neither verification nor marker, giving the cheater
// back through the re-draw what the clawback takes away. A marked job is always audited: the retry
// skips the lottery and only retries the committee draw. The marker is removed when the audit opens
// (committee anchored) or when it resolves.
//
// Exported at genesis. It is not transient: this key is indexed by jobId, and jobs are carried across
// export/import. Losing the marker at migration would replay the lottery on the retry, reopening the
// one-in-two evasion this KeySet closes.
var AuditSelectedKey = collections.NewPrefix("audit_selected/value/")

// HeldSinceKey is the creation height of each held fee (jobId -> height of the settle that wrote
// HeldFee). It is a strict twin of HeldFee: written where HeldFee is written, removed where HeldFee is
// removed. An orphaned date would lie about the age, and a held fee without a date would be invisible
// to the alarm. It feeds `held_oldest_age` (HeldSummary query): since audits defer instead of clawing
// back, "held is not lost" is a promise, and this date is what can contradict it.
var HeldSinceKey = collections.NewPrefix("held_since/value/")

// CommitteeSinceKey is the anchoring height of each committee (AuditCommittee key -> height of the
// draw). It is a strict twin of AuditCommittee for the keys that carry seats (`<jobId>` and
// `<jobId>__redo`; never `__humandispute`, which summons nobody): written where the committee is
// anchored (anchorCommittee), removed where it is purged (clearAuditCommittee). It feeds
// `oldest_lock_age_blocks` (HeldSummary query): since the exit guard, a summoned juror is held until its
// audit resolves, so "locked is not seized" is a promise, and this date is what can contradict it.
var CommitteeSinceKey = collections.NewPrefix("committee_since/value/")

// AuditsOpenedKey (ADR-034 partition) is a monotonic counter of opened audits: an accepted human
// dispute, or the first selection by the audit lottery, whether it ends in an anchoring or in a
// deferral for lack of a jury. One increment per job, whatever path it takes afterwards.
//
// It is the independent source of the partition check: the six classes are counted from the job
// states, while `opened` is counted here, at the opening, so a sum that does not balance means a state
// is hiding. Without an independent source both sides came from the same enumeration, and a sum that
// cannot come out wrong verifies nothing (ADR-033).
var AuditsOpenedKey = collections.NewPrefix("audits_opened/value/")

// MinerLastCommitHeightKey (ADR-034) is the height of the last commit anchored per miner. It turns
// "registered" into "demonstrably alive", the way ADR-032 turned "registered" into "drawn". Two uses:
// (a) admit to the jury only miners that actually produce, otherwise silent identities occupy the
// seats and cause a run of no-quorum outcomes; (b) evict inactive miners, which `silence_slash` cannot
// reach, since it lives in `clawbackPayment` and so presupposes a payment, which never arrives for
// someone who produces nothing.
//
// Deliberately not exported at genesis. The absence of an entry is therefore read as "seen recently",
// a benign default: after an export/import nobody is pruned or excluded from a jury by mistake. A
// severe default would turn a migration into a collective punishment.
var MinerLastCommitHeightKey = collections.NewPrefix("miner_last_commit_height/value/")

// MinerRegisteredHeightKey (ADR-034) is the miner's registration height.
//
// Why a second date: with the commit date alone, a missing entry (`!ok`) encoded two opposite states,
// "new miner, innocent" and "registered long ago, has never produced anything", which is the target of
// the guard. The benign default applied to both, so the guard only caught miners that produced and
// then stopped, the honest population. Dating the registration makes `!ok` readable without weakening
// anything: h - registered <= window means new, and gets the benefit of the doubt; beyond it means
// registered and sterile, which is the target.
var MinerRegisteredHeightKey = collections.NewPrefix("miner_registered_height/value/")

// ADR-025 liveness: open audits awaiting self-resolution by timeout. Key = (deadlineHeight, jobId).
var PendingAuditResolveKey = collections.NewPrefix("pending_audit_resolve/value/")

// Second, appeal deadline: a permissionless late reveal by an honest primary that was merely offline.
// Key = (appealDeadlineHeight, jobId). Dormant: only fed when appeal_window > 0.
var PendingAppealResolveKey = collections.NewPrefix("pending_appeal_resolve/value/")

// ADR-032: the anchored audit committee, jobId -> ids of the juror members drawn at sampling time,
// joined by ','. This is the only list entitled to vote on that job (see auditVerdictTally). Without
// this anchoring, any registered miner could post a verdict and get an honest miner slashed.
var AuditCommitteeKey = collections.NewPrefix("audit_committee/value/")

// WorkCommitteeKey is the anchored work committee of a job (jobId -> ordered member ids joined by ',',
// element 0 being the primary). It is kept apart from AuditCommittee deliberately: three consumers
// read every AuditCommittee entry as a jury seat (hasOpenObligation, held_summary, the genesis export),
// so storing a work committee there would make a miner that merely did the work look like a summoned
// juror, two meanings in one structure, the shape ADR-045 catalogues.
//
// Why it exists (ADR-045, entry 12): the draw froze who may be drawn and when vitality is evaluated
// (ADR-037), but read the ordering weight live from the miner record. Slashing a member for an
// unrelated job therefore moved an open job's committee: the member that answered was no longer
// assigned, a stranger was, and settlement refused an incomplete committee, leaving an escrow with no
// path out. The committee is anchored the first time it is derivable and read from here afterwards.
var WorkCommitteeKey = collections.NewPrefix("work_committee/value/")

// HeldFeeKey is the optimistic payment withheld per jobId until the audit reaches finality.
var HeldFeeKey = collections.NewPrefix("held_fee/value/")

// PendingHeldReleaseKey lists jobs whose held-fee release (releaseHeld) failed on send and is still
// due. Without this queue, "the held fee remains claimable" was false: no handler ever replayed
// `releaseHeld`, so a fee kept back by a failed send became a slow confiscation. The EndBlocker sweeps
// this queue once per epoch and retries. It is a transient KeySet, not exported: on restart,
// resolution schedules again whatever is still due.
var PendingHeldReleaseKey = collections.NewPrefix("pending_held_release/value/")

// Deferred burn: the amount to burn, withheld per jobId until finality. It is burned on release or
// vindication, which makes it a tax on successful work, and returned to the client on clawback or
// slash. Dormant when hold_bps == 0.
var HeldBurnKey = collections.NewPrefix("held_burn/value/")

// ADR-037: freeze of the draw pool. jobId -> the height at which this job's pool is frozen, written
// once at `OpenJob` and shared by all three draws (primary, audit, redo). A miner is drawable only if
// its registration height is at or before that height.
//
// Without it, once the seed is public anyone can try `id` values offline (`sha256(seed|id)` is
// public), create a miner, and place itself in slot 0 of an already open job with a dust stake:
// measured on the exact comparator, 1 stake against 1,000,000 wins 3.5 % of draws at 2^16 tries, 38 %
// at 2^20, 94 % at 2^24. Stake weighting resists the splitting of a stake, never the choice of an
// identifier.
//
// Not exported at genesis: `InitGenesis` re-arms it at `initH`, like `MinerRegisteredHeight` and in the
// same block. The two anchors are compared to each other, so they must be written and re-armed by the
// same code at the same height (ADR-037 §3.b); otherwise their relative order would be an artifact of
// the migration code instead of a fact of history.
var JobPoolFreezeHeightKey = collections.NewPrefix("job_pool_freeze_height/value/")

// ADR-048 item 4: the three shares of the protocol cut, retained per jobId until finality. Until epoch
// 14 they went straight into `Pools` counters that nothing ever paid out, and a refund reversed "the
// cut" from whichever sub-pool held enough, taking from other jobs' shares. Per job, each share is paid
// out once (validators to fee_collector, team to team_address, treasury into Pools) or returned once.
var HeldValidatorShareKey = collections.NewPrefix("held_validator_share/value/")
var HeldTeamShareKey = collections.NewPrefix("held_team_share/value/")
var HeldTreasuryShareKey = collections.NewPrefix("held_treasury_share/value/")

// ADR-048 item 3: the Demand a job will credit its miner at finality. Credited at settlement, it would
// let a miner claim the subsidy it unlocks before the job is final, and a refund would then reverse
// the Demand but never the subsidy already taken.
var HeldDemandKey = collections.NewPrefix("held_demand/value/")

// ADR-048 item 5: the last availability epoch a miner proved, per minerId. `Available` is purged at
// every epoch boundary, so it cannot answer "was this miner present in the previous window?" once the
// window has closed; this can. Not exported: on import every miner is re-dated by its registration
// height, which grants the same newcomer grace a freshly registered miner gets.
var MinerLastPresentEpochKey = collections.NewPrefix("miner_last_present_epoch/value/")
