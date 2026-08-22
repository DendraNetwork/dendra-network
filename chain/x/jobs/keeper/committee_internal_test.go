package keeper

import (
	"strconv"
	"testing"
)

// TestSelectCommitteeStakeWeightedGO04 — committee selection is STAKE-WEIGHTED.
// Properties checked against the pure core `selectCommittee` (no keeper involved).
func TestSelectCommitteeStakeWeightedGO04(t *testing.T) {
	miners := []minerWeight{
		{"whale", 8000},
		{"m1", 1000}, {"m2", 1000}, {"m3", 1000}, {"m4", 1000}, {"m5", 1000},
	}
	const size = 2
	const N = 3000

	counts := map[string]int{}
	for i := 0; i < N; i++ {
		seed := "beacon" + strconv.Itoa(i) + "|job"
		for id := range selectCommittee(seed, miners, size) {
			counts[id]++
		}
	}
	// (1) WEIGHTING: the whale (8x stake) is selected MUCH more often than a small miner.
	if counts["whale"] <= counts["m1"] {
		t.Fatalf("weighting broken: whale=%d <= m1=%d (stake carries no weight)", counts["whale"], counts["m1"])
	}
	// (1b) anti-centralization: a small miner is still selected sometimes (no total eviction).
	if counts["m1"] == 0 {
		t.Fatalf("a small miner is NEVER selected -> excessive centralization")
	}

	// (2) DETERMINISM: same seed -> same committee (consensus reproducible across validators).
	a := selectCommittee("X|job", miners, size)
	b := selectCommittee("X|job", miners, size)
	if len(a) != size || len(b) != size {
		t.Fatalf("wrong committee size: %d / %d (expected %d)", len(a), len(b), size)
	}
	for id := range a {
		if !b[id] {
			t.Fatalf("not deterministic: %v != %v", a, b)
		}
	}
}

// TestSelectCommitteeZeroStakeRelegatedGO04 — a miner with ZERO stake (fully slashed) must never evict
// miners carrying a positive bond; it only fills in when there is not enough bonded stake.
func TestSelectCommitteeZeroStakeRelegatedGO04(t *testing.T) {
	miners := []minerWeight{
		{"zero", 0},
		{"m1", 1000}, {"m2", 1000}, {"m3", 1000}, {"m4", 1000},
	}
	const size = 2
	for i := 0; i < 2000; i++ {
		seed := "s" + strconv.Itoa(i)
		if selectCommittee(seed, miners, size)["zero"] {
			t.Fatalf("seed %s: a stake=0 miner was selected despite 4 miners with a positive bond", seed)
		}
	}
	// But when size >= the number of positively bonded miners, the zero-stake miner fills in, so the
	// committee is never blocked.
	if !selectCommittee("s", miners, len(miners))["zero"] {
		t.Fatalf("with size = all miners, the zero-stake miner must fill the committee")
	}
}
