package keeper

import (
	"context"
	"strings"
)

// anchorWorkCommittee -- WRITES THE ANCHOR ONCE, at the first moment the committee is derivable.
//
// Called from BOTH places where the seed becomes available: `OpenJob` when there is no reveal delay, and
// the EndBlocker when a deferred reveal lands. A single caller would have left half the jobs unanchored,
// and that half is the live chain's (`committee_reveal_delay = 1`).
//
// IT NEVER REWRITES. An anchor that can be rewritten is not an anchor: it is the original defect with one
// more step in front of it.
//
// IT ANCHORS NOTHING WHEN THE DERIVATION FAILS, and that is deliberate: reveal pending, unreadable pool,
// no eligible miner. Anchoring an impossible derivation would amount to inventing a committee, and on a
// criterion that decides WHO IS PAID no default is supplied.
//
// It returns no error: it is called from an EndBlocker, where an error aborts the block for every
// validator. A failed anchoring leaves the job on the derivation, hence on the previous behaviour --
// degraded, never broken.
func (k Keeper) anchorWorkCommittee(ctx context.Context, jobId string) {
	if raw, err := k.WorkCommittee.Get(ctx, jobId); err == nil && raw != "" {
		return
	}
	ordered, err := k.deriveCommitteeOrdered(ctx, jobId, CommitteeSize)
	if err != nil || len(ordered) == 0 {
		return
	}
	_ = k.WorkCommittee.Set(ctx, jobId, strings.Join(ordered, ","))
}
