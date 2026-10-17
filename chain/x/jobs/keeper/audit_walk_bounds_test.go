package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// The three audit passes must neither walk the exact height nor run without a cap. Each half is a
// defect, and each is the other's cure, so they are tested together.
//
// Exact height: an entry filed at a height already past is never collected, because every later block
// asks for its own height only. `resolveDisputedAudit` reschedules at `h+retry`, genesis import files at
// `initH+BlocksRemaining`, and the deferral moves keys by a fixed stride; any of those landing on a
// height the pass has already run leaves an orphan, a deadline that exists in the store and is never
// honoured, so a held fee nothing can release. Walking every due height (`K1 <= h`) drains them, oldest
// first.
//
// No cap: `auditDeferStride` makes every deferred batch of the same residue class land on the same
// height and merge, so a persistently undecentralized seed would grow the per-block batch without bound.
// The reveal pass has the same shape and the same cap (`revealMaxPerBlock`); a deferral by a fixed
// stride always needs one, because it concentrates batches onto shared heights.
//
// Neither half works alone: capping without widening would strand what does not fit, and widening
// without capping would grow the block. The two cases below fail if either half is reverted.

// An orphaned deadline is still honoured.
func TestAuditWalkDrainsAnOverdueDeadline(t *testing.T) {
	f := initFixture(t)
	const h int64 = 500

	// One deadline already past, one due exactly now. The jobs do not exist: `resolveDisputedAudit` returns
	// nil for an absent job, so this case observes the walk and leaves the resolution aside.
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h-5, "jobLate")))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h, "jobDueNow")))
	require.NoError(t, f.keeper.PendingAppealResolve.Set(f.ctx, collections.Join(h-7, "appealLate")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	var remaining []collections.Pair[int64, string]
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		remaining = append(remaining, key)
		return false, nil
	}))
	require.Empty(t, remaining,
		"a deadline filed at a past height must be honoured, not orphaned: the walk covers every due height")

	var remainingAppeal []collections.Pair[int64, string]
	require.NoError(t, f.keeper.PendingAppealResolve.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		remainingAppeal = append(remainingAppeal, key)
		return false, nil
	}))
	require.Empty(t, remainingAppeal, "the appeal pass has the same shape and the same duty")
}

// And one block does not take everything at once.
//
// The cap is not retyped here: the test asserts the property (some entries are left for the next block,
// and the queue shrinks), so a change to the constant does not stop this case from covering anything.
// Without the cap the queue empties in one block and `remaining` is zero.
func TestAuditWalkCapsAHugeBatch(t *testing.T) {
	f := initFixture(t)
	const h int64 = 900
	const n = 300 // comfortably above any sane per-block cap

	for i := 0; i < n; i++ {
		require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(h, fmt.Sprintf("job%03d", i))))
	}

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	remaining := 0
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(collections.Pair[int64, string]) (bool, error) {
		remaining++
		return false, nil
	}))
	require.Greater(t, remaining, 0, "a single block must not take an unbounded batch: some entries stay for the next one")
	require.Less(t, remaining, n, "and it must make progress: the queue has to shrink every block")

	// The remainder drains on the following blocks instead of staying forever; the widened range makes that
	// true, since those entries now carry a height already past.
	for b := int64(1); b <= 3 && remaining > 0; b++ {
		require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h+b)))
		remaining = 0
		require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(collections.Pair[int64, string]) (bool, error) {
			remaining++
			return false, nil
		}))
	}
	require.Equal(t, 0, remaining, "what did not fit must be taken by the following blocks, oldest first")
}
