package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// Anti-evasion of the optimistic slash.
//
// Problem: under optimistic verification with k=1 the primary is paid first. The J1 reveal (re-sealing
// prompt and answer for the fresh committee) is cooperative: only the primary can produce it. A primary
// that cheated and then stayed silent was neither judged nor slashed, because the liveness timeout
// vindicated it by default. Silence was the cheater's dominant strategy, and the Nash inequality no
// longer held.
//
// Fix: the optimistic primary is the suspect and never keeps an unverified payment. Two levers and a
// floor:
//  1. Off-chain (non-forgeable signal): `judge_worker.py` posts a "0" verdict when it cannot obtain a
//     usable reveal after the grace period. A primary that stays silent, or reveals nothing usable, is
//     turned into a cheat quorum by the committee itself, never by a forgeable on-chain marker.
//  2. On-chain (runAuditResolveTimeout): at the deadline, above the participation floor, a cheat
//     quorum means hard slash + clawback and a valid quorum means vindication; below the floor or with
//     no majority, a light refundable clawback (neither hard slash nor vindication).
//  3. Symmetric participation floor (auditSlashFloor): the tally is computed on the stake of those
//     present. Without a floor, two judges or sybils forming a "majority of those present" would slash
//     an honest miner (grief) or vindicate a cheater (the dual of grief) whenever the honest judges
//     vote late. Hard slash and vindication therefore require enough distinct judges to have voted
//     (⌈AuditLegacyFloorBasis/2⌉+1 = 4, independent of CommitteeSize=3).

// AuditLegacyFloorBasis does not describe the size of a committee. The audit draw targets 15 seats
// (`auditCommitteeDrawSize`, audit_committee.go). A name that implicitly claims to describe the
// current state hides its own staleness, because nobody re-reads a threshold whose name says it is up
// to date. The absolute floor of 4 this basis produces is 4/15 = 27 % on a committee three times
// larger than the one it was calibrated for, while the old name still read "4 out of 5".
//
// A name gets copied into every new call site by whoever reads it in good faith, so the constant is
// named for what it is: the historical basis of the v1 floor, and nothing else. A reader looking for
// the committee size will not find it here, on purpose.
//
// It remains the only input of `auditSlashFloor()`, the absolute participation floor of the v1
// fallback. The seat bar is relative to the anchored seats and lives in `auditRelativeBar`. It is
// independent of `CommitteeSize`=3 (committee.go), which stays the size of the redundant (mode 0)
// committee: raising that one would break `settle_semantic`, which requires
// `len(vecs) >= CommitteeSize`.
const AuditLegacyFloorBasis = 5

// auditSlashFloor (participation floor) is the minimum number of distinct fresh judges present
// for a hard decision (slash or vindication): ⌈AuditLegacyFloorBasis/2⌉+1 = 4. The tally is computed
// on the stake of those present; without this floor, two judges or sybils forming a "majority of those
// present" could slash an honest miner (grief) or vindicate a cheater (the dual of grief) whenever the
// honest judges vote late. Below it, a light refundable clawback, never a hard slash and never a
// vindication. This is an absolute floor and says nothing about the number of seats drawn; the
// seat-dependent bar is `auditRelativeBar`, and the two must not be confused. v2 is a governed fraction
// via effectiveSlashFloor/AuditMinQuorum.
func auditSlashFloor() int { return (AuditLegacyFloorBasis+1)/2 + 1 }

// effectiveSlashFloor is the effective participation floor: `AuditMinQuorum` (governed, an
// integer count of distinct fresh judges) when it is > 0, otherwise the `auditSlashFloor()` fallback
// (4). Dormant: while `audit_min_quorum == 0` (the default) it returns exactly `auditSlashFloor()`, so
// v1 behaviour is unchanged. Every hard-slash decision site (timeout and AdjudicateDispute) goes
// through here.
func effectiveSlashFloor(p types.Params) int {
	if p.AuditMinQuorum > 0 {
		return int(p.AuditMinQuorum)
	}
	return auditSlashFloor()
}

// auditVerdictTally is the stake-weighted tally of the fresh committee's verdicts
// (`<jobId>__verdict__<minerId>`, "0" = invalid). It excludes the primary (under k=1 it is the only
// "origin") and accepts only members of the committee anchored at draw time.
//
// Do not relax this filter. It once accepted any registered miner, on the assumption that registering
// "costs something". That does not hold quantitatively: at `min_stake`, four identities cost 0.2 DNDR
// and were enough to strip any honest miner of 80 % of its stake, and a test asserted that behaviour.
// Membership in the drawn committee is now the only authorisation to vote.
//
// It returns (cheated, valid, voters): `voters` is the number of summoned judges that voted, cheated a
// strict majority of "invalid" stake, valid a strict majority of "valid" stake (a tie yields neither).
// `invalidVotes` is the count of "invalid" judges (not their stake), consumed by the veto
// (auditSlashDecision), whose threshold is relative to the anchored committee (⌈2/3⌉ of the seats)
// and no longer "4 out of 5". The caller applies effectiveSlashFloor() to the
// vindication and auditSlashDecision() to the slash (veto count when governed, otherwise stake majority
// at the v1 floor). Below either, neither slash nor vindication: a light refundable clawback.
// (totalStake <= the 1e13 udndr supply, so *2 cannot overflow uint64.)
//
// `distinctOps` is the number of distinct operators behind the counted votes: a
// quorum reached by 15 seats held by a single operator is a quorum in form only. `maxOpVotes` is the
// largest number of votes held by one operator, since plurality is not counted in heads ("distinct >=
// 2" holds when one operator has 9 votes of 10), and the C3 gate reads `maxOpVotes/voters < 50 %`. Both
// are counted here, inside the same filter as the tally (anchored, non-primary, registered), because a
// second loop with its own filter would eventually diverge. An empty operator falls into a single
// bucket, so distinct is under-counted and maxOpVotes over-counted, and both errors harden the gate.
func (k Keeper) auditVerdictTally(ctx context.Context, jobId, primId string) (cheated, valid bool, voters, invalidVotes, seats, distinctOps, maxOpVotes int) {
	// Only the summoned vote. Without this barrier, being a registered miner was enough to weigh
	// on a slash: four identities at min_stake cost an honest miner 80 % of its stake.
	// Fail closed: no anchored committee (older jobs, paths outside mode 1) gives an empty tally, so no
	// hard slash is possible and the light clawback applies. The asymmetry is deliberate: missing a
	// cheater is bounded and -EV, while slashing an honest miner cannot be undone.
	allowed, anchored := k.auditCommitteeAllowed(ctx, jobId)
	if !anchored {
		return false, false, 0, 0, 0, 0, 0
	}
	// The denominator counts seats that can still vote, not seats that were drawn.
	//
	// It is not `len(allowed)`, the committee as anchored at draw time. A verdict below only counts when
	// `k.Miner.Get(mid)` succeeds (a registered miner is required), and a juror can leave the registry
	// after the anchor, through eviction for inactivity or a delete. A draw-time denominator would keep
	// its size while the number of votes that can still be cast drops, so `auditRelativeBar` would ask
	// for a share of a committee that no longer exists. The bar would become unreachable, the audit would
	// defer forever, and the held fee and dispute bond would stay locked, with nothing reporting it.
	//
	// Shrinking the denominator is safe here, despite the obvious fear that an attacker silences jurors to
	// lower the bar and slash an honest primary cheaply. Two independent floors forbid it:
	// `auditVindicateDecision` requires `voters >= effectiveSlashFloor(p)`, an absolute count that does not
	// depend on seats, and `auditRelativeBar` starts from that same floor before taking ⌈2/3 x seats⌉.
	// Removing seats can only reduce the relative part, never take the decision below the absolute floor.
	seats = 0
	for mid := range allowed {
		if _, mErr := k.Miner.Get(ctx, mid); mErr == nil {
			seats++
		}
	}
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
			return false, nil // not summoned: its verdict does not count
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // a registered miner is required
		}
		voters++
		totalStake += m.Stake
		ops[m.Operator]++ // real plurality: the operator, not the seat (a count, not a set)
		if strings.TrimSpace(c.ResultCommit) == "0" {
			invalidStake += m.Stake
			invalidVotes++ // veto: count the "invalid" verdicts (bar relative to anchored seats), not just their stake
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

// auditRelativeBar is the seat bar, the single source for both the slash and the vindication.
//
// Why the bar must be relative, and why the default path is the dangerous one: the relative bar
// ⌈2/3 x anchored seats⌉ once existed only inside the `audit_min_quorum > 0` branch. Under the default
// setting (`audit_min_quorum = 0`, params.go, which `Params.Validate()` accepts together with
// `verification_mode = 1`, since it only rejects a governed quorum below 4), the decision fell back to
// v1: `voters >= auditSlashFloor()` = 4, an absolute count.
//
// That 4 was right for a 5-seat committee (4/5 is near-unanimity, so an honest miner judged invalid by
// a minority is never slashed). The draw was raised to 15 seats for liveness, and 4 out of 15 is
// 27 %: on the default path, four jurors hard-slashed an honest miner even against eleven "valid"
// votes. The threshold was right and its denominator had changed underneath it, the same
// absolute-versus-relative defect the audit and re-adjudication tallies once had, and the only occurrence that survived in the
// default configuration.
//
// The public README also states that a hard slash requires at least two thirds of the anchored seats to
// return invalid, which was only true if the launcher remembered to arm `audit_min_quorum`; a security
// property cannot depend on an optional setting.
//
// So there is one function with two callers: a new call site cannot diverge, because there is no
// second computation to forget to update.
// At `seats == 0` it yields the floor alone, and the tally is already fail-closed upstream.
func auditRelativeBar(p types.Params, seats int) int {
	need := auditSlashFloor() // absolute v1 floor (⌈N/2⌉+1)
	if p.AuditMinQuorum > 0 {
		need = int(p.AuditMinQuorum) // governed floor, lower bound
	}
	if q := (2*seats + 2) / 3; q > need { // ⌈2/3 x anchored seats⌉ in integer arithmetic
		need = q
	}
	return need
}

// auditSlashDecision (pro-honest veto) decides the hard slash.
//
// `seats` is the size of the anchored committee. At 0 (jobs without an anchor) only the governed floor
// applies, but the tally is already fail-closed upstream, so that path slashes nothing. Pure Go,
// testable outside the keeper.
func auditSlashDecision(p types.Params, cheated bool, voters, invalidVotes, seats int) bool {
	// Three independent locks, each priced in capital: participation (effective floor), seats (⌈2/3⌉
	// of the anchored ones) and a stake majority. Requiring all three can only reduce the number of
	// slashes. That is pro-honest here and only here: see `auditVindicateDecision`, which the same
	// hardening makes harder to obtain, at the honest miner's expense.
	return voters >= effectiveSlashFloor(p) && invalidVotes >= auditRelativeBar(p, seats) && cheated
}

// auditVindicateDecision decides the vindication, at the same relative bar as the slash.
//
// The slash requires ⌈2/3⌉ of the anchored seats. If the vindication only needed
// the governed floor, about 27 % of the stake (4 seats of 15) would buy the vindication of any cheat
// where 67 % is needed to slash it. The two outcomes differ only by the direction of the majority
// (validVotes against invalidVotes), never by the bar for being heard. `validVotes` is the
// number of summoned jurors that voted "valid" (voters - invalidVotes). Both outcomes read
// `auditRelativeBar`, so the symmetry is structural.
//
// The reason is symmetry; the pro-honest argument for the slash does not cover this function. Hardening
// the slash protects the honest miner, and hardening the vindication costs it: vindication is what
// releases an honest primary's held fee, and at the default the requirement goes from 4 voters to
// ⌈2/3 of the anchored seats⌉ valid votes, 10 out of 15. The price is longer retention for honest
// miners, in a network that already struggles to close its audits, and the deferral for want of a jury is not
// bounded.
//
// The price is paid because the alternative is worse: a vindication cheaper than a slash lets a small
// colluding subset buy a cheater's innocence for about 27 % of the stake when 67 % is needed to
// convict it.
//
// Scope: with about 5 anchored seats, ⌈2/3 x 5⌉ = 4 = the floor, and the hardening changes nothing.
// The cost only appears as the pool grows. If retention becomes an observed problem, the answer is to
// bound the deferral rather than lower this bar.
func auditVindicateDecision(p types.Params, valid bool, voters, validVotes, seats int) bool {
	return voters >= effectiveSlashFloor(p) && validVotes >= auditRelativeBar(p, seats) && valid
}

// slashCheatedPrimary handles a proven cheat (a quorum of "invalid" verdicts above the bar): a hard
// slash of `SlashLeakBps` of the primary's stake (a counter; the module already holds the coins through
// the bond), a refund to the client of the price paid (min(fee, slashed)), the remainder to the
// Treasury, and a refundable SlashRecord. Bounded; the transfer is best-effort (an address failure must
// not block EndBlock). It mutates `job`, which the caller persists.
func (k Keeper) slashCheatedPrimary(ctx context.Context, job *types.Job, params types.Params) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		// The primary may be absent: a job whose miner was evicted, or state carried in by a migration
		// or an import. The `hasOpenObligation` exit guard normally makes this unreachable, but it is
		// handled anyway. A bare `return nil` froze the retention forever: the job stayed in
		// `+resolved+clawed+quorum` with its HeldFee intact and no path left to settle it, the client
		// was never refunded, and held-summary watched a dead debt age. There is nothing to slash, but
		// the client is still refunded out of the retention (nil = no Demand to reverse) and the
		// clawback is traced (slashed=0, explicit reason).
		refunded := k.refundRetainedToClient(ctx, job, params, nil)
		k.emitClawback(ctx, job.JobId, job.MinerId, 0, refunded, "cheated_primary_vanished")
		return nil
	}
	// The client is refunded first out of the whole retained fee (held minerNet + this job's
	// retained cut + the deferred burn given back), and a credited Demand is reversed. The hard
	// slash of the bond stays punitive (Treasury); it only refunds the client for whatever was paid
	// immediately (partial hold; nothing at full hold).
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
		k.addTreasury(ctx, amt-fromBond) // the rest of the slash (punitive) goes to the Treasury
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "cheated")
	return nil
}

// clawbackPayment has no production caller. The "no quorum -> clawback" path was replaced by
// deferral (resolveDisputedAudit, default branch): a committee's silence is no longer billed to the
// primary, while a silent primary is still punished by the slash branch (⌈2/3⌉ of "0" verdicts posted
// against a missing reveal form a cheat quorum, slashCheatedPrimary). As a result the
// `silence_slash_bps` penalty below can no longer fire: the parameter is currently inert, like
// `audit_judge_model`. An inert parameter is less harmful than a penalty on silence, but it must not
// be publicly described as active. Removing the parameter is an economics decision; until then this
// function is kept as an executable reference for the former semantics. Do not wire it again without
// re-reading why the deferral replaced it.
//
// Original semantics, for no quorum or insufficient participation: the primary does not keep an
// unverified payment, but it is not hard-slashed (no proof of cheating at the required floor). Only the
// job price is taken back from its stake and refunded to the client; the SlashRecord is refundable (an
// honest miner that was offline can recover it through governance). It mutates `job`, persisted by
// the caller.
//
// `params.SilenceSlashBps` is dormant (0 = the v1 behaviour above, price clawback only).
// When > 0, a dedicated stake penalty `stake x SilenceSlashBps/10000` is added (calibrated at or above
// the compute saved by the lazy "stay silent" cheat), paid to the Treasury and recorded as a refundable
// SlashRecord. The penalty comes on top of the price clawback, computed on the remaining stake (after
// clawback) and bounded, so it can never exceed the available stake.
//
// The silence penalty requires `muteSignal`, at least one "0" verdict posted (the non-forgeable
// silence signal: an absent or unusable reveal, or a genuine suspicion). A no-quorum made of pure
// abstentions (no verdict posted at all, since an uncertain judge abstains) triggers only the price
// clawback. The earlier behaviour punished the honest miner for the judge's uncertainty, measured at
// -20 % of compounded bond per incident without a single hard slash, which erodes trust like a false
// slash, only slower. A truly silent primary draws "0" verdicts en masse (posted against the missing
// reveal), so muteSignal is true and the -EV property holds.
func (k Keeper) clawbackPayment(ctx context.Context, job *types.Job, params types.Params, muteSignal bool) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		return nil
	}
	// Fee hold: refund the client out of the retained fee first, and touch the bond only for
	// the shortfall (held < fee). With hold_bps=10000, held is about fee, so the bond stays intact: an
	// honest miner below the floor is a liveness failure of the committee, not of the miner, and its
	// security deposit must survive it. The refund draws on the whole retained fee (held minerNet + the
	// cut reversed from Pools + the deferred burn given back) and reverses the Demand before any recourse
	// to the bond. At full hold the retained amount is the entire fee, so `owed` = 0 and the bond is
	// intact. At partial hold the bond only covers what was already paid immediately to the miner.
	// (hold_bps=0: nothing retained, v1 behaviour, the price comes out of the bond.)
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
		k.addTreasury(ctx, amt-fromBond) // invalid client address: remainder to the Treasury (never lost)
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	if refundedRetained+fromBond > 0 || amt > 0 {
		k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "no_quorum")
	}

	// Dedicated silence penalty (dormant at 0), computed on the stake remaining after the
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
			k.addTreasury(ctx, penalty) // coins already held by the module (bond): Treasury counter
			job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: penalty})
			k.emitClawback(ctx, job.JobId, job.MinerId, penalty, 0, "silence_slash")
		}
	}
	return nil
}

// refundClient refunds min(`owed`, `available`) to the client out of the module: `owed` is what is
// still owed to the client, `available` what the refund may draw on (for instance the amount taken
// from the stake). It returns the amount actually refunded (0 on an invalid address). Under the
// fee hold, an explicit `owed` allows refunding the balance left after the retained fee.
func (k Keeper) refundClient(ctx context.Context, job *types.Job, owed, available uint64) uint64 {
	refund := owed
	if refund > available {
		refund = available
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

// addTreasury increments the Treasury counter (the module already holds the coins).
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

// refundRetainedToClient refunds the client out of the whole fee retained by the module, in order,
// before any recourse to the bond:
//
//	(1) the held minerNet (HeldFee);
//	(2) this job's retained cut, share by share (retained until finality, so before it the
//	    whole cut is still here), and its subsidy right: dropped when still pending, reversed
//	    (bounded) when the job was already final and had credited it;
//	(3) the deferred burn (HeldBurn, given back to the client, never burned on an undelivered job).
//
// It mutates `miner.Demand` (the caller sets the miner). It returns the total refunded to the client:
// at full hold that is the entire fee, so the caller does not touch the bond; at partial hold the
// balance (what was already paid immediately to the miner) remains owed and the bond covers it. It
// consumes HeldFee and HeldBurn; the Pools decrement is bounded. Best-effort: with an invalid client
// address, whatever cannot be sent goes to the Treasury and is never lost.
//
// `miner` may be nil when the primary is absent. Refunding the client does not depend on the miner
// still existing; only the Demand reversal (a miner field) is skipped. Without that tolerance the
// retention froze on a resolved job, an orphan debt that held-summary watched age with no path to
// settle it.
func (k Keeper) refundRetainedToClient(ctx context.Context, job *types.Job, params types.Params, miner *types.Miner) uint64 {
	sent := uint64(0)
	// (1) held minerNet
	if held, err := k.HeldFee.Get(ctx, job.JobId); err == nil && held > 0 {
		_ = k.HeldFee.Remove(ctx, job.JobId)
		_ = k.HeldSince.Remove(ctx, job.JobId) // twin key: once the debt is settled its date goes with it, or the age would be wrong
		s := k.refundClient(ctx, job, held, held)
		sent += s
		k.addTreasury(ctx, held-s) // invalid client -> remainder to the Treasury
	}
	// (2) This job's retained cut goes to the client, share by share. It replaces a reversal
	// that took "the cut" from whichever sub-pool of `Pools` held enough, in a fixed order: refunding
	// one job drew on the shares of others and left this job's own shares as counters nobody would pay.
	cutSent, hadRight := k.refundRetainedCut(ctx, job)
	sent += cutSent
	// The subsidy right. Still pending (`hadRight`): it was never credited and is simply dropped above,
	// so there is nothing to reverse, which is why the right is born at finality. Not pending, and not
	// self-dealing: the job was already final and its right was credited at finality, so it is reversed
	// now, bounded by what the miner holds. That case is a slash decided after finality (a human dispute
	// opened inside the dispute window); the shares already paid out at finality are then owed by the
	// bond, through the caller's `owed = fee - refunded`. `miner == nil`: no record, nothing to reverse.
	if !hadRight && miner != nil && job.Client != miner.Operator {
		cut := job.Fee * params.ProtocolFeeBps / 10000
		demR := cut - cut*params.ValidatorRewardBps/10000
		if demR > miner.Demand {
			demR = miner.Demand
		}
		miner.Demand -= demR
	}
	// (3) deferred burn, given back to the client (no burn on an undelivered job)
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

// releaseHeld releases the retained fee (HeldFee[jobId]) to the primary's
// operator when the job is vindicated or not audited (optimistic payment confirmed). No-op when nothing
// is held (hold_bps == 0 at settlement). Best-effort: an address failure must not block EndBlock (the
// coins stay with the module). Idempotent (Remove first).
func (k Keeper) releaseHeld(ctx context.Context, job *types.Job) {
	// Finality reached (vindicated or not audited): the deferred burn happens now, as a tax on
	// successful work. It is independent of the minerNet retention below and can exist even when held == 0.
	if hb, e := k.HeldBurn.Get(ctx, job.JobId); e == nil && hb > 0 {
		// Burn, then remove, never the other way round. Removing the accounting key before
		// `BurnCoins` (with the burn error swallowed) erased it even when the burn failed: the coins
		// stayed with the module, neither burned nor traced. The trace is only dropped once the burn
		// succeeds; otherwise HeldBurn survives, the coins stay accounted for, and the next finality
		// retries.
		if bErr := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(hb)))); bErr == nil {
			_ = k.HeldBurn.Remove(ctx, job.JobId)
		}
	}
	// Finality also pays the protocol cut retained for this job (validators to
	// fee_collector, team to team_address, treasury into Pools) and credits the miner's subsidy right.
	// This deliberately sits before the early return below: with hold_bps = 0 nothing is held in HeldFee,
	// yet the cut and the right are still retained until this moment.
	cutPending := k.payRetainedCut(ctx, job)
	held, err := k.HeldFee.Get(ctx, job.JobId)
	if err != nil || held == 0 {
		_ = k.HeldSince.Remove(ctx, job.JobId) // defence in depth: no date survives without its debt
		if cutPending {
			_ = k.PendingHeldRelease.Set(ctx, job.JobId) // a share of the cut is still owed: retried
		} else {
			_ = k.PendingHeldRelease.Remove(ctx, job.JobId) // nothing left to release: no pending retry
		}
		return
	}
	// The retention is only removed after a successful transfer.
	//
	// Removing `HeldFee` before `Miner.Get`, and then ignoring the transfer error, destroyed an honest
	// miner's payment in two ways: (a) miner gone, a bare `return` with the retention already out of the
	// KV store and the money left with the module, with no accounting key left to claim it; (b) transfer
	// failure, swallowed, with the same result. Path (a) was reachable: audit deferral is unbounded, so a
	// job can outlive the inactivity threshold, its miner be evicted, and its held fee evaporate, in the
	// small-pool situation both guards exist for. It billed an outage to an honest party and broke the
	// escrow balance.
	//
	// Erasing the trace of a debt before proving it is paid turns an outage into a loss. The retention
	// survives every failure: it stays owed and claimable.
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		_ = k.PendingHeldRelease.Set(ctx, job.JobId) // claimable debt: retried at the next epoch
		return                                       // retention kept: the miner may reappear, and the debt stays on the books
	}
	toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
	if aErr != nil {
		_ = k.PendingHeldRelease.Set(ctx, job.JobId)
		return // likewise: an unreadable address is no reason to erase a debt
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(held)))
	if sErr := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); sErr != nil {
		// Transfer failed: the retention is not removed and the job is queued for retry. Keeping the
		// retention without any handler ever replaying it made "stays claimable" false, a slow
		// confiscation. The EndBlocker sweeps `PendingHeldRelease` and retries until it succeeds.
		_ = k.PendingHeldRelease.Set(ctx, job.JobId)
		return
	}
	_ = k.HeldFee.Remove(ctx, job.JobId)   // paid: the debt can finally be settled
	_ = k.HeldSince.Remove(ctx, job.JobId) // twin key: the date goes with the debt (an age must never outlive what it dates)
	if cutPending {
		_ = k.PendingHeldRelease.Set(ctx, job.JobId) // the miner is paid, a share of the cut is not yet
	} else {
		_ = k.PendingHeldRelease.Remove(ctx, job.JobId) // settled: removed from the retry queue
	}
}

// retryHeldReleases replays retention releases that are still due after a failed transfer. Without it,
// a retention kept after a failed transfer was never retried, and "stays claimable" was false.
// Best-effort, swept once per epoch by the EndBlocker (cost proportional to the number of stuck
// releases, never to traffic). `releaseHeld` removes the job from the queue on success and marks it
// again on failure.
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
			_ = k.PendingHeldRelease.Remove(ctx, jobId) // job gone: nothing left to release
			continue
		}
		k.releaseHeld(ctx, &job) // retry; leaves the queue on success, marks itself again on failure
	}
	return nil
}
