package types

import (
	"encoding/hex"
	"fmt"

	"cosmossdk.io/math"
)

// NewParams builds a Params instance.
//
// Four positional arguments were dropped along with the proto fields they fed — miner_vest_blocks,
// burn_bps, reporter_bps and demand_client_cap (numbers 6, 8, 9 and 12, now `reserved`). Every one of
// them was displayed by `query jobs params` and read by no production line. Because the arguments are
// POSITIONAL, a call site that was not updated does not compile: the removal cannot be half-applied.
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

// DefaultParams returns the shipped parameter set (ADR-017/018).
//
// The Go DEFAULT is verification_mode=0: k=3 cosine redundancy, WITHOUT the pro-honest bar of the
// optimistic audit, which only guards mode 1. Anyone starting the chain without the docker
// entrypoint — which arms mode 1, fee-hold and silence_slash at genesis — therefore runs in mode 0.
// The deterrence floors in Validate() guard BOTH paths; the audit bar applies only to ARMED genesis
// files. This is documented rather than flipped, because flipping the default is a consensus change.
//
// Deliberately not written here: an absolute committee threshold. Such a figure was once framed
// around a 5-seat committee, and ADR-032 draws 15; the bar is RELATIVE (⌈2/3 of anchored seats⌉),
// and an absolute number copied into this comment would go stale in silence again — that is exactly
// how a bar written for five seats ends up applied to fifteen without anyone seeing it.
//
// Flipping this default requires adapting the keeper tests that depend on it (hold_bps=0 implies
// the strict non-hold settlement path).
func DefaultParams() Params {
	p := NewParams(
		1000,  // min_stake
		1500,  // protocol_fee_bps (15 %)
		5000,  // validator_reward_bps (50 % of the cut)
		2000,  // team_fee_bps (20 % of the cut)
		3000,  // miner_vest_bps (30 %) -- HELD BACK at legacy settlement; there is no release schedule
		8000,  // slash_leak_bps (80 %)
		100,   // epoch_blocks
		15000, // work_gate_bps (1.5x)
		500,   // fee_burn_bps (5 % soft burn) -- REAL, via BurnCoins at settlement
	)
	p.AvailRequireDemand = true // Availability anti-farming is ON by default on an incentivised testnet.
	// ADR-028 — governable parameters, ALL DORMANT at 0: the default behaviour is strictly unchanged.
	// Assigning 0 explicitly documents the dormancy; the Go zero value is already 0, so the redundancy
	// is safe and the intent becomes readable.
	p.SilenceSlashBps = 0 // 0 = light clawback only
	p.AppealWindow = 0    // 0 = restitution through governance; reserved, not consumed yet
	// audit_min_quorum: 0 falls back to the participation floor (⌈N/2⌉+1) plus the stake majority.
	// Above 0 it acts as a LOWER BOUND on the quorum, not as the quorum itself: since ADR-032 the
	// EFFECTIVE threshold is max(this parameter, ⌈2/3 x ANCHORED seats⌉) AND the stake majority. With
	// a 15-seat draw, an absolute threshold of 4 is 27 %, not the near-unanimity such a number
	// suggests — which is why the relative term exists.
	p.AuditMinQuorum = 0
	p.HoldBps = 0                     // 0 = full optimistic payment immediately; >0 = windowed hold until finality
	p.CommitteeMinVrfContributors = 0 // 0 = no floor (dormant); >0 = a decentralised seed built from fewer than N contributors raises an alert and falls back, never halts the chain
	// ADR-022 — slashable availability (VRF challenge). ALL DORMANT at 0: inert until AvailEpochBlocks
	// is set AND these parameters are posted.
	p.AvailThresholdBps = 0   // 0 = no semantic check on the challenge; >0 = cosine threshold
	p.AvailDeadlineBlocks = 0 // 0 = no challenge is due; >0 = blocks granted to answer
	p.AvailSlashBps = 0       // 0 = no availability slash; >0 = fraction of the stake slashed
	p.AvailSlashMax = 0       // 0 = no cap; >0 = absolute cap (udndr) on the availability slash
	p.AvailFailWindow = 0     // 0 = quorum never reached; >0 = sliding window over which failures are counted
	p.AvailFailK = 0          // 0 = never slash (full false-positive protection); >0 = failures required within the window
	// ADR-044, gesture 2. WRITTEN EXPLICITLY THOUGH IT IS THE ZERO VALUE, because "dormant" is a
	// decision here, not an omission: shipping the unwind armed would pick, on the operator's behalf,
	// how long a debt may stay unresolvable on their network -- a number the code cannot derive.
	// 0 keeps every deferral byte-identical to the behaviour that preceded this parameter.
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
	// ⛔ A SLASH ABOVE 100 % OF A BOND, AND A BURN ABOVE 100 % OF A FEE, BOTH UNDERFLOW A uint64.
	// Nine bps parameters were bounded from above here; these two were not — `slash_leak_bps` carried a
	// FLOOR only (2500, in optimistic mode) and `fee_burn_bps` had no bound at all. MEASURED, not
	// reasoned: `Validate` returned nil at 20000 for both, a permissionless `VerifySemantic` then
	// succeeded, and the punished miner's bond went from 1000 to 18446744073709550616 while the
	// Treasury was credited 2000 coins that do not exist. The slashed cheater becomes by far the
	// largest bond on the chain — and the audit jury draw reads the RAW stake (no assignment cap
	// applies there), so it would take every seat it is offered.
	//
	// A single mistyped percent-for-bps posts this: 80 read as 80 % is 8000, but 80000 is what a
	// percent-shaped habit writes. Four of the seven slash sites are permissionless, so once the
	// parameter is in, anyone triggers it for the price of gas.
	//
	// ⚠️ WHY ONLY THESE TWO, when four bps fields carry no direct upper test. `validator_reward_bps` IS
	// bounded, by the SUM check above — a regex looking for `> 10000` on the field alone misses it.
	// And `work_gate_bps` is DELIBERATELY above 10000: the live chain serves 15000, because it scales a
	// subsidy against demand rather than splitting a total. Bounding it "for consistency" would have
	// refused the running configuration. The rule is not "every bps is a share"; it is "a share of X
	// cannot exceed X".
	if p.SlashLeakBps > 10000 {
		return fmt.Errorf("slash_leak_bps > 10000: a slash cannot exceed the bond it takes from (uint64 underflow: the punished miner would end up with the largest stake on the chain)")
	}
	if p.FeeBurnBps > 10000 {
		return fmt.Errorf("fee_burn_bps > 10000: a burn cannot exceed the fee it is taken from (uint64 underflow on the distributable remainder)")
	}
	// ADR-034 — the ORDER of the two vitality windows is an invariant, not a hope.
	// `juror_freshness_blocks` (stop SUMMONING) must come before `miner_prune_blocks` (EVICT from the
	// registry). The reverse order evicts a miner the chain still considers summonable as a juror:
	// inconsistent in itself, and it drops the second guard of `hasOpenObligation` into a state
	// nobody designed for. Both are governable since ADR-034 §5, and Validate used to inspect their
	// values in isolation. A parameter whose RELATIVE ORDER carries meaning must be bounded, not
	// hoped for.
	// The dormant pattern is preserved: 0 means "compiled-in constant", so governing only one of the
	// two triggers nothing — only explicitly posted values are compared.
	if p.MinerPruneBlocks > 0 && p.JurorFreshnessBlocks > 0 && p.MinerPruneBlocks < p.JurorFreshnessBlocks {
		return fmt.Errorf("miner_prune_blocks (%d) < juror_freshness_blocks (%d): this evicts from the registry a miner still summonable as a juror",
			p.MinerPruneBlocks, p.JurorFreshnessBlocks)
	}
	// ADR-037 §4 — a SECOND ordering relation, distinct from the one above. The pool freeze bounds
	// eligibility by the JOB'S AGE (a miner registered after the job opened will never judge it);
	// eviction bounds it by INACTIVITY. If `miner_prune_blocks < dispute_window`, the registry evicts
	// precisely the jurors the freeze still allows to judge, and a job old enough becomes unjudgeable.
	// The degradation is a deferral, never a sanction, but it is real and silent.
	// This is NOT the relation above (`prune >= juror_freshness`): neither implies the other, and
	// enforcing one leaves the other hole open. Same dormant pattern: only posted values are compared.
	// ADR-044, gesture 2 — a THIRD ordering relation, on the same dormant pattern: a pair of zeros
	// compares nothing. An unwind age BELOW the resolve timeout fires before the audit has had its own
	// scheduled retry, so the retention would be undone while the verification it waits for is still
	// pending -- the parameter would REPLACE the deferral instead of bounding it, which is the opposite
	// of what the record decides. The bar is DERIVED from the timeout already in Params, never retyped:
	// `auditDeferStride` lives in the keeper and copying its value here is how two numbers stop agreeing.
	if p.AuditUnwindBlocks > 0 && p.AuditResolveTimeout > 0 && p.AuditUnwindBlocks < p.AuditResolveTimeout {
		return fmt.Errorf("audit_unwind_blocks (%d) < audit_resolve_timeout (%d): the retention would be unwound before the audit it waits for has been retried even once",
			p.AuditUnwindBlocks, p.AuditResolveTimeout)
	}
	if p.MinerPruneBlocks > 0 && p.DisputeWindow > 0 && p.MinerPruneBlocks < p.DisputeWindow {
		return fmt.Errorf("miner_prune_blocks (%d) < dispute_window (%d): this evicts from the registry the only jurors the pool freeze (ADR-037) still allows to judge this job",
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
	// ADR-025: optimistic verification, dormant by default.
	if p.VerificationMode > 1 {
		return fmt.Errorf("verification_mode must be 0 (redundant) or 1 (optimistic)")
	}
	if p.AuditSampleBps > 10000 {
		return fmt.Errorf("audit_sample_bps > 10000")
	}
	// ADR-028 (anti-evasion): in optimistic mode the audit MUST be able to end in a slash. Without
	// these cross bounds the optimistic slash is INERT even when enabled: with no sample no job is
	// ever audited, and if the resolve timeout is not STRICTLY after the dispute window, auto
	// resolution (clawback or vindication) fires before adjudication is admissible.
	if p.VerificationMode == 1 {
		if p.AuditSampleBps == 0 {
			return fmt.Errorf("verification_mode=1 requires audit_sample_bps > 0 (otherwise no audit is ever drawn)")
		}
		if p.DisputeWindow == 0 {
			return fmt.Errorf("verification_mode=1 requires dispute_window > 0")
		}
		// Deterrence floor, following the ADR-022 pattern "armed implies full machinery". Without it,
		// governance can post slash_leak_bps=0 on an ARMED optimistic chain: the cheater who is caught
		// loses NOTHING, so cheating and self-dealing become +EV, and neither Validate nor the wash
		// invariant catches it — that invariant guards the SUBSIDY wash, not the slash. The floor of
		// 2500 (25 % of the stake) is structural, meaning "never symbolic"; the fine economics of an
		// exact -EV at around 10 % audit are calibrated elsewhere (shipped value 8000). Redundant mode
		// is untouched.
		if p.SlashLeakBps < 2500 {
			return fmt.Errorf("verification_mode=1 requires slash_leak_bps >= 2500 (deterrence must never be symbolic; shipped value 8000)")
		}
		if p.AuditSampleBps < 500 {
			return fmt.Errorf("verification_mode=1 requires audit_sample_bps >= 500 (below a 5%% audit rate, cheating is +EV again even at full slash)")
		}
		// Lower bound on `audit_min_quorum`, a parameter that previously had none.
		// It does not only drive the slash — that side is protected by the ⌈2/3⌉ of anchored seats
		// (ADR-032). Through `effectiveSlashFloor` it also drives the VINDICATION floor. At 1, a single
		// juror was enough to release a cheater's held payment and deny the client its refund, so
		// governance could disarm half the guarantee without naming it. The floor matches
		// `auditSlashFloor()` on the keeper side (⌈AuditLegacyFloorBasis/2⌉+1 = 4); shipped value 4.
		// 0 remains allowed: it is the explicit fallback, not a silent weakening.
		if p.AuditMinQuorum > 0 && p.AuditMinQuorum < 4 {
			return fmt.Errorf("verification_mode=1 requires audit_min_quorum >= 4 when it is governed (below this floor an isolated juror vindicates a cheater; 0 selects the fallback floor)")
		}
		if p.DisputeWindow > 0 && p.DisputeBond == 0 {
			return fmt.Errorf("verification_mode=1 requires dispute_bond > 0 (disputes are ACTIVE: without a bond, disputing is free and the anti-grief forfeiture forfeits nothing; suggested anchor: min_stake)")
		}
		// The committee seed must not be predictable by the party the committee judges.
		//
		// A DEFERRED REVEAL IS REQUIRED, AND A DECENTRALISED SEED DOES NOT REPLACE IT.
		//
		// The two knobs do not answer the same attacker, which is why only one of them is mandatory:
		//   - `committee_seed_source = 1` makes the seed an aggregate no single actor controls. That
		//     defends against the JOB CREATOR, which is a party to the transaction and nothing more.
		//   - `committee_reveal_delay > 0` keeps the seed empty at open time and fixes it later on the
		//     AppHash of a FUTURE block. That is the only one that defends against the PROPOSER.
		//
		// With `delay = 0` the seed is fixed INSIDE the opening block, and the proposer composes that
		// block: it reads the pending MsgOpenJob, it assembles the vote extensions the seed is built
		// from, and it chooses the block time the legacy seed is built from. It therefore knows the
		// committee before it commits to proposing, and can re-order or withhold until the draw suits
		// it. A decentralised source does not remove that: being unable to pick the value alone is not
		// the same as being unable to SEE it before acting.
		//
		// In redundant mode (k=3) peer divergence limits the gain; in optimistic mode the audit
		// committee IS the only verification, so choosing it amounts to choosing the verdict.
		if p.CommitteeRevealDelay == 0 {
			return fmt.Errorf("verification_mode=1 requires committee_reveal_delay>0 (deferred reveal): with a zero delay the committee seed is fixed inside the opening block, which the PROPOSER composes -- it reads the pending job, builds the seed and knows the committee before proposing. committee_seed_source=1 does not substitute: it stops a single actor from PICKING the seed, not the proposer from SEEING it in time to act")
		}
	}

	// ⚠️ AND THIS CLAUSE FIRST LANDED INSIDE THE MODE-1 BLOCK, WITH NOTHING TO SAY SO. It carried the
	// indentation of the outer level — so it read as general — while the BRACES kept it within. Go does
	// not read indentation. Build green, vet green, eight test packages green: NIL effect, and a
	// perfectly reassuring diff. What revealed it was a bench asserting its own premise —
	// `require.NoError(p.Validate())` on the set that was now supposed to be REFUSED — by NOT going
	// red. Scope is measured by counting braces from the opening, never by reading the margin.
	// Disputing must never be free. Opening a dispute escrows `dispute_bond`, and an unfounded
	// dispute forfeits it. At 0 the forfeiture forfeits nothing: the anti-grief mechanism exists,
	// it is inert, and disputing every settled job costs only gas. `dispute_window > 0` is enough
	// to open disputes, and optimistic mode REQUIRES `dispute_window > 0` (bound above), so the
	// combination "mode 1 with a zero bond" is reachable and it disarms an advertised guard.
	// A DISPUTE THAT CAN BE OPENED MUST HAVE A DEADLINE — IN EVERY MODE.
	// This clause lived inside the `verification_mode == 1` branch, and its reason never depended on
	// the mode: `DisputeVerdict` gates only on `dispute_window`, and the appointment that guarantees an
	// open dispute cannot hang for ever is booked only `if audit_resolve_timeout > 0`. The handler
	// explains why it pays for that appointment: the re-adjudication pool is N-5, so too few or silent
	// jurors would leave the job `+disputed` FOREVER — a state nobody can force back into existence.
	// In k=3 mode nothing coupled the two, so `dispute_window > 0` beside `audit_resolve_timeout = 0`
	// was a legal set one MsgUpdateParams away: measured, a dispute opened there escrows its bond,
	// marks the job, receives no appointment at any height, and is still open a hundred thousand blocks
	// later (dispute_deadline_unarmed_test.go).
	// `>` and not `> 0`: a timeout at or below the dispute window resolves before adjudication can run,
	// which is the reason the mode-1 clause already gave. The DEFAULTS are 0/0, so a dormant dispute
	// system trips nothing here.
	if p.DisputeWindow > 0 && p.AuditResolveTimeout <= p.DisputeWindow {
		return fmt.Errorf("dispute_window > 0 requires audit_resolve_timeout > dispute_window (a dispute that can be opened must have a deadline: without it a job with too few or silent jurors stays +disputed for ever; below the window the timeout would resolve before adjudication)")
	}
	// A contributor floor with no source to guard is inert, and therefore misleading.
	// `committee_min_vrf_contributors` is only read on the DECENTRALISED seed path
	// (`committeeBaseSeedSourced`). Posted while `committee_seed_source != 1`, it is never evaluated:
	// governance believes it has required a decentralisation floor, and there is none.
	// This holds in ALL modes; the inconsistency does not depend on the verification mode.
	if p.CommitteeMinVrfContributors > 0 && p.CommitteeSeedSource != 1 {
		return fmt.Errorf("committee_min_vrf_contributors > 0 requires committee_seed_source=1 (without a decentralised VRF seed this floor is never evaluated: an INERT guard advertised as active)")
	}
	// ADR-028 bounds. All inactive at the dormant value 0.
	if p.SilenceSlashBps > 10000 {
		return fmt.Errorf("silence_slash_bps > 10000")
	}
	// appeal_window: an appeal (late reveal) only makes sense after a resolve timeout. At 0 (dormant)
	// it constrains nothing.
	if p.AppealWindow > 0 && p.AuditResolveTimeout == 0 {
		return fmt.Errorf("appeal_window > 0 requires audit_resolve_timeout > 0")
	}
	// audit_min_quorum is an INTEGER count of distinct fresh jurors; 0 selects the fallback floor.
	// There is deliberately no upper bound: requiring a quorum above CommitteeSize makes the hard
	// slash unreachable, which is a governance choice, not an invalid parameter set.
	// hold_bps is the fraction (bps) of the optimistic payment held until finality. Dormant at 0.
	if p.HoldBps > 10000 {
		return fmt.Errorf("hold_bps > 10000")
	}
	// ADR-022 — slashable availability. Bounds inactive at the dormant value 0.
	if p.AvailThresholdBps > 10000 {
		return fmt.Errorf("avail_threshold_bps > 10000")
	}
	if p.AvailSlashBps > 10000 {
		return fmt.Errorf("avail_slash_bps > 10000")
	}
	if p.AvailFailK > 0 && p.AvailFailWindow < p.AvailFailK {
		return fmt.Errorf("avail_fail_window must be >= avail_fail_k (otherwise the failure quorum is unreachable)")
	}
	// Activation coherence: an ARMED availability slash (avail_slash_bps>0) requires the whole
	// machinery, otherwise it is either inert or dangerous through false positives. With everything
	// at 0 (dormant), none of these constraints bite.
	if p.AvailSlashBps > 0 {
		if p.AvailEpochBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_epoch_blocks > 0 (with no challenge epoch the slash is inert)")
		}
		if p.AvailDeadlineBlocks == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_deadline_blocks > 0 (with no deadline a challenge never expires, so a failure is never recorded)")
		}
		if p.AvailFailK == 0 || p.AvailFailWindow == 0 {
			return fmt.Errorf("avail_slash_bps > 0 requires avail_fail_k > 0 AND avail_fail_window > 0 (false-positive protection: an isolated failure must never slash)")
		}
		// The sliding window is stored as a 64-bit BITMASK, so W <= 64 at arming time. Above that the
		// keeper would silently clamp to 64 and the calibration would be wrong; refusing is better than
		// quietly measuring something else.
		if p.AvailFailWindow > 64 {
			return fmt.Errorf("avail_fail_window > 64 (the sliding window is a 64-bit bitmask; reduce W or lengthen avail_epoch_blocks)")
		}
	}
	return nil
}

// WashSubsidyNegativeEV reports whether wash volume is strictly -EV under this parameter set.
//
// `Demand` (team + treasury per job, credited when the client differs from the operator, see
// settle_semantic.go) unlocks a WorkPool subsidy capped at `Demand x WorkGateBps/10000` (see
// x/emission). That counter cannot distinguish an EXTERNAL buyer from a Sybil self-dealer: two
// addresses are indistinguishable on-chain, so this is the oracle problem and it has no on-chain
// solution. Settlement volume therefore stays a PROXY for demand and is never proof of traction.
//
// The real risk is consequently not a flattering dashboard but an emission DRAIN, and a drain only
// exists if washing becomes +EV. Per washed job the operator LOSES `cut + burn` to unlock only
// `WorkGateBps x (team+treasury)`. As long as (the fee factor cancels out)
//
//	WorkGateBps x (team+treasury)  <  (cut + burn)
//
// washing is strictly -EV and there is no drain. This is PARAMETER-DEPENDENT — a governance increase
// of work_gate_bps, or a shift of the cut split towards team/treasury, could break the -EV property
// SILENTLY — so it is pinned by a test (params_invariant_test.go). It is an economic guard, not an
// on-chain anti-Sybil mechanism, which is not achievable.
//
// Everything in bps of the fee F, which cancels: cut = ProtocolFeeBps, team+treasury = cut minus
// validators, burn = FeeBurnBps:
//
//	-EV  <=>  WorkGateBps x ProtocolFeeBps x (10000 - ValidatorRewardBps)  <  (ProtocolFeeBps + FeeBurnBps) x 10000^2
//
// `workGateBps` is the SAME gate x/emission applies; it is passed in as an argument to avoid an
// import cycle between the two modules.
func (p Params) WashSubsidyNegativeEV(workGateBps uint64) bool {
	vr := p.ValidatorRewardBps
	if vr > 10000 {
		vr = 10000 // Validate already bounds validator+team at 10000; defensive clamp against underflow.
	}
	// math.Int keeps this overflow-proof even if governance posts an absurd work_gate_bps.
	lhs := math.NewIntFromUint64(workGateBps).
		Mul(math.NewIntFromUint64(p.ProtocolFeeBps)).
		Mul(math.NewIntFromUint64(10000 - vr)) // subsidy unlocked, on the 10000^3 * F scale
	if lhs.IsZero() {
		return true // Nothing is unlocked (work_gate=0 or no cut), so no drain is possible: parameters are safe.
	}
	rhs := math.NewIntFromUint64(p.ProtocolFeeBps + p.FeeBurnBps).
		Mul(math.NewIntFromUint64(100000000)) // cost of cut+burn, on the same 10000^2 * F scale
	return lhs.LT(rhs)
}
