package keeper_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// availLastPresent reads the date the draws read for presence, on the state of `ctx`.
func availLastPresent(t *testing.T, f *fixture, ctx context.Context, mid string) uint64 {
	t.Helper()
	last, err := f.keeper.MinerLastPresentEpoch.Get(ctx, mid)
	require.NoError(t, err)
	return last
}

// A proof in a boundary block dates the miner (`MinerLastPresentEpoch`, which the draws read) with the
// epoch the block closes, the one its EndBlock pays. A date one epoch ahead would count the miner
// present for a window it has not proven. Read on both states of a boundary height, and at an upgrade
// height on a boundary, where no roll height is stored.
func TestAvailabilityProofInABoundaryBlockDatesTheEpochItCloses(t *testing.T) {
	t.Run("a boundary after a roll", func(t *testing.T) {
		f := initFixture(t)
		op, srv, _ := availSetup(t, f, 0, "mDate")
		require.NoError(t, f.keeper.EndBlock(availBlock(f, 100, "epoch-one")))
		one, err := f.keeper.AvailChallenge.Get(f.ctx)
		require.NoError(t, err)

		_, err = srv.ProveAvailability(availBlock(f, 200, "epoch-two"), &types.MsgProveAvailability{Creator: op, MinerId: "mDate", Challenge: one})
		require.NoError(t, err)
		require.Equal(t, uint64(1), availLastPresent(t, f, f.ctx, "mDate"), "inside block 200: the epoch it closes")

		require.NoError(t, f.keeper.EndBlock(availBlock(f, 200, "epoch-two")))
		two, err := f.keeper.AvailChallenge.Get(f.ctx)
		require.NoError(t, err)
		sim, _ := availBlock(f, 200, "epoch-two").CacheContext()
		_, err = srv.ProveAvailability(sim, &types.MsgProveAvailability{Creator: op, MinerId: "mDate", Challenge: two})
		require.NoError(t, err)
		require.Equal(t, uint64(2), availLastPresent(t, f, sim, "mDate"), "on the committed block 200: the epoch of the new challenge")
		require.Equal(t, uint64(1), availLastPresent(t, f, f.ctx, "mDate"), "the simulation writes nothing")
	})

	t.Run("the upgrade height on a boundary", func(t *testing.T) {
		f := initFixture(t)
		op, srv, _ := availSetup(t, f, 0, "mDateUp")
		require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "rolled-by-epoch-14"))
		_, err := srv.ProveAvailability(availBlock(f, 300, "upgrade-block"), &types.MsgProveAvailability{Creator: op, MinerId: "mDateUp", Challenge: "rolled-by-epoch-14"})
		require.NoError(t, err)
		require.Equal(t, uint64(2), availLastPresent(t, f, f.ctx, "mDateUp"), "the upgrade block 300 closes epoch 2")
	})
}

// The halt before the upgrade height U when U-1 is a boundary (availability.go::availChallengeEpoch):
// the epoch 14 binary rolled the challenge at U-1 and stored no roll height. On that committed state the
// query and CheckTx count the challenge one epoch lower, and they agree. The miner daemon compares the
// reported epoch with the height (services/miner.py, avail_window), reads the answer as
// stale and asks again; a query that alone reported U-1's own epoch would send it into a CheckTx that
// refuses the proof as too late. Here U-1 = 200 with windows of 100 blocks and a deadline of 2.
func TestAvailabilityHaltAfterABoundaryQueryAgreesWithCheckTx(t *testing.T) {
	f := initFixture(t)
	op, srv, qs := availSetup(t, f, 2, "mHalt")
	require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "rolled-at-200-by-epoch-14"))

	halt := availBlock(f, 200, "rolled-at-200")
	q, err := qs.GetAvailChallenge(halt, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, "rolled-at-200-by-epoch-14", q.Challenge)
	require.Equal(t, int64(1), q.Epoch, "no roll height at a boundary: one epoch lower")
	require.NotEqual(t, halt.BlockHeight()/int64(q.AvailEpochBlocks), q.Epoch, "the daemon reads this answer as stale")
	check, _ := halt.CacheContext()
	_, err = srv.ProveAvailability(check, &types.MsgProveAvailability{Creator: op, MinerId: "mHalt", Challenge: q.Challenge})
	require.ErrorContains(t, err, "too late", "CheckTx counts the epoch the query reports, whose deadline has passed")

	upgrade := availBlock(f, 201, "upgrade-block")
	q, err = qs.GetAvailChallenge(upgrade, &types.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, int64(2), q.Epoch, "from block U on, both read the epoch of the challenge")
	_, err = srv.ProveAvailability(upgrade, &types.MsgProveAvailability{Creator: op, MinerId: "mHalt", Challenge: q.Challenge})
	require.NoError(t, err)
	require.True(t, availPresent(t, f, upgrade, 2, "mHalt"))
	require.Equal(t, uint64(2), availLastPresent(t, f, f.ctx, "mHalt"))
}
