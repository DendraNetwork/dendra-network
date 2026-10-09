package keeper

import (
	"strconv"
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A MULTIPLE OF 1 MAKES EVERY MINER BONDED AT `min_stake` OR ABOVE WEIGH THE SAME IN THE WORK DRAW.
//
// Fixture, not a reading of any chain: `min_stake` = 1 DNDR (1 000 000 udndr), one miner bonded at the
// floor -- what the kit stakes -- and one at 100 x the floor, the most a multiple of 100 lets count. When
// the stake comes from a free faucet, that hundredfold weight is bought with faucet grants, not with work.
//
// The draw below is the one `deriveCommitteeOrdered` runs, composed the same way:
// `selectCommitteeOrdered(seed, capAssignmentWeights(miners, p.MinStake, effectiveStakeCapMultiple(p)), n)`,
// and the primary is `members[0]`. The full keeper path (frozen pool, vitality, presence) is exercised by
// `TestEpoch14_GovernedStakeCapIsWhatTheWorkDrawApplies`; this test pins the arithmetic on the exact pair
// the governance proposal is about.
//
// The score is hash/stake and the SMALLEST wins, so a hundredfold weight does not buy a hundredfold share
// of primaries: with h uniform, P(h_big/100 < h_small) = 1 - 1/200. The outbid miner takes about 199
// primaries in 200, which is why the bounds are asserted on the draw and not only on the weights.
func TestStakeCapMultipleOne_EqualisesTheWorkDraw(t *testing.T) {
	const minStake = uint64(1_000_000)
	miners := []minerWeight{{id: "kit", stake: minStake}, {id: "outbid", stake: 100 * minStake}}

	params := func(multiple uint64) types.Params {
		p := types.DefaultParams()
		p.MinStake = minStake
		p.AssignmentStakeCapMultiple = multiple
		return p
	}
	weights := func(p types.Params) map[string]uint64 {
		out := map[string]uint64{}
		for _, m := range capAssignmentWeights(miners, p.MinStake, effectiveStakeCapMultiple(p)) {
			out[m.id] = m.stake
		}
		return out
	}
	const N = 4000
	primaries := func(p types.Params) int {
		capped := capAssignmentWeights(miners, p.MinStake, effectiveStakeCapMultiple(p))
		n := 0
		for i := 0; i < N; i++ {
			seed := "beacon" + strconv.Itoa(i) + "|job" + strconv.Itoa(i)
			if got := selectCommitteeOrdered(seed, capped, 1); len(got) == 1 && got[0] == "outbid" {
				n++
			}
		}
		return n
	}

	// (1) The value the proposal sets is accepted by the same Validate that MsgUpdateParams calls, and it
	// is read as itself: 1 is not 0, so it does not fall back to the compiled default.
	one, hundred := params(1), params(100)
	if err := one.Validate(); err != nil {
		t.Fatalf("Validate refuses assignment_stake_cap_multiple = 1: %v", err)
	}
	if err := hundred.Validate(); err != nil {
		t.Fatalf("Validate refuses assignment_stake_cap_multiple = 100: %v", err)
	}
	if got := effectiveStakeCapMultiple(one); got != 1 {
		t.Fatalf("effective multiple for a posted 1 is %d, want 1", got)
	}

	// (2) Before: the weights are 1 against 100. After: equal, both at the floor.
	if w := weights(hundred); w["kit"] != minStake || w["outbid"] != 100*minStake {
		t.Fatalf("multiple 100: weights %v, want kit=%d outbid=%d", w, minStake, 100*minStake)
	}
	if w := weights(one); w["kit"] != minStake || w["outbid"] != minStake {
		t.Fatalf("multiple 1: weights %v, want both at min_stake=%d", w, minStake)
	}

	// (3) The draw follows. Deterministic seeds, so the counts are exact; the bounds sit far from the
	// expected values on both sides (about 3980 and 2000 out of 4000).
	before, after := primaries(hundred), primaries(one)
	if before < N*98/100 {
		t.Fatalf("multiple 100: the outbid miner took %d/%d primaries, expected about 199 in 200", before, N)
	}
	if after < N*45/100 || after > N*55/100 {
		t.Fatalf("multiple 1: the outbid miner took %d/%d primaries, expected about half", after, N)
	}

	// (4) A proposal that LOSES the field does not equalise anything: absent is 0, and 0 selects the
	// compiled default of 2, a twofold weight (about 3 primaries in 4). Only a posted 1 removes the
	// stake weighting, which is why the proposal has to carry the field and every other one.
	zero := params(0)
	if got := effectiveStakeCapMultiple(zero); got != assignmentStakeCapMultiple || got == 1 {
		t.Fatalf("effective multiple for an absent field is %d, want the compiled default %d", got, assignmentStakeCapMultiple)
	}
	if w := weights(zero); w["outbid"] != 2*minStake {
		t.Fatalf("absent multiple: outbid weight %d, want twice the floor", w["outbid"])
	}
	dropped := primaries(zero)
	if dropped < N*70/100 || dropped > N*80/100 {
		t.Fatalf("absent multiple: the outbid miner took %d/%d primaries, expected about 3 in 4", dropped, N)
	}
	t.Logf("primaries won by the outbid miner out of %d draws: multiple 100 -> %d, multiple 1 -> %d, field absent (compiled 2) -> %d",
		N, before, after, dropped)
}
