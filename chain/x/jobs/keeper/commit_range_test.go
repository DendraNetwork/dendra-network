package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The commit walk is bounded to the current job.
//
// Eight handlers (settlement, payment, finalization, adjudication, semantic verification, anti-evasion)
// read commits. Walking the whole `Commit` collection and filtering in memory makes a settlement cost grow
// with the entire history of the chain, until gas exceeds the block limit and no job can settle.
//
// This test is about cost: the callers still apply a `strings.HasPrefix`, so a broken bound would still
// give the right answer while scanning everything. It counts the keys actually visited and compares them
// against an unbounded walk.
func TestCommitWalkIsBoundedToOneJob(t *testing.T) {
	f := initFixture(t)

	// "j10" is the trap: it starts with "j1" but its keys do not start with "j1__".
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

	// Only the keys of job j1 are visited; the others are never read from the store.
	require.ElementsMatch(t, []string{"j1__mA", "j1__mB", "j1__redo__mA"}, visited,
		"the walk must stop at the keys of the current job")
	require.NotContains(t, visited, "j10__mA",
		"prefix collision: j10 must not be confused with j1")

	// Contrast: this is what the unbounded form did. If this counter ever equalled the previous one, the
	// bound would bound nothing and the assertion above would still pass.
	all := 0
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, nil, func(string, types.Commit) (bool, error) {
		all++
		return false, nil
	}))
	require.Equal(t, len(seeded), all)
	require.Less(t, len(visited), all,
		"without a bound the walk reads the commits of other jobs: that is exactly the cost removed here")

	// A longer prefix (the __redo__ / __verdict__ variants) is bounded the same way: the same primitive serves
	// the eight callers, so it must not depend on the shape of the key.
	redo := []string{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("j1__redo__"),
		func(key string, _ types.Commit) (bool, error) {
			redo = append(redo, key)
			return false, nil
		}))
	require.Equal(t, []string{"j1__redo__mA"}, redo)
}
