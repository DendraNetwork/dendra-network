package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// WALK BOUNDED TO THE CURRENT JOB.
//
// Eight handlers (settlement, payment, finalization, adjudication, semantic verification,
// anti-evasion) used to walk the WHOLE `Commit` collection and filter in memory. The cost of a
// settlement therefore grew with the entire history of the chain, for transactions whose content did
// not change — until the day gas exceeds the block limit and no job settles at all.
//
// This test is about COST, not about the result: the callers still apply a `strings.HasPrefix`, so a
// broken bound would still produce the right answer while scanning everything. Hence it counts the
// keys actually VISITED and compares them against an unbounded walk.
func TestCommitWalkIsBoundedToOneJob(t *testing.T) {
	f := initFixture(t)

	// "j10" is the trap: it starts with "j1" but its keys do NOT start with "j1__".
	seeded := []string{"j1__mA", "j1__mB", "j1__redo__mA", "j10__mA", "j2__mA", "j2__mB"}
	for _, key := range seeded {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, key, types.Commit{JobId: key}))
	}

	visited := []string{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("j1__"),
		func(key string, _ types.Commit) (bool, error) {
			visited = append(visited, key)
			return false, nil
		}))

	// Only the keys of job j1 are VISITED — the others are never even read from the store.
	require.ElementsMatch(t, []string{"j1__mA", "j1__mB", "j1__redo__mA"}, visited,
		"the walk must stop at the keys of the current job")
	require.NotContains(t, visited, "j10__mA",
		"prefix collision: j10 must not be confused with j1")

	// PROOF BY CONTRAST — this is what the unbounded form did. If this counter ever equalled the
	// previous one, the bound would bound nothing and the assertion above would still pass.
	all := 0
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, nil, func(string, types.Commit) (bool, error) {
		all++
		return false, nil
	}))
	require.Equal(t, len(seeded), all)
	require.Less(t, len(visited), all,
		"without a bound the walk reads the commits of OTHER jobs: that is exactly the cost removed here")

	// A longer prefix (the __redo__ / __verdict__ variants) must be bounded the same way: the same
	// primitive serves the eight callers, so it must not depend on the shape of the key.
	redo := []string{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("j1__redo__"),
		func(key string, _ types.Commit) (bool, error) {
			redo = append(redo, key)
			return false, nil
		}))
	require.Equal(t, []string{"j1__redo__mA"}, redo)
}
