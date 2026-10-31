package keeper_test

import (
	"fmt"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The two-thirds bar at the audit deadline (resolveDisputedAudit). With a jury of declared judges the
// jury is often smaller than the draw size, so the bar is checked on the seats it actually anchors.

// At 8 and 11 anchored seats two thirds is 6 and 8: one vote fewer in either direction defers, and
// exactly two thirds concludes, so the caller must pass the count of anchored seats.
func TestADR059_TwoThirdsBarRoundsUpAtTheTimeout(t *testing.T) {
	p := types.DefaultParams()
	for _, seats := range []int{8, 11} {
		bar := gateD6TwoThirds(seats)
		require.Greater(t, bar, keeper.EffectiveSlashFloorForTest(p), "premise: two thirds is above the floor")

		t.Run(fmt.Sprintf("seats=%d/invalid=%d defers", seats, bar-1), func(t *testing.T) {
			f, op := gateD6AtTheTimeout(t, "jD6r", p, seats, gateD6Votes(bar-1, false, gateD6JurorStake))
			gateD6RequireDeferred(t, f, op, "jD6r", "one invalid vote short of two thirds of the anchored seats")
		})
		t.Run(fmt.Sprintf("seats=%d/invalid=%d slashes", seats, bar), func(t *testing.T) {
			f, _ := gateD6AtTheTimeout(t, "jD6r", p, seats, gateD6Votes(bar, false, gateD6JurorStake))
			gateD6RequireSlashed(t, f, p, "jD6r", "two thirds of the anchored seats invalid")
		})
		t.Run(fmt.Sprintf("seats=%d/valid=%d defers", seats, bar-1), func(t *testing.T) {
			f, op := gateD6AtTheTimeout(t, "jD6r", p, seats, gateD6Votes(bar-1, true, gateD6JurorStake))
			gateD6RequireDeferred(t, f, op, "jD6r", "one valid vote short of two thirds of the anchored seats")
		})
		t.Run(fmt.Sprintf("seats=%d/valid=%d vindicates", seats, bar), func(t *testing.T) {
			f, op := gateD6AtTheTimeout(t, "jD6r", p, seats, gateD6Votes(bar, true, gateD6JurorStake))
			gateD6RequireVindicated(t, f, op, "jD6r", "two thirds of the anchored seats valid")
		})
	}
}

// A governed audit_min_quorum is the bar when it is above two thirds of the jury: 6 for a 7-seat
// jury, where two thirds is 5. Five votes in one direction, with six voters, must defer.
func TestADR059_TwoThirdsBarHonoursAGovernedQuorumAboveTwoThirds(t *testing.T) {
	p := types.DefaultParams()
	p.AuditMinQuorum = 6
	const seats = 7
	require.Less(t, gateD6TwoThirds(seats), 6, "premise: the governed quorum is above two thirds of the jury")

	t.Run("5 invalid and 1 valid defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(5, false, gateD6JurorStake), gateD6Votes(1, true, gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6q", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6q", "5 invalid votes are below a governed quorum of 6")
	})
	t.Run("6 invalid slashes", func(t *testing.T) {
		f, _ := gateD6AtTheTimeout(t, "jD6q", p, seats, gateD6Votes(6, false, gateD6JurorStake))
		gateD6RequireSlashed(t, f, p, "jD6q", "6 invalid votes reach the governed quorum")
	})
	t.Run("5 valid and 1 invalid defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(5, true, gateD6JurorStake), gateD6Votes(1, false, gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6q", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6q", "5 valid votes are below a governed quorum of 6")
	})
	t.Run("6 valid vindicates", func(t *testing.T) {
		f, op := gateD6AtTheTimeout(t, "jD6q", p, seats, gateD6Votes(6, true, gateD6JurorStake))
		gateD6RequireVindicated(t, f, op, "jD6q", "6 valid votes reach the governed quorum")
	})
}

// The seat count and the stake majority are two locks: two thirds of the seats voting one way, with
// the stake majority the other way, concludes nothing in either direction.
func TestADR059_TwoThirdsBarKeepsTheStakeMajorityOfBothOutcomes(t *testing.T) {
	p := types.DefaultParams()
	const seats = 15
	bar := gateD6TwoThirds(seats)

	t.Run("two thirds valid, invalid stake majority, defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, true, gateD6JurorStake), gateD6Votes(seats-bar, false, 3*gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6s", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6s", "a vindication needs the valid stake majority")
	})
	t.Run("two thirds invalid, valid stake majority, defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, false, gateD6JurorStake), gateD6Votes(seats-bar, true, 3*gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6s", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6s", "a slash needs the invalid stake majority")
	})
}

// An exact stake tie is not a majority: with two thirds of the seats on one side and half the stake on
// each, neither the slash nor the vindication fires and the audit defers.
func TestADR059_TwoThirdsBarAnExactStakeTieDefers(t *testing.T) {
	p := types.DefaultParams()
	const seats = 15
	bar := gateD6TwoThirds(seats)
	require.Equal(t, uint64(bar)*gateD6JurorStake, uint64(seats-bar)*2*gateD6JurorStake, "premise: the two sides carry equal stake")

	t.Run("two thirds invalid at half the stake defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, false, gateD6JurorStake), gateD6Votes(seats-bar, true, 2*gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6t", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6t", "half the stake invalid is not an invalid majority")
	})
	t.Run("two thirds valid at half the stake defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, true, gateD6JurorStake), gateD6Votes(seats-bar, false, 2*gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6t", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6t", "half the stake valid is not a valid majority")
	})
}

// A vindication counts the valid votes alone: 6 valid and 4 invalid out of 15 seats is 10 voters and
// 6 valid votes, below two thirds, even with the valid stake majority.
func TestADR059_TwoThirdsBarCountsOnlyValidVotesForAVindication(t *testing.T) {
	p := types.DefaultParams()
	const seats = 15
	bar := gateD6TwoThirds(seats)

	t.Run("6 valid and 4 invalid defers", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(6, true, 10*gateD6JurorStake), gateD6Votes(bar-6, false, gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6v", p, seats, votes)
		gateD6RequireDeferred(t, f, op, "jD6v", "10 voters with 6 valid votes do not vindicate")
	})
	t.Run("two thirds valid and 4 invalid vindicates", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, true, 10*gateD6JurorStake), gateD6Votes(4, false, gateD6JurorStake))
		f, op := gateD6AtTheTimeout(t, "jD6v", p, seats, votes)
		gateD6RequireVindicated(t, f, op, "jD6v", "two thirds of the seats valid, with the stake majority, vindicate")
	})
}

// The seats are the anchored seats still registered. A juror that left the registry can no longer
// vote, so it leaves the denominator: with 6 of 15 seats gone, two thirds of the 9 left is 6.
func TestADR059_TwoThirdsBarCountsTheAnchoredSeatsStillRegistered(t *testing.T) {
	p := types.DefaultParams()
	const anchored, gone = 15, 6
	bar := gateD6TwoThirds(anchored - gone)
	require.Less(t, bar, gateD6TwoThirds(anchored), "premise: the departures lower two thirds")

	f, _ := gateD6AtTheTimeoutAfter(t, "jD6g", p, anchored, gateD6Votes(bar, false, gateD6JurorStake),
		func(f *fixture, seatIds []string) {
			for _, id := range seatIds[anchored-gone:] {
				require.NoError(t, f.keeper.Miner.Remove(f.ctx, id))
			}
		})
	gateD6RequireSlashed(t, f, p, "jD6g", "two thirds of the seats still registered invalid")
}
