package keeper

import (
	"context"
	"strings"
)

// anchorWorkCommittee writes the anchor once, at the first moment the committee is derivable.
//
// It is called from both places where the seed becomes available: `OpenJob` when there is no reveal
// delay, and the EndBlocker when a deferred reveal lands. A single caller would have left half the jobs
// unanchored, and that half is the live chain's (`committee_reveal_delay = 1`).
//
// It never rewrites: an anchor that can be rewritten is not an anchor.
//
// It anchors nothing when the derivation fails (reveal pending, unreadable pool, no eligible miner), on
// purpose. Anchoring an impossible derivation would amount to inventing a committee, and no default is
// supplied on a criterion that decides who is paid.
//
// It returns no error because it is called from an EndBlocker, where an error aborts the block for
// every validator. A failed anchoring leaves the job on the derivation, the previous behaviour:
// degraded, never broken.
func (k Keeper) anchorWorkCommittee(ctx context.Context, jobId string) {
	if raw, err := k.WorkCommittee.Get(ctx, jobId); err == nil && raw != "" {
		return
	}
	ordered, err := k.deriveCommitteeOrdered(ctx, jobId, k.workCommitteeSize(ctx))
	if err != nil || len(ordered) == 0 {
		return
	}
	_ = k.WorkCommittee.Set(ctx, jobId, strings.Join(ordered, ","))
}
