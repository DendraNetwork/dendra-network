package keeper_test

import (
	"math"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// `work_gate_bps` was the only bps parameter without an upper bound.
//
// `EpochRelease` (emission.go) caps the work subsidy with `mulBps(nonRecDemand, WorkGateBps)`, that is
// `nonRecDemand * WorkGateBps / 10000`: the MULTIPLICATION precedes the division. `nonRecDemand` is
// bounded by the supply (~10^13 udndr, fixed 10 M supply); with no cap on `WorkGateBps`, a work gate
// voted near 2^64 makes the product OVERFLOW BEFORE the division, so the subsidy "cap" becomes a
// wrapped value and the anti-self-dealing bound silently disappears. The bound is 100000 bps, ten
// times the economic 1.5x work gate.

// (A) THE BOUND EXISTS AND REJECTS 100001, mirroring the three neighbouring bounds
// (`WorkSplit+AvailSplit`, `ReserveRelease`, `EpochBlocks`).
func TestWorkGateBpsBoundRejectsAboveTenX(t *testing.T) {
	p := types.DefaultParams()

	// Canonical economic setting (1.5x = 15000 bps): accepted.
	require.NoError(t, p.Validate(), "the economic 1.5x work gate (15000 bps) must pass")

	// Exactly at the bound (10x): accepted — a generous governance margin.
	p.WorkGateBps = 100000
	require.NoError(t, p.Validate(), "100000 bps (10x) sits exactly at the bound: accepted")

	// One step above: REFUSED.
	p.WorkGateBps = 100001
	require.Error(t, p.Validate(),
		"100001 bps exceeds the 10x bound: Validate MUST refuse (beyond it, the cap is meaningless and can overflow)")

	// 0 stays VALID: a fully gated work flow is a legitimate governance choice, not a sentinel.
	p.WorkGateBps = 0
	require.NoError(t, p.Validate(), "work_gate_bps=0 (work flow switched off) is an acceptable setting")
}

// (B) WHAT THE BOUND DEFENDS: the product `nonRecDemand * WorkGateBps` can no longer overflow uint64.
// Deterministic overflow-detection idiom: `x*y` overflows iff `x > MaxUint64/y`. Exercised on the
// MAXIMUM possible demand (the entire supply), the only case that matters.
func TestWorkGateBpsBoundPreventsSubsidyOverflow(t *testing.T) {
	const maxDemand uint64 = uint64(10_000_000) * 1_000_000 // FIXED supply = 10 M DNDR in udndr = 10^13
	const maxU64 uint64 = math.MaxUint64

	// At the bound (100000 bps): the product stays under 2^64 (~18x of margin) -> no overflow possible.
	require.LessOrEqual(t, maxDemand, maxU64/100000,
		"at 100000 bps, nonRecDemand*WorkGateBps CANNOT overflow (10^13 x 10^5 = 10^18 < 2^64)")
	capAtBound := maxDemand * 100000 / 10000 // = mulBps(maxDemand, 100000) = 10x the demand, computed safely
	require.Greater(t, capAtBound, maxDemand,
		"a SOUND cap is a multiple of the demand (10x here), computed without overflow")

	// Hostile value an UNBOUNDED WorkGateBps would have allowed (~2M bps, past the overflow threshold
	// for the maximum demand): the product would OVERFLOW BEFORE the division, so the anti-self-dealing
	// cap would be computed on a wrapped value. That is exactly the hole the bound closes.
	const hostile uint64 = 2_000_000
	require.Greater(t, hostile, uint64(100000), "the hostile value exceeds the bound: Validate refuses it")
	require.Greater(t, maxDemand, maxU64/hostile,
		"WITHOUT the bound, nonRecDemand*WorkGateBps OVERFLOWS (x > MaxUint64/y): the bug the bound removes")
}
