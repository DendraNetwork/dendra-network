package keeper_test

import (
	"fmt"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The two-thirds bar: the bar is the larger of effectiveSlashFloor and two thirds of the anchored
// seats, rounded up. Checked on every seat count from 0 to 40, so a rounding slip at one residue of 3
// cannot hide behind the seat counts other tests happen to use, and under a governed quorum above
// two thirds of a small jury, which decision 5 makes an ordinary jury size.
func TestADR059_TwoThirdsBarIsTheLargerOfTheFloorAndTwoThirdsOfTheSeats(t *testing.T) {
	for _, quorum := range []uint64{0, 4, 6, 9, 13} {
		p := types.DefaultParams()
		p.AuditMinQuorum = quorum
		floor := keeper.AuditSlashFloorForTest()
		if quorum > 0 {
			floor = int(quorum)
		}
		require.Equal(t, floor, keeper.EffectiveSlashFloorForTest(p),
			"audit_min_quorum=%d: the floor is the governed quorum, or the compiled floor at 0", quorum)
		for seats := 0; seats <= 40; seats++ {
			want := gateD6TwoThirds(seats)
			if floor > want {
				want = floor
			}
			require.Equal(t, want, keeper.AuditRelativeBarForTest(p, seats),
				"audit_min_quorum=%d, %d anchored seats: the bar is max(floor, ceil(2/3 x seats))", quorum, seats)
		}
	}
}

// Both outcomes read the same bar and each keeps its stake majority. Seats 8, 11 and 14 are the
// counts where two thirds rounds up by a full vote above the floor, the edge a truncating division misses.
func TestADR059_TwoThirdsBarDecidesBothOutcomesWhereTwoThirdsRoundsUp(t *testing.T) {
	for _, quorum := range []uint64{0, 4} {
		p := types.DefaultParams()
		p.AuditMinQuorum = quorum
		for _, seats := range []int{8, 11, 14} {
			t.Run(fmt.Sprintf("audit_min_quorum=%d/seats=%d", quorum, seats), func(t *testing.T) {
				bar := gateD6TwoThirds(seats)
				require.Greater(t, bar, keeper.EffectiveSlashFloorForTest(p), "premise: two thirds is above the floor here")

				require.False(t, keeper.AuditSlashDecisionForTest(p, true, seats, bar-1, seats),
					"one invalid vote short of two thirds must not slash")
				require.True(t, keeper.AuditSlashDecisionForTest(p, true, seats, bar, seats),
					"two thirds of invalid votes with the invalid stake majority slashes")
				require.False(t, keeper.AuditSlashDecisionForTest(p, false, seats, bar, seats),
					"two thirds of invalid votes without the invalid stake majority must not slash")

				require.False(t, keeper.AuditVindicateDecisionForTest(p, true, seats, bar-1, seats),
					"one valid vote short of two thirds must not vindicate")
				require.True(t, keeper.AuditVindicateDecisionForTest(p, true, seats, bar, seats),
					"two thirds of valid votes with the valid stake majority vindicates")
				require.False(t, keeper.AuditVindicateDecisionForTest(p, false, seats, bar, seats),
					"two thirds of valid votes without the valid stake majority must not vindicate")
			})
		}
	}
}
