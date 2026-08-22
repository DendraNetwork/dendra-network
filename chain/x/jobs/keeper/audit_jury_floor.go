package keeper

import (
	"context"
	"strconv"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// deferAuditForSmallJury — INSUFFICIENT JUDGE POOL: DEFER, NEVER PUNISH.
//
// The failure this prevents: a job served by an HONEST primary (real inference, correct answer,
// anchored commit) ends `+resolved+clawed` because the open audit CANNOT conclude. The jury-eligible
// set is "every miner EXCEPT the primary" (drawAuditCommittee); when only two remain, both silent,
// against an `audit_min_quorum` floor of 4, the quorum is UNREACHABLE BY CONSTRUCTION. No conduct of
// the miner, however irreproachable, can then avoid the clawback of its payment. That is exactly
// "billing an infrastructure outage to the honest party", the invariant this protocol refuses.
//
// WHY THE ORIGINAL REASONING STILL HOLDS ELSEWHERE. `runAuditResolveTimeout` treats "below the floor"
// as an UNVERIFIED payment and claws it back (a light, restitutable clawback). That is correct when a
// committee CAPABLE of judging abstains: the silence is then a signal, and the doubt must cost the
// party paid, not the network. It becomes FALSE when the committee could not exist: the silence is no
// longer a signal, it is an absent network. The two cases are therefore separated HERE, at the draw —
// by timeout time, the fact that the pool was too small has already been lost.
//
// THIS IS NOT ONLY A COLD-START CASE, IT IS AN ATTACK VECTOR. Silent miners are ELIGIBLE AS JUDGES.
// Registering N identities at `min_stake` that never answer costs N·min_stake ONCE and produces two
// cumulative effects: (a) they capture jobs that are never served, whose escrow stays locked (nothing
// evicts them: `silence_slash` goes through `clawbackPayment`, which requires a prior payment that
// never arrives); (b) they occupy committee seats without ever voting, hence systematic no-quorum,
// hence SERIAL CLAWBACKS ON HONEST MINERS. At a modest N the network stops paying anyone who works, and
// the only ones punished are those who produce. Deferring instead of punishing removes the whole
// benefit of the attack: the attacker can DELAY an audit, it can no longer make an honest miner LOSE.
//
// SAME SHAPE AS THE NEIGHBOURING GUARD, DELIBERATELY. When no decentralized seed exists,
// `runOptimisticAudit` already defers by a wide stride (`auditDeferStride`) instead of drawing. Same
// diagnosis ("the network is not in a state to audit"), therefore same answer: deferral by a wide
// stride, the payment stays HELD (this is the "audit drawn" branch: nothing is released, no funds
// move), and automatic resorption as soon as the network has enough judges. The payment is never
// released after N attempts: releasing for want of an audit would make the dodge free.
//
// The current deadline is removed here, because the caller does `continue` and skips its own
// end-of-loop `PendingAudit.Remove` — without this, the pending key would leak into the KV store.
//
// TWO HEIGHTS, AND CONFUSING THEM LEAKS A KEY. `hPending` is the height the entry is FILED UNDER,
// which is no longer necessarily the current one: the audit walk now covers every due height
// (`K1 <= h`) so that a capped block strands nothing, so an entry taken here may carry an older
// height. Removing `Join(h, ...)` in that case deletes NOTHING and the old key stays due forever,
// re-collected at every block — the unbounded growth this cap exists to prevent, reintroduced by its
// own fix. The new deadline, on the other hand, is counted from the CURRENT height: the retry is
// `auditDeferStride` blocks from now, not from a date already past.
func (k Keeper) deferAuditForSmallJury(ctx context.Context, h, hPending int64, jobId string, eligibles, floor int) error {
	if err := k.PendingAudit.Set(ctx, collections.Join(h+auditDeferStride, jobId)); err != nil {
		return err
	}
	if err := k.PendingAudit.Remove(ctx, collections.Join(hPending, jobId)); err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	sdkCtx.Logger().Error("SECURITY: pool of eligible judges INSUFFICIENT -> audit DEFERRED, NO sanction. The primary is neither slashed nor clawed back: it cannot do anything about the absence of judges. Usual causes: too few live miners, or silent identities occupying the seats. If this message persists, the network verifies NOTHING.",
		"job_id", jobId, "eligible", eligibles, "floor", floor, "next_attempt", h+auditDeferStride)
	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"audit_draw_deferred",
		sdk.NewAttribute("job_id", jobId),
		sdk.NewAttribute("reason", "insufficient eligible judge pool"),
		sdk.NewAttribute("eligibles", strconv.Itoa(eligibles)),
		sdk.NewAttribute("floor", strconv.Itoa(floor)),
	))
	return nil
}
