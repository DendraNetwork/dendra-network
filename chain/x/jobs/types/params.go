package types

import (
	"encoding/hex"
	"fmt"

	"cosmossdk.io/math"
	"github.com/cosmos/cosmos-sdk/types/bech32"
)

// NewParams builds a Params instance.
//
// Four positional arguments were dropped along with the proto fields they fed: miner_vest_blocks,
// burn_bps, reporter_bps and demand_client_cap (numbers 6, 8, 9 and 12, now `reserved`). Each of them
// was displayed by `query jobs params` and read by no production line. Because the arguments are
// positional, a call site that was not updated does not compile, so the removal cannot be half-applied.
func NewParams(
	minStake, protocolFeeBps, validatorRewardBps, teamFeeBps,
	minerVestBps, slashLeakBps,
	epochBlocks, workGateBps, feeBurnBps uint64,
) Params {
	return Params{
		MinStake:           minStake,
		ProtocolFeeBps:     protocolFeeBps,
		ValidatorRewardBps: validatorRewardBps,
		TeamFeeBps:         teamFeeBps,
		MinerVestBps:       minerVestBps,
		SlashLeakBps:       slashLeakBps,
		EpochBlocks:        epochBlocks,
		WorkGateBps:        workGateBps,
		FeeBurnBps:         feeBurnBps,
	}
}

// DefaultParams returns the shipped parameter set.
//
// The Go default is verification_mode=0: k=3 cosine redundancy, without the pro-honest bar of the
// optimistic audit, which only guards mode 1. Anyone starting the chain without the docker entrypoint
// (which arms mode 1, fee-hold and silence_slash at genesis) therefore runs in mode 0. The deterrence
// floors in Validate() guard both paths; the audit bar applies only to armed genesis files. This is
// documented rather than flipped, because flipping the default is a consensus change.
//
// No absolute committee threshold is written here. Such a figure was once framed around a 5-seat
// committee, and the audit draw targets 15; the bar is relative (⌈2/3 of anchored seats⌉), and an absolute
// number copied into this comment would go stale silently, which is how a bar written for five seats
// ends up applied to fifteen.
//
// Flipping this default requires adapting the keeper tests that depend on it (hold_bps=0 implies the
// strict non-hold settlement path).
func DefaultParams() Params {
	p := NewParams(
		1000,  // min_stake
		1500,  // protocol_fee_bps (15 %)
		5000,  // validator_reward_bps (50 % of the cut)
		2000,  // team_fee_bps (20 % of the cut)
		3000,  // miner_vest_bps (30 %); held back at legacy settlement, with no release schedule
		8000,  // slash_leak_bps (80 %)
		100,   // epoch_blocks
		15000, // work_gate_bps (1.5x)
		500,   // fee_burn_bps (5 % soft burn), burned for real via BurnCoins at settlement
	)
	p.AvailRequireDemand = true // Availability anti-farming is on by default on an incentivised testnet.
	// Governable parameters, all dormant at 0, so the default behaviour is unchanged.
	// Assigning 0 explicitly documents the dormancy; the Go zero value is already 0, so the redundancy
	// is harmless and the intent becomes readable.
	p.SilenceSlashBps = 0 // 0 = light clawback only
	p.AppealWindow = 0    // 0 = restitution through governance; reserved, not consumed yet
	// audit_min_quorum: 0 falls back to the participation floor (⌈N/2⌉+1) plus the stake majority.
	// Above 0 it is a lower bound on the quorum, not the quorum itself: the effective
	// threshold is max(this parameter, ⌈2/3 x anchored seats⌉) and the stake majority. With a 15-seat
	// draw, an absolute threshold of 4 is 27 %, far from the near-unanimity such a number suggests,
	// which is why the relative term exists.
	p.AuditMinQuorum = 0
	p.HoldBps = 0                     // 0 = full optimistic payment immediately; >0 = windowed hold until finality
	p.CommitteeMinVrfContributors = 0 // 0 = no floor (dormant); >0 = a decentralised seed built from fewer than N contributors raises an alert and falls back, never halts the chain
	// Slashable availability (VRF challenge). All dormant at 0: inert until AvailEpochBlocks
	// is set and these parameters are posted.
	p.AvailThresholdBps = 0   // 0 = no semantic check on the challenge; >0 = cosine threshold
	p.AvailDeadlineBlocks = 0 // 0 = no challenge is due; >0 = blocks granted to answer
	p.AvailSlashBps = 0       // 0 = no availability slash; >0 = fraction of the stake slashed
	p.AvailSlashMax = 0       // 0 = no cap; >0 = absolute cap (udndr) on the availability slash
	p.AvailFailWindow = 0     // 0 = quorum never reached; >0 = sliding window over which failures are counted
	p.AvailFailK = 0          // 0 = never slash (full false-positive protection); >0 = failures required within the window
	// The audit unwind. Written explicitly though it is the zero value, because dormant is a decision
	// here: shipping the unwind armed would choose, on the operator's behalf, how long a debt may stay
	// unresolvable on their network, a number the code cannot derive. 0 keeps every deferral
	// byte-identical to the behaviour before this parameter existed.
	p.AuditUnwindBlocks = 0
	return p
}

// Validate checks the parameter set.
func (p Params) Validate() error {
	if p.ProtocolFeeBps > 10000 {
		return fmt.Errorf("protocol_fee_bps > 10000")
	}
	if p.ValidatorRewardBps+p.TeamFeeBps > 10000 {
		return fmt.Errorf("validator+team > 100%% of the cut")
	}
	if p.MinerVestBps > 10000 {
		return fmt.Errorf("miner_vest_bps > 10000")
	}
	if p.AvailPayoutBps > 10000 {
		return fmt.Errorf("avail_payout_bps > 10000")
	}
	// A slash above 100 % of a bond, or a burn above 100 % of a fee, underflows a uint64.
	// Nine bps parameters were bounded from above here and these two were not: `slash_leak_bps` had only
	// a floor (2500, in optimistic mode) and `fee_burn_bps` had no bound at all. Measured: `Validate`
	// returned nil at 20000 for both, a permissionless `VerifySemantic` then succeeded, and the punished
	// miner's bond went from 1000 to 18446744073709550616 while the Treasury was credited 2000 coins that
	// do not exist. The slashed cheater became by far the largest bond on the chain, and the audit jury
	// draw reads the raw stake (no assignment cap applies there), so it would take every seat offered.
	//
	// A single mistyped percent-for-bps posts this: 80 % is 8000, but 80000 is what a percent-shaped
	// habit writes. Four of the seven slash sites are permissionless, so once the parameter is in, anyone
	// triggers it for the price of gas.
	//
	// Only these two get a check, although four bps fields have no direct upper test, for two reasons.
	// `validator_reward_bps` is bounded by the sum check above (a search for `> 10000` on the field alone
	// misses it). `work_gate_bps` is deliberately above 10000: the live chain serves 15000, because it
	// scales a subsidy against demand rather than splitting a total, and bounding it for consistency
	// would refuse the running configuration. The rule is that a share of X cannot exceed X; not every
	// bps is a share.
	if p.SlashLeakBps > 10000 {
		return fmt.Errorf("slash_leak_bps > 10000: a slash cannot exceed the bond it takes from (uint64 underflow: the punished miner would end up with the largest stake on the chain)")
	}
	if p.FeeBurnBps > 10000 {
		return fmt.Errorf("fee_burn_bps > 10000: a burn cannot exceed the fee it is taken from (uint64 underflow on the distributable remainder)")
	}
	// The order of the two vitality windows is an invariant.
	// `juror_freshness_blocks` (stop summoning) must come before `miner_prune_blocks` (evict from the
	// registry). The reverse order evicts a miner the chain still considers summonable as a juror, which
	// is inconsistent in itself and puts the second guard of `hasOpenObligation` in a state nobody
	// designed for. Both are governable, and checking their values in isolation is not
	// enough: a parameter whose relative order carries meaning must be bounded.
	// The dormant pattern is kept: 0 means "compiled-in constant", so governing only one of the two
	// triggers nothing; only explicitly posted values are compared.
	if p.MinerPruneBlocks > 0 && p.JurorFreshnessBlocks > 0 && p.MinerPruneBlocks < p.JurorFreshnessBlocks {
		return fmt.Errorf("miner_prune_blocks (%d) < juror_freshness_blocks (%d): this evicts from the registry a miner still summonable as a juror",
			p.MinerPruneBlocks, p.JurorFreshnessBlocks)
	}
	// A second ordering relation, distinct from the one above. The pool freeze bounds
	// eligibility by the job's age (a miner registered after the job opened will never judge it), and
	// eviction bounds it by inactivity. If `miner_prune_blocks < dispute_window`, the registry evicts the
	// jurors the freeze still allows to judge, and a job old enough becomes unjudgeable. The degradation
	// is a deferral, never a sanction, but it is real and silent. This relation and the one above
	// (`prune >= juror_freshness`) do not imply each other, and enforcing one leaves the other open.
	// Same dormant pattern: only posted values are compared.
	// A third ordering relation, on the same dormant pattern (a pair of zeros
	// compares nothing). An unwind age below the resolve timeout fires before the audit has had its own
	// scheduled retry, so the retention would be undone while the verification it waits for is still
	// pending: the parameter would replace the deferral instead of bounding it, the opposite of what the
	// record decides. The bar is derived from the timeout already in Params and never retyped:
	// `auditDeferStride` lives in the keeper, and copying its value here is how two numbers stop agreeing.
	if p.AuditUnwindBlocks > 0 && p.AuditResolveTimeout > 0 && p.AuditUnwindBlocks < p.AuditResolveTimeout {
		return fmt.Errorf("audit_unwind_blocks (%d) < audit_resolve_timeout (%d): the retention would be unwound before the audit it waits for has been retried even once",
			p.AuditUnwindBlocks, p.AuditResolveTimeout)
	}
	if p.MinerPruneBlocks > 0 && p.DisputeWindow > 0 && p.MinerPruneBlocks < p.DisputeWindow {
		return fmt.Errorf("miner_prune_blocks (%d) < dispute_window (%d): this evicts from the registry the only jurors the pool freeze still allows to judge this job",
			p.MinerPruneBlocks, p.DisputeWindow)
	}
	if p.CommitteeSeedSource > 1 {
		return fmt.Errorf("committee_seed_source must be 0 (fallback) or 1 (decentralised vrf)")
	}
	if p.VrfBeaconPubkey != "" {
		if b, err := hex.DecodeString(p.VrfBeaconPubkey); err != nil || len(b) != 32 {
			return fmt.Errorf("invalid vrf_beacon_pubkey (expected 32 hex-encoded bytes)")
		}
	}
	// Optimistic verification, dormant by default.
	if p.VerificationMode > 1 {
		return fmt.Errorf("verification_mode must be 0 (redundant) or 1 (optimistic)")
	}
	if p.AuditSampleBps > 10000 {
		return fmt.Errorf("audit_sample_bps > 10000")
	}
	// Anti-evasion: in optimistic mode the audit must be able to end in a slash. Without
	// these cross bounds the optimistic slash is inert even when enabled: with no sample no job is
	// ever audited, and if the resolve timeout is not strictly after the dispute window, auto
	// resolution (clawback or vindication) fires before adjudication is admissible.
	if p.VerificationMode == 1 {
		if p.AuditSampleBps == 0 {
			return fmt.Errorf("verification_mode=1 requires audit_sample_bps > 0 (otherwise no audit is ever drawn)")
		}
		if p.DisputeWindow == 0 {
			return fmt.Errorf("verification_mode=1 requires dispute_window > 0")
		}
		// Deterrence floor, following the pattern "armed implies the full machinery". Without
		// it, governance can post slash_leak_bps=0 on an armed optimistic chain: a caught cheater loses
		// nothing, so cheating and self-dealing become +EV, and neither Validate nor the wash invariant
		// catches it (that invariant guards the subsidy wash, not the slash). The floor of 2500 (25 % of
		// the stake) is structural, meaning "never symbolic"; the exact -EV economics at around 10 %
		// audit are calibrated elsewhere (shipped value 8000). Redundant mode is untouched.
		if p.SlashLeakBps < 2500 {
			return fmt.Errorf("verification_mode=1 requires slash_leak_bps >= 2500 (deterrence must never be symbolic; shipped value 8000)")
		}
		if p.AuditSampleBps < 500 {
			return fmt.Errorf("verification_mode=1 requires audit_sample_bps >= 500 (below a 5%% audit rate, cheating is +EV again even at full slash)")
		}
		// Lower bound on `audit_min_quorum`, a parameter that previously had none.
		// It drives more than the slash, which the ⌈2/3⌉ of anchored seats protects: through
		// `effectiveSlashFloor` it also drives the vindication floor. At 1, a single juror was enough to
		// release a cheater's held payment and deny the client its refund, so governance could disarm
		// half the guarantee without naming it. The floor matches `auditSlashFloor()` on the keeper side
		// (⌈AuditLegacyFloorBasis/2⌉+1 = 4); shipped value 4. 0 remains allowed: it is the explicit
		// fallback, not a silent weakening.
		if p.AuditMinQuorum > 0 && p.AuditMinQuorum < AuditFallbackFloor {
			return fmt.Errorf("verification_mode=1 requires audit_min_quorum >= 4 when it is governed (below this floor an isolated juror vindicates a cheater; 0 selects the fallback floor)")
		}
		// Disputing must never be free. Opening a dispute escrows `dispute_bond`, and an unfounded
		// dispute forfeits it; at 0 the forfeiture forfeits nothing, so disputing every settled job
		// costs only gas. Optimistic mode requires `dispute_window > 0` (bound above), so "mode 1
		// with a zero bond" is reachable and would disarm an advertised guard.
		if p.DisputeWindow > 0 && p.DisputeBond == 0 {
			return fmt.Errorf("verification_mode=1 requires dispute_bond > 0 (disputes are active: without a bond, disputing is free and the anti-grief forfeiture forfeits nothing; suggested anchor: min_stake)")
		}
		// The committee seed must not be predictable by the party the committee judges.
		//
		// A deferred reveal is required, and a decentralised seed does not replace it.
		//
		// The two settings answer different attackers, which is why only one of them is mandatory:
		//   - `committee_seed_source = 1` makes the seed an aggregate no single actor controls. That
		//     defends against the job creator, which is a party to the transaction and nothing more.
		//   - `committee_reveal_delay > 0` keeps the seed empty at open time and fixes it later on the
		//     AppHash of a future block. Only that one defends against the proposer.
		//
		// With `delay = 0` the seed is fixed inside the opening block, and the proposer composes that
		// block: it reads the pending MsgOpenJob, assembles the vote extensions the seed is built from,
		// and chooses the block time the legacy seed is built from. It therefore knows the committee
		// before it commits to proposing, and can reorder or withhold until the draw suits it. A
		// decentralised source does not change that: being unable to pick the value alone does not
		// prevent seeing it before acting.
		//
		// In redundant mode (k=3) peer divergence limits the gain; in optimistic mode the audit
		// committee is the only verification, so choosing it amounts to choosing the verdict.
		if p.CommitteeRevealDelay == 0 {
			return fmt.Errorf("verification_mode=1 requires committee_reveal_delay>0 (deferred reveal): with a zero delay the committee seed is fixed inside the opening block, which the proposer composes: it reads the pending job, builds the seed and knows the committee before proposing. committee_seed_source=1 does not substitute: it stops a single actor from picking the seed, and does nothing against a proposer who sees it in time to act")
		}
	}

	// A dispute that can be opened must have a deadline, in every mode.
	// `DisputeVerdict` gates only on `dispute_window`, and the appointment that guarantees an open
	// dispute cannot hang forever is booked only `if audit_resolve_timeout > 0`. The handler explains
	// why it books that appointment: the re-adjudication pool is N-5, so too few or silent jurors would
	// leave the job `+disputed` forever, a state nothing can move it out of. In k=3 mode nothing coupled
	// the two, so `dispute_window > 0` with `audit_resolve_timeout = 0` was one MsgUpdateParams away:
	// a dispute opened there escrows its bond, marks the job, receives no appointment at any height,
	// and is still open a hundred thousand blocks later (dispute_deadline_unarmed_test.go).
	// The clause sits outside the mode-1 block because its reason does not depend on the mode; its
	// scope is set by the braces, whatever the indentation suggests.
	// `>` rather than `> 0`: a timeout at or below the dispute window resolves before adjudication
	// can run, the reason the mode-1 clause already gave. The defaults are 0/0, so a dormant dispute
	// system trips nothing here.
	if p.DisputeWindow > 0 && p.AuditResolveTimeout <= p.DisputeWindow {
		return fmt.Errorf("dispute_window > 0 requires audit_resolve_timeout > dispute_window (a dispute that can be opened must have a deadline: without it a job with too few or silent jurors stays +disputed for ever; below the window the timeout would resolve before adjudication)")
	}
	// A contributor floor with no source to guard is inert, and therefore misleading.
	// `committee_min_vrf_contributors` is only read on the decentralised seed path
	// (`committeeBaseSeedSourced`). Posted while `committee_seed_source != 1`, it is never evaluated:
	// governance believes it has required a decentralisation floor, and there is none.
	// This holds in all modes.
	if p.CommitteeMinVrfContributors > 0 && p.CommitteeSeedSource != 1 {
		return fmt.Errorf("committee_min_vrf_contributors > 0 requires committee_seed_source=1 (without a decentralised VRF seed this floor is never evaluated, so the guard would be advertised as active while doing nothing)")
	}
	// Anti-evasion bounds. All inactive at the dormant value 0.
	if p.SilenceSlashBps > 10000 {
		return fmt.Errorf("silence_slash_bps > 10000")
	}
	// appeal_window: an appeal (late reveal) only makes sense after a resolve timeout. At 0 (dormant)
	// it constrains nothing.
	if p.AppealWindow > 0 && p.AuditResolveTimeout == 0 {
		return fmt.Errorf("appeal_window > 0 requires audit_resolve_timeout > 0")
	}
	// audit_min_quorum is an integer count of distinct fresh jurors; 0 selects the fallback floor.
	// It deliberately has no upper bound: requiring a quorum above CommitteeSize makes the hard slash
	// unreachable, which is a governance choice, not an invalid parameter set.
	// hold_bps is the fraction (bps) of the optimistic payment held until finality. Dormant at 0.
	if p.HoldBps > 10000 {
		return fmt.Errorf("hold_bps > 10000")
	}
	// Slashable availability. Bounds inactive at the dormant value 0.
	if p.AvailThresholdBps > 10000 {
		return fmt.Errorf("avail_threshold_bps > 10000")
	}
	if p.AvailSlashBps > 10000 {
		return fmt.Errorf("avail_slash_bps > 10000")
	}
	if p.AvailFailK > 0 && p.AvailFailWindow < p.AvailFailK {
		return fmt.Errorf("avail_fail_window must be >= avail_fail_k (otherwise the failure quorum is unreachable)")
	}
	// Activation coherence: an armed availability slash (avail_slash_bps>0) requires the whole
	// machinery, otherwise it is either inert or dangerous through false positives. With everything
	// at 0 (dormant), none of these constraints apply.
	if p.AvailSlashBps > 0 {
		if p.AvailEpochBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_epoch_blocks > 0 (with no challenge epoch the slash is inert)")
		}
		if p.AvailDeadlineBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_deadline_blocks > 0 (with no deadline a challenge never expires, so a failure is never recorded)")
		}
		if p.AvailFailK == 0 || p.AvailFailWindow == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_fail_k > 0 and avail_fail_window > 0 (false-positive protection: an isolated failure must never slash)")
		}
		// The sliding window is stored as a 64-bit bitmask, so W <= 64 when armed. Above that the keeper
		// would silently clamp to 64 and the calibration would be wrong; refusing is better than quietly
		// measuring something else.
		if p.AvailFailWindow > 64 {
			return fmt.Errorf("avail_fail_window > 64 (the sliding window is a 64-bit bitmask; reduce W or lengthen avail_epoch_blocks)")
		}
	}
	return p.validateEpoch14()
}

// AuditFallbackFloor is the jury floor `effectiveSlashFloor` returns when `audit_min_quorum` is 0.
// The keeper derives it (`auditSlashFloor()`, from AuditLegacyFloorBasis); this package cannot import
// the keeper, so the number is named here once and a keeper test asserts that the two agree. Validate
// reads it for the mode-1 quorum floor instead of a bare 4, since a bare number in two packages drifts.
const AuditFallbackFloor = 4

// effectiveAuditFloor mirrors the keeper's `effectiveSlashFloor` for bounds that live here.
func (p Params) effectiveAuditFloor() uint64 {
	if p.AuditMinQuorum > 0 {
		return p.AuditMinQuorum
	}
	return AuditFallbackFloor
}

// validateEpoch14 bounds the five fields epoch 14 added (team_address and four former constants).
//
// Each keeps the module's dormant pattern: 0 or empty selects the compiled behaviour and is always
// valid, so a genesis that omits them validates as before. A posted value is bounded on both sides: a
// lower bound alone lets governance post a value that makes the mechanism vanish, and an upper bound
// alone lets it post one that empties the network.
func (p Params) validateEpoch14() error {
	if p.TeamAddress != "" {
		// Prefix-agnostic on purpose: Validate runs in genesis tooling and in tests before the app sets
		// the Bech32 prefix, so decoding against the global config would refuse a valid `dm1...` address
		// there and accept a foreign-prefix one in production. What matters is that the string names an
		// account: a checksum and a 20- or 32-byte payload.
		if _, bz, err := bech32.DecodeAndConvert(p.TeamAddress); err != nil || (len(bz) != 20 && len(bz) != 32) {
			return fmt.Errorf("team_address %q is not a valid account address (bech32, 20 or 32 bytes): the team share would have nowhere to go", p.TeamAddress)
		}
	}
	// job_expiry_blocks: an unserved job's escrow returns to the client after this many blocks.
	// Too short, and a miner that is merely slow loses a job it was about to serve; it must also
	// outlast the reveal delay, or a job could expire before its committee exists.
	if v := p.JobExpiryBlocks; v > 0 {
		if v < 100 || v > 10_000_000 {
			return fmt.Errorf("job_expiry_blocks %d out of [100, 10000000] (0 selects the compiled default)", v)
		}
		if v <= p.CommitteeRevealDelay {
			return fmt.Errorf("job_expiry_blocks (%d) <= committee_reveal_delay (%d): a job would expire before its committee is revealed", v, p.CommitteeRevealDelay)
		}
	}
	// work_committee_size is the redundancy factor in mode 0, where settlement needs a strict majority
	// of a complete committee: below 3 a single answer settles alone. In mode 1 only the primary is
	// paid and 1 is legitimate. The ceiling bounds an O(miners) draw that runs in the EndBlocker.
	if v := p.WorkCommitteeSize; v > 0 {
		if v > 15 {
			return fmt.Errorf("work_committee_size %d > 15 (0 selects the compiled default)", v)
		}
		if p.VerificationMode == 0 && v < 3 {
			return fmt.Errorf("work_committee_size %d < 3 in redundant mode: a strict majority of fewer than three answers is one answer", v)
		}
	}
	// assignment_stake_cap_multiple: 1 removes stake weighting from the work draw (a decision
	// left open), and far above the observed spread the cap stops capping anything.
	if v := p.AssignmentStakeCapMultiple; v > 0 && v > 100 {
		return fmt.Errorf("assignment_stake_cap_multiple %d > 100 (0 selects the compiled default of 2)", v)
	}
	// audit_committee_draw_size: fewer seats than the floor a hard decision needs makes the slash and
	// the vindication unreachable by construction. A clamp in the keeper would hide that silently;
	// refusing the posted value says it.
	if v := p.AuditCommitteeDrawSize; v > 0 {
		if v < p.effectiveAuditFloor() || v > 64 {
			return fmt.Errorf("audit_committee_draw_size %d out of [%d, 64]: below the effective jury floor no audit can ever conclude (0 selects the compiled default)", v, p.effectiveAuditFloor())
		}
	}
	return nil
}

// WashSubsidyNegativeEV reports whether wash volume is strictly -EV under this parameter set.
//
// `Demand` (team + treasury per job, credited when the client differs from the operator, see
// settle_semantic.go) unlocks a WorkPool subsidy capped at `Demand x WorkGateBps/10000` (see
// x/emission). That counter cannot tell an external buyer from a Sybil self-dealer: two addresses
// are indistinguishable on-chain, so this is the oracle problem and it has no on-chain solution.
// Settlement volume therefore stays a proxy for demand and is never proof of traction.
//
// The real risk is an emission drain, and a drain only exists if washing becomes +EV. Per washed job
// the operator loses what it cannot get back, to unlock only `WorkGateBps x (team+treasury)`.
//
// What it cannot get back changed at epoch 14. The validators' share used to stay in
// the module as a counter; it is now sent to `fee_collector` at finality and x/distribution pays it to
// whoever stakes. A washer that is also a staker gets that share back in proportion to its stake, so
// the cost that holds for every outside washer is what no staker receives: the team share, the
// treasury share and the burn. This bound assumes the worst case for an outsider, a washer holding the
// whole stake, because the stake split is a reading and not a parameter.
//
// It does not hold for the holder of `team_address`. That holder gets the team share back as well;
// with most of the stake too (a single-validator relaunch), its cost falls to treasury + burn, and the
// shipped gate makes washing +EV for it (break-even near work_gate_bps 12 666 under the shipped
// split). That is an insider drain of the work subsidy that no parameter here can price,
// because the insider also controls governance; this guard is not a protection against the operator.
// As long as (the fee factor cancels out)
//
//	WorkGateBps x (team+treasury)  <  (team + treasury + burn)
//
// washing is strictly -EV and there is no drain. This depends on the parameters: a governance increase
// of work_gate_bps, or a shift of the cut split towards team/treasury, could break the -EV property
// silently, so a test pins it (params_invariant_test.go). It is an economic guard; an on-chain
// anti-Sybil mechanism is not achievable.
//
// Everything is in bps of the fee F, which cancels: with V = ValidatorRewardBps, team+treasury =
// ProtocolFeeBps x (10000 - V) / 10000 and burn = FeeBurnBps. Multiplying both sides by 10000^2:
//
//	-EV  <=>  WorkGateBps x ProtocolFeeBps x (10000 - V)  <  (ProtocolFeeBps x (10000 - V) + FeeBurnBps x 10000) x 10000
//
// The epoch-13 form counted the whole cut as lost (`(ProtocolFeeBps + FeeBurnBps) x 10000^2`). It
// allowed work_gate_bps up to 26666 at the shipped split; this one allows 16666. The shipped 15000
// passes both, with a narrower margin under the true flow.
//
// `workGateBps` is the same gate x/emission applies; it is passed as an argument to avoid an import
// cycle between the two modules.
func (p Params) WashSubsidyNegativeEV(workGateBps uint64) bool {
	vr := p.ValidatorRewardBps
	if vr > 10000 {
		vr = 10000 // Validate already bounds validator+team at 10000; defensive clamp against underflow.
	}
	// team+treasury, on the 10000^2 * F scale. math.Int keeps everything overflow-proof even if governance
	// posts an absurd work_gate_bps.
	nonStaker := math.NewIntFromUint64(p.ProtocolFeeBps).Mul(math.NewIntFromUint64(10000 - vr))
	lhs := math.NewIntFromUint64(workGateBps).Mul(nonStaker) // subsidy unlocked, on the 10000^3 * F scale
	if lhs.IsZero() {
		return true // Nothing is unlocked (work_gate=0 or no cut), so no drain is possible: parameters are safe.
	}
	rhs := nonStaker.Add(math.NewIntFromUint64(p.FeeBurnBps).Mul(math.NewIntFromUint64(10000))).
		Mul(math.NewIntFromUint64(10000)) // what no staker gets back, on the same 10000^3 * F scale
	return lhs.LT(rhs)
}
