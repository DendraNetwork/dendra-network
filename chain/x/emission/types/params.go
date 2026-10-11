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

// DefaultEpochBlocks is the Reserve release period: one year at the target block interval.
//
// This number must exist exactly once. Written both here and in `keeper.EpochBlocks` with no link
// between the two, `31_536_000` could change in one place without the other. It is derived from
// `x/chaintime`: a block count that means "one year" says so in the way it is
// written. The numeric value stays 365 x 86 400 / 1, so consensus is not affected.
const DefaultEpochBlocks uint64 = chaintime.Year / chaintime.TargetBlockSeconds

// DefaultParams returns the default set of parameters (ADR-021). reserve_release_bps=2200 (22 %) is
// applied once per epoch of DefaultEpochBlocks blocks, on a decreasing curve. That block count is
// about a year only under the block interval chaintime declares as a target; consensus does not
// guarantee it, and the live network has measured about 5x that interval, which makes the same epoch
// about five years. State this rate per epoch, the unit the chain counts, and name the assumed
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
	// Upper bound on `work_gate_bps`. In `EpochRelease` (emission.go),
	// `mulBps(nonRecDemand, WorkGateBps)` multiplies before dividing: a demand close to the supply
	// (about 10^13 udndr) times a work gate close to 2^64 overflows uint64 and yields an absurd work
	// subsidy cap. The cap is 100000 bps (10x). The bound is economic first: the work gate sits at 1.5x
	// (15000 bps), and beyond 10x a work-flow multiplier no longer makes anti-bubble sense; 10^13 x 10^5
	// = 10^18 still leaves about 18x of headroom under 2^64. `0` stays allowed: it means the work flow
	// is fully gated, a legitimate governance choice.
	if p.WorkGateBps > 100000 {
		return fmt.Errorf("work_gate_bps > 100000 (10x the 1.5x economic work gate; beyond that the multiplier is anti-bubble nonsense and risks overflowing the work subsidy)")
	}
	// Upper bound on `epoch_blocks`. Close to 2^64, `last + epochBlocks` overflowed, the comparison
	// inverted, and the epoch fired at every block: a proposal meaning "never emit again" produced
	// maximal emission instead. The cap is 2^32 blocks (about 136 years at one second per block); a
	// larger value is a typo.
	//
	// `0` is deliberately not refused here. The default genesis (`config.yml`, `GenesisState{}`) sets no
	// emission parameter at all, so everything is zero there, and refusing 0 at validation would panic
	// `InitGenesis` when a fresh chain starts. `0` means "not configured" and is treated at runtime as
	// emission disabled (see epoch.go), never as a fallback to the defaults.
	if p.EpochBlocks > 1<<32 {
		return fmt.Errorf("epoch_blocks > 2^32 (overflows the deadline computation and inverts the condition -> emission at every block)")
	}
	return nil
}
