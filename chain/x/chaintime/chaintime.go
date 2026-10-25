// Package chaintime holds the single definition of the link between a block count and a duration.
//
// The chain counts time in blocks, because height is the only deterministic clock two validators
// agree on. But almost every one of those numbers means a duration: `epoch_blocks = 31 536 000`
// means "one year", `audit_resolve_timeout` means "a few minutes", `miner_prune_blocks` means
// "weeks of inactivity". Left alone, the conversion happens in the head of whoever writes the
// constant, with "about 1 s per block" restated in several places and wordings.
//
// That gives an absolute value, correct when written, whose meaning depends on an outside quantity
// nothing watches. If the real block interval moves to 2 s, none of these numbers change, yet the
// emission epoch becomes two years, so "about 22 %/year" becomes about 22 % every two years, with
// no test, guard or parameter changing value. The drift is silent and it lands on the monetary
// curve.
//
// TargetBlockSeconds is a target the chain cannot guarantee. CometBFT does not fix the block
// interval in consensus: it results from `timeout_commit`, which is node configuration, plus real
// network propagation. So no value expressed here can be enforced on-chain, and the real interval
// drifts with load and topology. Any block count that claims to be a duration is an estimate; this
// package only makes the estimate visible and changeable in one place.
package chaintime

// Duration units, in seconds. Written once so that block constants read as `Year / TargetBlockSeconds`
// instead of as figures the reader has to divide mentally.
const (
	Minute uint64 = 60
	Hour   uint64 = 60 * Minute
	Day    uint64 = 24 * Hour
	Year   uint64 = 365 * Day // Plain calendar year: 365 days, not 365.25. The 6-hour difference is
	// far below the uncertainty on the block interval itself, and a round value stays readable.
)

// TargetBlockSeconds is the block interval the network targets, in seconds.
//
// Changing this value changes the meaning of every derived constant, and potentially the genesis app
// hash (`x/emission` derives `EpochBlocks` from it). It is a consensus decision, to be taken together
// with a chain reset.
//
// This is the declared value, and the observed value differs. A measurement on the public endpoint
// (the timestamps of two block headers a known height apart, read from `/blockchain`) gave about five
// seconds per block, five times the value below. Neither number is wrong: this constant is a target
// the chain cannot enforce, and the real interval comes from node configuration. The mistake would be
// to read durations off this constant as facts.
//
// So for any duration derived from here, quote the block count, which is exact, and attach the
// assumption to the seconds: a number of days or years is the block count multiplied by an
// assumption. At the observed interval, every duration implied below is five times longer than it
// reads.
//
// The value is deliberately left unchanged: it feeds `DefaultEpochBlocks` in `x/emission`, so
// editing it moves the genesis app hash. Aligning it is a decision for a reset, and the interval it
// would be aligned to can drift again.
const TargetBlockSeconds uint64 = 1

// Blocks converts a duration in seconds into a block count at the target interval.
// The division is an integer one: a duration that is not a multiple of the interval is truncated.
// Truncating a window is preferable to overshooting it: a window that is too short fires its guard
// early, which is recoverable, while one that is too long fires it late, which is not.
func Blocks(seconds uint64) uint64 { return seconds / TargetBlockSeconds }

// Seconds converts a block count into the targeted duration in seconds. It is the inverse of Blocks
// up to truncation: `Seconds(Blocks(d)) <= d`.
func Seconds(blocks uint64) uint64 { return blocks * TargetBlockSeconds }
