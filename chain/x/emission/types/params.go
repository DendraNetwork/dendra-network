package types

import (
	"fmt"

	"github.com/DendraNetwork/dendra-network/chain/x/chaintime"
)

// NewParams creates a new Params instance.
func NewParams(reserveReleaseBps, workSplitBps, availSplitBps, workGateBps, epochBlocks uint64) Params {
	return Params{
		ReserveReleaseBps: reserveReleaseBps,
		WorkSplitBps:      workSplitBps,
		AvailSplitBps:     availSplitBps,
		WorkGateBps:       workGateBps,
		EpochBlocks:       epochBlocks,
	}
}

// DefaultEpochBlocks — the Reserve release period: ONE YEAR at the target block interval.
//
// This number must exist exactly once. `31_536_000` used to live both here and in
// `keeper.EpochBlocks`, with no link between the two: two sources of truth for the denominator of the
// monetary curve, either of which could be adjusted without the other. There is one now, and it is
// DERIVED from `x/chaintime` — a block count that means "one year" must say so in the way it is
// written, not in a comment beside it. The numeric value is unchanged (365 × 86 400 / 1), so it has no
// effect on consensus.
const DefaultEpochBlocks uint64 = chaintime.An / chaintime.TargetBlockSeconds

// DefaultParams returns the default set of parameters (ADR-021). reserve_release_bps=2200 (22 %) is
// applied ONCE per epoch of DefaultEpochBlocks blocks, and the curve decreases. That block count is
// "about a year" ONLY under the block interval chaintime declares as a target; the consensus does not
// guarantee it, and the live network has measured ~5x that interval, which makes the same epoch about
// five years. State this rate per EPOCH — the unit the chain actually counts — and name the assumed
// interval whenever a duration is converted to wall-clock time. An observable devnet overrides both
// through the genesis (emission.params) with a short epoch_blocks and a small reserve_release_bps.
func DefaultParams() Params {
	return NewParams(2200, 5000, 2000, 15000, DefaultEpochBlocks)
}

// Validate validates the set of params.
func (p Params) Validate() error {
	if p.WorkSplitBps+p.AvailSplitBps > 10000 {
		return fmt.Errorf("work_split_bps + avail_split_bps > 10000 (security receives the remainder)")
	}
	if p.ReserveReleaseBps > 10000 {
		return fmt.Errorf("reserve_release_bps > 10000")
	}
	// UPPER BOUND ON `work_gate_bps`. In `EpochRelease` (emission.go),
	// `mulBps(nonRecDemand, WorkGateBps)` MULTIPLIES before dividing: a demand close to the supply
	// (~10^13 udndr) times a work_gate close to 2^64 OVERFLOWS the uint64 and yields an absurd work
	// subsidy cap. The cap is 100000 bps (10x). The bound is ECONOMIC before it is anti-overflow: the
	// work gate sits at 1.5x (15000 bps); beyond 10x, a work-flow multiplier no longer makes any
	// anti-bubble sense, and 10^13 x 10^5 = 10^18 still leaves ~18x of headroom under 2^64. `0` stays
	// ALLOWED: it means the work flow is fully gated, a legitimate governance choice.
	if p.WorkGateBps > 100000 {
		return fmt.Errorf("work_gate_bps > 100000 (10x the 1.5x economic work gate; beyond that the multiplier is anti-bubble nonsense and risks overflowing the work subsidy)")
	}
	// UPPER BOUND ON `epoch_blocks`. Close to 2^64, `last + epochBlocks` overflowed, the comparison
	// inverted, and the epoch fired at EVERY block — a proposal meaning "never emit again" produced
	// maximal emission instead. The cap is 2^32 blocks (~136 years at one second per block): beyond
	// that it is not an intention, it is a typo.
	//
	// `0` IS NOT REFUSED HERE, deliberately. The default genesis (`config.yml`, `GenesisState{}`) sets
	// NO emission parameter at all: everything is zero there. Refusing 0 at validation would panic
	// `InitGenesis` when a fresh chain starts, replacing a value leak with a chain that does not boot.
	// `0` means "not configured" and is treated at RUNTIME as emission disabled (see epoch.go) — never
	// as a fallback to the defaults.
	if p.EpochBlocks > 1<<32 {
		return fmt.Errorf("epoch_blocks > 2^32 (overflows the deadline computation and inverts the condition -> emission at every block)")
	}
	return nil
}
