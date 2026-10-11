package keeper

import (
	"context"
	"strconv"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// deferAuditForSmallJury handles an insufficient judge pool: defer, never punish.
//
// Without it, a job served by an honest primary (real inference, correct answer, anchored commit) ends
// `+resolved+clawed` because the open audit cannot conclude. The jury-eligible set is "every miner
// except the primary" (drawAuditCommittee); when only two remain, both silent, against an
// `audit_min_quorum` floor of 4, the quorum is unreachable by construction, and no conduct of the
// miner, however irreproachable, can avoid the clawback of its payment. That bills an infrastructure
// outage to the honest party, which this protocol refuses.
//
// The original reasoning still holds elsewhere. `runAuditResolveTimeout` treats "below the floor" as an
// unverified payment and claws it back (a light, restitutable clawback). That is correct when a
// committee able to judge abstains: the silence is then a signal, and the doubt must cost the party
// paid, not the network. It is wrong when the committee could not exist, because the silence is then an
// absent network, not a signal. The two cases are therefore separated here, at the draw, since by
// timeout time the fact that the pool was too small is lost.
//
// This is also an attack vector, beyond cold start. Silent miners are eligible as judges. Registering N
// identities at `min_stake` that never answer costs N x min_stake once and has two cumulative effects:
// (a) they capture jobs that are never served, whose escrow stays locked (nothing evicts them:
// `silence_slash` goes through `clawbackPayment`, which requires a prior payment that never arrives);
// (b) they occupy committee seats without ever voting, hence systematic no-quorum, hence serial
// clawbacks on honest miners. At a modest N the network stops paying anyone who works, and only those
// who produce are punished. Deferring instead of punishing removes the benefit of the attack: the
// attacker can delay an audit, but can no longer make an honest miner lose.
//
// It deliberately has the same shape as the neighbouring guard. When no decentralized seed exists,
// `runOptimisticAudit` already defers by a wide stride (`auditDeferStride`) instead of drawing. Same
// diagnosis ("the network is not in a state to audit"), so the same answer: deferral by a wide stride,
// the payment stays held (this is the "audit drawn" branch: nothing is released, no funds move), and
// automatic recovery as soon as the network has enough judges. The payment is never released after N
// attempts, since releasing for want of an audit would make the dodge free.
//
// The current deadline is removed here, because the caller does `continue` and skips its own
// end-of-loop `PendingAudit.Remove`; without this, the pending key would leak into the KV store.
//
// There are two heights, and confusing them leaks a key. `hPending` is the height the entry is filed
// under, which is not necessarily the current one: the audit walk covers every due height (`K1 <= h`)
// so that a capped block strands nothing, so an entry taken here may carry an older height. Removing
// `Join(h, ...)` in that case deletes nothing and the old key stays due forever, collected again at
// every block. The new deadline, on the other hand, is counted from the current height: the retry is
// `auditDeferStride` blocks from now, not from a date already past.
func (k Keeper) deferAuditForSmallJury(ctx context.Context, h, hPending int64, jobId string, eligibles, floor int, p types.Params) error {
	// ADR-044, gesture 2: the bound sits at the decision rather than at the caller, so that any future
	// path reaching this deferral inherits it; a bound one caller applies is a bound the next caller
	// silently escapes. Dormant at `audit_unwind_blocks = 0`, where `unwindStrandedRetention` returns
	// before reading anything.
	if k.unwindStrandedRetention(ctx, h, jobId, p) {
		// Settled, not postponed: the pending entry is removed rather than moved forward. Leaving it
		// would collect again, at every block and forever, a job that no longer has a debt.
		if err := k.PendingAudit.Remove(ctx, collections.Join(hPending, jobId)); err != nil {
			return err
		}
		return nil
	}
	if err := k.PendingAudit.Set(ctx, collections.Join(h+auditDeferStride, jobId)); err != nil {
		return err
	}
	if err := k.PendingAudit.Remove(ctx, collections.Join(hPending, jobId)); err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	sdkCtx.Logger().Error("SECURITY: pool of eligible judges INSUFFICIENT -> audit deferred, no sanction. The primary is neither slashed nor clawed back: it cannot do anything about the absence of judges. Usual causes: too few live miners, or silent identities occupying the seats. If this message persists, the network verifies nothing.",
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
