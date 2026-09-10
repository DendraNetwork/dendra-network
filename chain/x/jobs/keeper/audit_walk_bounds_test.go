package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// THE THREE AUDIT PASSES WALKED THE EXACT HEIGHT, WITH NO CAP. Both halves of that were defects, and
// they are each other's cure, so they are tested together here.
//
// EXACT HEIGHT: an entry filed at a height that is already past can never be collected again, because
// every later block asks for its own height and nothing else. `resolveDisputedAudit` reschedules with
// `h+retry`, genesis import files at `initH+BlocksRemaining`, and the deferral moves keys by a fixed
// stride — any of those landing on a height the pass has already run is an ORPHAN: a job whose
// deadline exists in the store and will never be honoured, so a held fee that no appointment can ever
// release. Widening the range to every DUE height (`K1 <= h`) drains them, oldest first.
//
// NO CAP: `auditDeferStride` makes every deferred batch of the same residue class land on the SAME
// height and MERGE, so a persistently undecentralized seed would grow the per-block batch without
// bound. The reveal pass carries the same shape and the same cap (`revealMaxPerBlock`); a deferral by
// a fixed stride always needs one, because it is what concentrates batches onto shared heights.
//
// Neither half works alone: capping without widening would strand what does not fit, widening without
// capping would grow the block. The two cases below fail if either half is reverted.

// AN ORPHANED DEADLINE MUST STILL BE HONOURED.
func TestAuditWalkDrainsAnOverdueDeadline(t *testing.T) {
	f := initFixture(t)
	const h int64 = 500

	// One deadline already past, one due exactly now. The jobs do not exist: `resolveDisputedAudit`
	// returns nil for an absent job, so what this case observes is the WALK, not the resolution.
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h-5, "jobEnRetard")))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h, "jobDuJour")))
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(h-7, "appelEnRetard")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	var restants []collections.Pair[int64, string]
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		restants = append(restants, key)
		return false, nil
	}))
	require.Empty(t, restants,
		"a deadline filed at a PAST height must be honoured, not orphaned: the walk covers every due height")

	var restantsAppel []collections.Pair[int64, string]
	require.NoError(t, f.keeper.PendingAppealResolve.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		restantsAppel = append(restantsAppel, key)
		return false, nil
	}))
	require.Empty(t, restantsAppel, "the appeal pass has the same shape and the same duty")
}

// AND THE BLOCK MUST NOT TAKE EVERYTHING AT ONCE.
//
// The cap is deliberately NOT retyped here: the test asserts the PROPERTY (some entries are left for
// the next block, and the queue does shrink), so a change to the constant does not make this case stop
// covering anything. Without the cap the queue empties in one block and `restants` is zero.
func TestAuditWalkPlafonneUnLotEnorme(t *testing.T) {
	f := initFixture(t)
	const h int64 = 900
	const n = 300 // comfortably above any sane per-block cap

	for i := 0; i < n; i++ {
		require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h, fmt.Sprintf("job%03d", i))))
	}

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	restants := 0
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(collections.Pair[int64, string]) (bool, error) {
		restants++
		return false, nil
	}))
	require.Greater(t, restants, 0, "a single block must not take an unbounded batch: some entries stay for the next one")
	require.Less(t, restants, n, "and it must make progress: the queue has to shrink every block")

	// The remainder drains on the following blocks rather than sitting there forever — the widened
	// range is what makes that true, since those entries now carry a height already past.
	for b := int64(1); b <= 3 && restants > 0; b++ {
		require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h+b)))
		restants = 0
		require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(collections.Pair[int64, string]) (bool, error) {
			restants++
			return false, nil
		}))
	}
	require.Equal(t, 0, restants, "what did not fit must be taken by the following blocks, oldest first")
}
