package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// ADR-028 — ANTI-EVASION OF THE OPTIMISTIC SLASH.
//
// Problem: under optimistic verification with k=1 the primary is PAID first. The J1 reveal
// (re-sealing prompt+answer towards the fresh committee) is COOPERATIVE: only the primary can produce
// it. A primary that CHEATS and then STAYS SILENT was neither judged nor slashed — the liveness
// timeout vindicated it by default. Silence was therefore the cheater's dominant strategy, and the
// Nash inequality no longer held.
//
// Fix: the optimistic primary is the SUSPECT; it NEVER keeps an unverified payment. Two levers and a
// floor:
//  1. OFF-CHAIN (non-forgeable signal): `judge_worker.py` posts a "0" verdict when it cannot obtain a
//     USABLE reveal after the grace period. A primary that stays silent, or reveals nothing usable,
//     is converted into a CHEAT QUORUM by the committee itself, never by a forgeable on-chain marker.
//  2. ON-CHAIN (runAuditResolveTimeout): at the deadline, ABOVE the participation floor: cheat quorum
//     -> hard slash + clawback; valid quorum -> vindicated; below the floor or no majority -> LIGHT,
//     refundable clawback (neither hard slash nor vindication).
//  3. SYMMETRIC PARTICIPATION FLOOR (auditSlashFloor): the tally is computed on the stake of those
//     PRESENT. Without a floor, two judges or sybils forming a "majority of those present" would
//     slash an honest miner (grief) OR vindicate a cheater (the dual of grief) whenever the honest
//     judges vote late. Hard slash AND vindication therefore require enough distinct judges to have
//     voted (⌈AuditLegacyFloorBasis/2⌉+1 = 4, DECOUPLED from CommitteeSize=3).

// AuditLegacyFloorBasis — ⛔ THIS CONSTANT DOES NOT DESCRIBE THE SIZE OF A COMMITTEE. ADR-032 draws
// 15 seats (`auditCommitteeDrawSize`, audit_committee.go). A name that implicitly claims to describe
// current state is what makes staleness invisible: nobody re-reads a threshold whose name asserts it
// is up to date. The absolute floor of 4 this basis produces is 4/15 = 27 % on a committee three
// times larger than the one it was calibrated for, while the name still reads "4 out of 5".
//
// A drifting reference is not always a line number: here it is a NAME — and a name gets copied into
// every new call site by whoever read it in good faith. The constant is therefore named for what it
// IS: the HISTORICAL BASIS of the v1 floor, and nothing else. A reader looking for the committee size
// will not find it here, which is exactly the intent.
//
// What it remains: the sole input of `auditSlashFloor()`, the ABSOLUTE participation floor of the v1
// fallback. The SEAT bar is relative to the ANCHORED seats and lives in `auditRelativeBar`. Decoupled
// from `CommitteeSize`=3 (committee.go), which stays the size of the REDUNDANT/mode-0 committee:
// raising that one would break `settle_semantic`, which requires `len(vecs) >= CommitteeSize`.
const AuditLegacyFloorBasis = 5

// auditSlashFloor (ADR-028, participation floor) — MINIMUM number of distinct fresh judges PRESENT
// for a HARD DECISION (slash OR vindication) to fire: ⌈AuditLegacyFloorBasis/2⌉+1 = 4. The tally is
// computed on the stake of those PRESENT; without this floor, two judges or sybils forming a
// "majority of those present" could slash an honest miner (grief) OR vindicate a cheater (the dual of
// grief) whenever the honest judges vote late. Below it -> light refundable clawback, NEVER a hard
// slash and never a vindication. ⚠️ This is an ABSOLUTE floor: it says NOTHING about the number of
// seats drawn. The seat-dependent bar is `auditRelativeBar`; conflating the two is the defect this
// file has carried. v2 = governed fraction via effectiveSlashFloor/AuditMinQuorum.
func auditSlashFloor() int { return (AuditLegacyFloorBasis+1)/2 + 1 }

// effectiveSlashFloor (ADR-028 v2) — EFFECTIVE participation floor: `AuditMinQuorum` (governed, an
// INTEGER count of distinct fresh judges) when it is > 0, otherwise the `auditSlashFloor()` fallback
// (= 4). DORMANT: while `audit_min_quorum == 0` (the default) it returns EXACTLY `auditSlashFloor()`,
// so v1 behaviour is strictly unchanged. Every "hard slash" decision site (timeout and
// AdjudicateDispute) goes through here.
func effectiveSlashFloor(p types.Params) int {
	if p.AuditMinQuorum > 0 {
		return int(p.AuditMinQuorum)
	}
	return auditSlashFloor()
}

// auditVerdictTally — STAKE-weighted tally of the fresh committee's verdicts
// (`<jobId>__verdict__<minerId>`, "0" = invalid). Excludes the primary (under k=1 it is the sole
// "origin"), and accepts ONLY members of the committee ANCHORED at draw time (ADR-032).
//
// ⚠️ DO NOT RELAX THIS FILTER BACK. It once accepted any REGISTERED miner, on the assumption that
// registering "costs something". That assumption is QUALITATIVE and quantitatively false: at
// `min_stake`, four identities cost 0.2 DNDR and were enough to strip any honest miner of 80 % of its
// stake — and a test asserted that behaviour, green on every run. Membership in the drawn committee is
// now the only authorisation to vote.
//
// Returns (cheated, valid, voters): `voters` = number of summoned judges that voted; cheated = STRICT
// majority of "invalid" stake; valid = STRICT majority of "valid" stake (a tie yields neither).
// `invalidVotes` = COUNT of "invalid" judges (not their stake) -> consumed by the VETO
// (auditSlashDecision), whose threshold is RELATIVE to the anchored committee (⌈2/3⌉ of the seats)
// since ADR-032 as amended, no longer "4 out of 5". The CALLER applies effectiveSlashFloor() TO THE
// VINDICATION and auditSlashDecision() TO THE SLASH (veto count when governed, otherwise stake
// majority at the v1 floor). Below either -> neither slash nor vindication, light refundable clawback.
// (totalStake ≤ the 1e13 udndr supply, so *2 cannot overflow uint64.)
//
// `distinctOps` (ADR-034 partition) = number of DISTINCT OPERATORS behind the counted votes: a quorum
// reached by 15 seats held by a single operator is a quorum in form only. `maxOpVotes` = the largest
// number of votes held by ONE operator: plurality is not counted in heads ("distinct >= 2" is
// satisfied when one operator holds 9 votes out of 10), and the C3 gate reads
// `maxOpVotes/voters < 50 %`. Both are counted HERE, inside the SAME filter as the tally (anchored,
// non-primary, registered): a second loop with its own filter would eventually diverge from it. An
// empty operator falls into a single bucket, so distinct is UNDER-counted and maxOpVotes
// OVER-counted — both errors HARDEN the gate, never the opposite.
func (k Keeper) auditVerdictTally(ctx context.Context, jobId, primId string) (cheated, valid bool, voters, invalidVotes, seats, distinctOps, maxOpVotes int) {
	// ADR-032 — ONLY THE SUMMONED VOTE. Without this barrier, "registered miner" was enough to weigh on
	// a slash: four identities at min_stake cost an honest miner 80 % of its stake.
	// FAIL-CLOSED: no anchored committee (older jobs, paths outside mode 1) -> EMPTY tally -> no hard
	// slash is possible and we fall back to the light clawback. The asymmetry is deliberate: missing a
	// cheater is bounded and -EV (ADR-025), slashing an honest miner cannot be undone.
	allowed, anchored := k.auditCommitteeAllowed(ctx, jobId)
	if !anchored {
		return false, false, 0, 0, 0, 0, 0
	}
	seats = len(allowed) // size of the ANCHORED committee: the quorum denominator (ADR-032 as amended)
	prefix := jobId + "__verdict__"
	var invalidStake, totalStake uint64
	ops := map[string]int{}
	_ = k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if !strings.HasPrefix(key, prefix) {
			return false, nil
		}
		mid := strings.TrimPrefix(key, prefix)
		if mid == primId {
			return false, nil // exclude the primary (the sole "origin" under k=1)
		}
		if !allowed[mid] {
			return false, nil // NOT summoned -> its verdict does not count (ADR-032)
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // a registered miner is required
		}
		voters++
		totalStake += m.Stake
		ops[m.Operator]++ // REAL plurality: the operator, not the seat (a count, not a set)
		if strings.TrimSpace(c.ResultCommit) == "0" {
			invalidStake += m.Stake
			invalidVotes++ // VETO: COUNT the "invalid" verdicts (bar relative to anchored seats), not just their stake
		}
		return false, nil
	})
	distinctOps = len(ops)
	for _, n := range ops {
		if n > maxOpVotes {
			maxOpVotes = n
		}
	}
	if voters == 0 || totalStake == 0 {
		return false, false, 0, 0, seats, distinctOps, maxOpVotes
	}
	cheated = invalidStake*2 > totalStake
	valid = invalidStake*2 < totalStake
	return cheated, valid, voters, invalidVotes, seats, distinctOps, maxOpVotes
}

// auditRelativeBar — the SEAT bar, SINGLE SOURCE for both the slash and the vindication.
//
// ⛔ WHY THE BAR MUST BE RELATIVE, AND WHY THE DEFAULT PATH IS THE DANGEROUS ONE. The relative bar
// ⌈2/3 × anchored seats⌉ once existed ONLY inside the `audit_min_quorum > 0` branch. Under the
// DEFAULT setting (`audit_min_quorum = 0`, params.go — and `Params.Validate()` ACCEPTS it together
// with `verification_mode = 1`, since it only rejects a governed quorum below 4), the decision fell
// back to v1: `voters >= auditSlashFloor()` = 4, an ABSOLUTE COUNT.
//
// That 4 was correct for a 5-seat committee (4/5 = near-unanimity, which was the whole point: an
// honest miner judged invalid by a MINORITY is never slashed). ADR-032 raised the draw to 15 seats
// for liveness. 4 out of 15 is 27 %: on the default path, four jurors hard-slashed an honest miner
// even against eleven "valid" votes. The threshold was right; its DENOMINATOR had changed underneath
// it. This is the same absolute-vs-relative class of defect that has appeared repeatedly
// (ADR-032 -> ADR-033 -> here), and the only occurrence that survived in the DEFAULT configuration.
//
// It is also what the public README asserts: "A hard slash requires at least two thirds of the
// anchored seats to return invalid." That sentence was only true if the launcher remembered to arm
// `audit_min_quorum` — a security property cannot be delegated to an optional setting.
//
// The remedy is structural, not local: ONE function, two callers. A fifth call site cannot diverge,
// because there is no second computation left to forget to update.
// At `seats == 0` it yields the floor alone — and the tally is already fail-closed upstream.
func auditRelativeBar(p types.Params, seats int) int {
	need := auditSlashFloor() // ABSOLUTE v1 floor (⌈N/2⌉+1)
	if p.AuditMinQuorum > 0 {
		need = int(p.AuditMinQuorum) // GOVERNED floor, lower bound
	}
	if q := (2*seats + 2) / 3; q > need { // ⌈2/3 × ANCHORED seats⌉ in integer arithmetic
		need = q
	}
	return need
}

// auditSlashDecision (ADR-028 v2, pro-honest VETO; ADR-032 as amended) — decides the HARD SLASH.
//
// `seats` = size of the anchored committee. At 0 (jobs without an anchor) only the governed floor
// applies — but the tally is already fail-closed upstream, so that path slashes nothing. Pure Go,
// testable outside the keeper.
func auditSlashDecision(p types.Params, cheated bool, voters, invalidVotes, seats int) bool {
	// THREE independent locks, each priced in capital: participation (effective floor), SEATS (⌈2/3⌉
	// of the anchored ones) and a STAKE majority. Requiring all three can only REDUCE the number of
	// slashes — pro-honest HERE, and only here: see `auditVindicateDecision`, which the same hardening
	// makes HARDER to obtain, which is no gift to an honest miner.
	return voters >= effectiveSlashFloor(p) && invalidVotes >= auditRelativeBar(p, seats) && cheated
}

// auditVindicateDecision — decides the VINDICATION, at the SAME relative bar as the slash.
//
// The asymmetry this closes: the slash required ⌈2/3⌉ of the anchored seats (ADR-032 as amended)
// while the vindication only needed the governed floor, so ~27 % of the stake (about 4 seats out of
// 15) BOUGHT the vindication of any cheat where 67 % was needed to slash it. What differs between the
// two outcomes is now the DIRECTION of the majority (validVotes vs invalidVotes), never the bar for
// being heard. `validVotes` = number of summoned jurors that voted "valid" (= voters − invalidVotes).
// The symmetry is STRUCTURAL: both outcomes read `auditRelativeBar`.
//
// ⛔ THE REASON IS SYMMETRY, NOT "PRO-HONEST" — the justification given for the slash does NOT cover
// this function, and saying so here keeps it from being copied across. Hardening the SLASH protects
// the honest miner; hardening the VINDICATION costs it. Vindication is what RELEASES an honest
// primary's held fee: at the default, the requirement goes from "4 voters" to "⌈2/3 of the anchored
// seats⌉ valid votes" — 10 out of 15 instead of 4. The price is therefore LONGER RETENTION for honest
// miners, in a network that already struggles to close its audits, and the deferral of ADR-034 is
// not bounded.
//
// The price is paid anyway, because the alternative is worse: a vindication cheaper than a slash lets
// a small colluding subset buy a cheater's innocence for ~27 % of the stake when 67 % is needed to
// convict it. What differs between the two outcomes must be the DIRECTION of the majority, never the
// bar for being heard.
//
// ⚠️ MEASURED SCOPE, and it is favourable in the short term: at launch scale (~7 miners, so ~5
// anchored seats), ⌈2/3 × 5⌉ = 4 = the floor, and the hardening changes NOTHING. The cost only
// appears as the pool grows, i.e. on success. It is DEFERRED, not absent: if retention becomes an
// observed problem, the answer is to bound the deferral (ADR-034), not to lower this bar.
func auditVindicateDecision(p types.Params, valid bool, voters, validVotes, seats int) bool {
	return voters >= effectiveSlashFloor(p) && validVotes >= auditRelativeBar(p, seats) && valid
}

// slashCheatedPrimary — PROVEN CHEAT (quorum of "invalid" verdicts ABOVE the bar): HARD slash of
// `SlashLeakBps` of the primary's stake (a counter; the coins are already held by the module through
// the bond), REFUNDS the price paid to the client (min(fee, slashed)), sends the REMAINDER to the
// Treasury, and RECORDS a REFUNDABLE SlashRecord. Bounded; the transfer is best-effort (an address
// failure must not block EndBlock). Mutates `job`, which the caller persists.
func (k Keeper) slashCheatedPrimary(ctx context.Context, job *types.Job, params types.Params) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		// The primary may be ABSENT: a job whose miner was evicted, or state carried in by a migration
		// or an import. The `hasOpenObligation` exit guard normally makes this unreachable, but it is
		// handled anyway. A bare `return nil` froze the retention FOREVER: the job left in
		// `+resolved+clawed+quorum` with its HeldFee intact and no remaining path to settle it — the
		// client never refunded, and held-summary watching a dead debt age. There is nothing to slash,
		// but the CLIENT is still refunded out of the retention (nil = no Demand to reverse) and the
		// clawback is TRACED (slashed=0, explicit reason).
		refunded := k.refundRetainedToClient(ctx, job, params, nil)
		k.emitClawback(ctx, job.JobId, job.MinerId, 0, refunded, "cheated_primary_vanished")
		return nil
	}
	// The client is refunded first out of the WHOLE retained fee (held minerNet + the cut reversed from
	// Pools + the deferred burn given back), and this job's Demand is reversed. The hard slash of the
	// bond stays PUNITIVE (-> Treasury); it only refunds the client for whatever was paid immediately
	// (partial hold; nothing at full hold).
	refundedRetained := k.refundRetainedToClient(ctx, job, params, &miner)
	amt := miner.Stake * params.SlashLeakBps / 10000
	if amt > 0 {
		miner.Stake -= amt
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil { // persists the reversed Demand (and any slash)
		return err
	}
	fromBond := uint64(0)
	if amt > 0 {
		owed := uint64(0)
		if job.Fee > refundedRetained { // balance = what was already paid immediately to the miner (0 at full hold)
			owed = job.Fee - refundedRetained
		}
		fromBond = k.refundClient(ctx, job, owed, amt)
		k.addTreasury(ctx, amt-fromBond) // the REST of the slash (punitive) -> Treasury
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "cheated")
	return nil
}

// clawbackPayment — ⚠️ NO PRODUCTION CALLER. The "no quorum -> clawback" path has been REPLACED by
// deferral (resolveDisputedAudit, default branch): a committee's silence is no longer billed to the
// primary, while a REALLY silent primary is still punished by the SLASH branch (⌈2/3⌉ of "0" verdicts
// posted against a missing reveal = cheat quorum, slashCheatedPrimary). Consequence: the
// `silence_slash_bps` penalty below can no longer fire — the parameter is INERT as things stand (like
// `audit_judge_model`). An inert parameter is less harmful than a penalty on silence, but it must NOT
// be publicly described as active. Removing the parameter is an economics decision; until then this
// function is KEPT as an executable reference for the former semantics. Do NOT re-wire it without
// re-reading why the deferral replaced it.
//
// (Original semantics) NO QUORUM / INSUFFICIENT PARTICIPATION: the primary does not keep an UNVERIFIED
// payment, but it is NOT hard-slashed (no proof of cheating at the required floor). Only the job price
// is taken back from its stake and refunded to the client; the SlashRecord is REFUNDABLE (an honest
// miner that was offline can recover it through governance). Mutates `job`, persisted by the caller.
//
// ADR-028 v2 — `params.SilenceSlashBps`: DORMANT (0 = the v1 behaviour above, price clawback only).
// When > 0, a DEDICATED stake penalty `stake·SilenceSlashBps/10000` is added (calibrated at or above
// the compute saved by the lazy "stay silent" cheat), paid to the Treasury and RECORDED as a
// REFUNDABLE SlashRecord. The penalty is added ON TOP of the price clawback, computed on the REMAINING
// stake (after clawback) and bounded, so it can never exceed the available stake.
//
// The silence penalty requires `muteSignal` = AT LEAST ONE "0" verdict posted (the non-forgeable
// ADR-028 signal: an absent or unusable reveal, or a genuine suspicion). A no-quorum made of PURE
// ABSTENTIONS (no verdict posted at all — an uncertain judge abstains) triggers ONLY the price
// clawback. The earlier behaviour punished the HONEST miner for the JUDGE's uncertainty: measured at
// −20 % of compounded bond per incident without a single hard slash, which erodes trust exactly like a
// false slash, only slower. A truly silent primary draws "0" verdicts en masse (posted against the
// missing reveal) -> muteSignal true -> the -EV property is preserved.
func (k Keeper) clawbackPayment(ctx context.Context, job *types.Job, params types.Params, muteSignal bool) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		return nil
	}
	// PLAN-V2-FEE-HOLD §A: refund the client out of the RETAINED FEE first; touch the BOND only for the
	// SHORTFALL (held < fee). With hold_bps=10000, held ≈ fee, so the bond is INTACT — an honest miner
	// below the floor is a LIVENESS failure of the committee, not of the miner, and its security
	// deposit must survive it. The refund draws on the WHOLE retained fee (held minerNet + the cut
	// reversed from Pools + the deferred burn given back) and reverses the Demand BEFORE any recourse
	// to the bond. At FULL HOLD the retained amount is the entire fee -> `owed` = 0 -> BOND INTACT. At
	// partial hold the bond only covers what was already paid immediately to the miner. (hold_bps=0 ->
	// nothing retained -> v1 behaviour: the price comes out of the bond.)
	refundedRetained := k.refundRetainedToClient(ctx, job, params, &miner)
	owed := uint64(0)
	if job.Fee > refundedRetained {
		owed = job.Fee - refundedRetained
	}
	amt := owed
	if amt > miner.Stake {
		amt = miner.Stake
	}
	if amt > 0 {
		miner.Stake -= amt
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil { // persists the reversed Demand (and any clawback)
		return err
	}
	fromBond := uint64(0)
	if amt > 0 {
		fromBond = k.refundClient(ctx, job, owed, amt)
		k.addTreasury(ctx, amt-fromBond) // invalid client address -> remainder to the Treasury (never lost)
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	if refundedRetained+fromBond > 0 || amt > 0 {
		k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "no_quorum")
	}

	// ADR-028 v2 — DEDICATED silence penalty (dormant at 0). Computed on the stake REMAINING after the
	// price clawback. Only on a real mute signal (at least one "0" verdict posted), never on pure
	// abstentions.
	if params.SilenceSlashBps > 0 && muteSignal {
		penalty := miner.Stake * params.SilenceSlashBps / 10000
		if penalty > miner.Stake {
			penalty = miner.Stake // safety bound (unreachable for bps <= 10000; defensive)
		}
		if penalty > 0 {
			miner.Stake -= penalty
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return err
			}
			k.addTreasury(ctx, penalty) // coins already held by the module (bond) -> Treasury counter
			job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: penalty})
			k.emitClawback(ctx, job.JobId, job.MinerId, penalty, 0, "silence_slash")
		}
	}
	return nil
}

// refundClient — refunds min(`owed`, `dispo`) to the client out of the module: `owed` = what is still
// owed to the client, `dispo` = the available source (for instance the amount taken from the stake).
// Returns the amount ACTUALLY refunded (0 on an invalid address). PLAN-V2-FEE-HOLD: making `owed`
// explicit is what allows refunding the BALANCE left after the retained fee.
func (k Keeper) refundClient(ctx context.Context, job *types.Job, owed, dispo uint64) uint64 {
	refund := owed
	if refund > dispo {
		refund = dispo
	}
	if refund == 0 {
		return 0
	}
	cliBz, e := k.addressCodec.StringToBytes(job.Client)
	if e != nil {
		return 0
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(refund)))
	if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), coins); err != nil {
		return 0
	}
	return refund
}

// addTreasury — increments the Treasury counter (the coins are already held by the module).
// Best-effort: it must never block EndBlock.
func (k Keeper) addTreasury(ctx context.Context, amt uint64) {
	if amt == 0 {
		return
	}
	pools, err := k.Pools.Get(ctx)
	if err != nil {
		pools = types.Pools{}
	}
	pools.Treasury += amt
	_ = k.Pools.Set(ctx, pools)
}

// refundRetainedToClient — refunds the client out of the WHOLE fee RETAINED by the module, in order,
// BEFORE any recourse to the bond:
//
//	(1) the held minerNet (HeldFee);
//	(2) the cut still sitting in Pools (never distributed -> reversed to the client), plus a bounded
//	    reversal of THIS job's Demand (treasury+team, if it was credited at settle time, i.e. when
//	    client != operator);
//	(3) the DEFERRED burn (HeldBurn -> GIVEN BACK to the client, NEVER burned on an undelivered job).
//
// Mutates `miner.Demand` (the caller Sets the miner). Returns the total refunded to the client: at
// FULL HOLD that is the entire fee, so the caller does NOT touch the bond; at partial hold the balance
// (what was already paid immediately to the miner) remains owed and is covered by the bond. Consumes
// HeldFee and HeldBurn; the Pools decrement is bounded. Best-effort: with an invalid client address,
// whatever cannot be sent goes to the Treasury and is never lost.
//
// `miner` MAY be nil when the primary is ABSENT. Refunding the CLIENT does not depend on the miner
// still existing; only the Demand reversal (a miner field) is skipped. Without that tolerance the
// retention froze on a resolved job — an orphan debt that held-summary watched age with no path to
// settle it.
func (k Keeper) refundRetainedToClient(ctx context.Context, job *types.Job, params types.Params, miner *types.Miner) uint64 {
	sent := uint64(0)
	// (1) held minerNet
	if held, err := k.HeldFee.Get(ctx, job.JobId); err == nil && held > 0 {
		_ = k.HeldFee.Remove(ctx, job.JobId)
		_ = k.HeldSince.Remove(ctx, job.JobId) // twin key: once the debt is settled its date goes with it, or the age LIES
		s := k.refundClient(ctx, job, held, held)
		sent += s
		k.addTreasury(ctx, held-s) // invalid client -> remainder to the Treasury
	}
	// (2) the cut still sitting in Pools (never distributed) -> reversed to the client; plus the Demand reversal
	cut := job.Fee * params.ProtocolFeeBps / 10000
	if cut > 0 {
		pools, perr := k.Pools.Get(ctx)
		if perr != nil {
			pools = types.Pools{}
		}
		rev := cut
		if avail := pools.Validators + pools.Team + pools.Treasury; rev > avail {
			rev = avail // never reverse more than the cut sub-pools currently hold
		}
		if rev > 0 {
			if s := k.refundClient(ctx, job, rev, rev); s > 0 {
				rem := s // bounded decrement of the sub-pools (treasury, then team, then validators)
				for _, fld := range []*uint64{&pools.Treasury, &pools.Team, &pools.Validators} {
					take := *fld
					if take > rem {
						take = rem
					}
					*fld -= take
					rem -= take
				}
				_ = k.Pools.Set(ctx, pools)
				sent += s
			}
		}
		// Reverse this job's Demand (treasury+team = cut − validators), ONLY if it was credited at settle
		// time. `miner == nil` (absent primary): there is no Demand to reverse, the record is gone.
		if miner != nil && job.Client != miner.Operator {
			demR := cut - cut*params.ValidatorRewardBps/10000
			if demR > miner.Demand {
				demR = miner.Demand
			}
			miner.Demand -= demR
		}
	}
	// (3) DEFERRED burn -> GIVEN BACK to the client (no burn on an undelivered job)
	if hb, err := k.HeldBurn.Get(ctx, job.JobId); err == nil && hb > 0 {
		_ = k.HeldBurn.Remove(ctx, job.JobId)
		s := k.refundClient(ctx, job, hb, hb)
		sent += s
		k.addTreasury(ctx, hb-s)
	}
	return sent
}

func (k Keeper) emitClawback(ctx context.Context, jobId, minerId string, slashed, refunded uint64, reason string) {
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
		sdk.NewEvent("audit_clawback",
			sdk.NewAttribute("job_id", jobId),
			sdk.NewAttribute("miner_id", minerId),
			sdk.NewAttribute("reason", reason),
			sdk.NewAttribute("slashed", math.NewIntFromUint64(slashed).String()),
			sdk.NewAttribute("refunded", math.NewIntFromUint64(refunded).String()),
		),
	)
}

// releaseHeld (PLAN-V2-FEE-HOLD §A) — releases the RETAINED fee (HeldFee[jobId]) to the primary's
// OPERATOR when the job is VINDICATED or NOT AUDITED (optimistic payment confirmed). No-op when
// nothing is held (hold_bps == 0 at settlement). Best-effort: an address failure must not block
// EndBlock (the coins stay with the module). Idempotent (Remove first).
func (k Keeper) releaseHeld(ctx context.Context, job *types.Job) {
	// FINALITY reached (vindicated or not audited): the DEFERRED burn happens NOW — it is a tax on
	// SUCCESSFUL work. Independent of the minerNet retention below, and it can exist even when held == 0.
	if hb, e := k.HeldBurn.Get(ctx, job.JobId); e == nil && hb > 0 {
		// ADR-034 M8 — BURN THEN REMOVE, never the other way round. Removing the accounting key before
		// `BurnCoins` (with the burn error swallowed) erased it even when the burn failed: the coins
		// stayed with the module, neither burned nor traced — an orphan residue, the very defect the rest
		// of this file fixes for the retention. The trace is only dropped once the burn SUCCEEDS;
		// otherwise HeldBurn survives, the coins stay accounted for, and the next finality retries.
		if bErr := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(hb)))); bErr == nil {
			_ = k.HeldBurn.Remove(ctx, job.JobId)
		}
	}
	held, err := k.HeldFee.Get(ctx, job.JobId)
	if err != nil || held == 0 {
		_ = k.PendingHeldRelease.Remove(ctx, job.JobId) // nothing to release -> no pending retry
		_ = k.HeldSince.Remove(ctx, job.JobId)          // defence in depth: no date survives without its debt
		return
	}
	// ADR-034 F3(ii) — THE RETENTION IS ONLY REMOVED AFTER A SUCCESSFUL TRANSFER.
	//
	// Removing `HeldFee` BEFORE `Miner.Get`, and then ignoring the transfer error, destroyed an HONEST
	// miner's payment through two paths: (a) miner gone -> bare `return`, with the retention already out
	// of the KV store and the money left with the module, no accounting key remaining to claim it;
	// (b) transfer failure -> swallowed, same result. Path (a) was reachable in practice: audit deferral
	// is UNBOUNDED, so a job can outlive the inactivity threshold, its miner be evicted, and its held fee
	// evaporate — in exactly the situation both guards exist for, a small pool. It violated "never bill
	// an outage to an honest party" and broke the escrow balance.
	//
	// General rule: erasing the trace of a debt before proving it is paid turns an outage into a loss.
	// The retention survives every failure — it stays owed, and stays claimable.
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		_ = k.PendingHeldRelease.Set(ctx, job.JobId) // claimable debt -> retried at the next epoch
		return                                       // retention KEPT: the miner may reappear, the debt stays on the books
	}
	toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
	if aErr != nil {
		_ = k.PendingHeldRelease.Set(ctx, job.JobId)
		return // likewise: an unreadable address is no reason to erase a debt
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(held)))
	if sErr := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); sErr != nil {
		// TRANSFER FAILED: the retention is NOT removed AND the job is QUEUED for retry. Keeping the
		// retention without any handler ever replaying it made "stays claimable" false — a slow
		// confiscation. The EndBlocker sweeps `PendingHeldRelease` and retries until it succeeds.
		_ = k.PendingHeldRelease.Set(ctx, job.JobId)
		return
	}
	_ = k.HeldFee.Remove(ctx, job.JobId)            // paid: the debt can finally be settled
	_ = k.HeldSince.Remove(ctx, job.JobId)          // twin key: the date goes WITH the debt (an age must never outlive what it dates)
	_ = k.PendingHeldRelease.Remove(ctx, job.JobId) // settled -> removed from the retry queue
}

// retryHeldReleases — REPLAYS retention releases that are still DUE after a failed transfer. It is what
// makes "stays claimable" true: without it, a retention kept after a failed transfer was never retried.
// Best-effort, swept once per epoch by the EndBlocker (cost proportional to the number of stuck
// releases, never to traffic). `releaseHeld` removes the job from the queue on success and re-marks it
// on failure.
func (k Keeper) retryHeldReleases(ctx context.Context) error {
	var jobs []string
	if err := k.PendingHeldRelease.Walk(ctx, nil, func(jobId string) (bool, error) {
		jobs = append(jobs, jobId)
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range jobs {
		job, err := k.Job.Get(ctx, jobId)
		if err != nil {
			_ = k.PendingHeldRelease.Remove(ctx, jobId) // job gone -> nothing left to release
			continue
		}
		k.releaseHeld(ctx, &job) // retry; leaves the queue on success, re-marks itself on failure
	}
	return nil
}
