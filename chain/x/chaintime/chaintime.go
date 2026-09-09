// Package chaintime holds the single definition of the link between a block count and a duration.
//
// The chain counts time in BLOCKS, because height is the only deterministic clock two validators
// agree on. But almost every one of those numbers means a DURATION: `epoch_blocks = 31 536 000`
// means "one year", `audit_resolve_timeout` means "a few minutes", `miner_prune_blocks` means
// "weeks of inactivity". Left to itself, the conversion happens in the head of whoever writes the
// constant, with "about 1 s per block" restated in several places and in several wordings.
//
// That is the same failure shape as a threshold whose denominator changed underneath it: an
// absolute value, correct when written, whose MEANING depends on an outside quantity nothing
// watches. If the real inter-block interval moves to 2 s, none of these numbers change — and yet
// the emission epoch becomes two years, so "about 22 %/year" becomes about 22 % every two years,
// with no test, no guard and no parameter changing value. The drift is entirely silent, and it
// lands on the monetary curve.
//
// What this constant is: a TARGET, not a guarantee. CometBFT does not fix the block interval in
// consensus; it results from `timeout_commit` — NODE configuration, not chain configuration — plus
// real network propagation. Two consequences worth stating rather than assuming: no value expressed
// here can be enforced on-chain, and the real interval drifts with load and topology. Any block
// count that claims to be a duration is therefore an ESTIMATE. This package does not make it true;
// it makes it VISIBLE and changeable in one place.
package chaintime

// Duration units, in seconds. Written once so that block constants read as `An / TargetBlockSeconds`
// instead of as figures the reader has to divide mentally.
const (
	Minute uint64 = 60
	Heure  uint64 = 60 * Minute
	Jour   uint64 = 24 * Heure
	An     uint64 = 365 * Jour // Plain calendar year: 365 days, not 365.25. The 6-hour difference is
	// far below the uncertainty on the block interval itself, and a round value stays readable.
)

// TargetBlockSeconds is the block interval the network TARGETS, in seconds.
//
// Changing this value changes the MEANING of every derived constant, and therefore potentially the
// genesis app-hash (`x/emission` derives `EpochBlocks` from it). This is not a comfort setting: it
// is a consensus decision, to be taken together with a chain reset.
//
// THE DECLARED VALUE AND THE OBSERVED VALUE ARE NOT THE SAME NUMBER, AND THIS ONE IS THE DECLARED
// ONE. A measurement on the public endpoint — the timestamps of two block headers a known height
// apart, read from `/blockchain` — gave about FIVE seconds per block, five times the value below.
// Nothing is wrong with either number: this constant is a target the chain cannot enforce, and the
// paragraph above already said the real interval comes from node configuration. What was wrong was
// reading durations off this constant as if they were facts.
//
// So the rule for any duration derived from here: quote the BLOCK COUNT, which is exact, and attach
// the assumption to the seconds. A count of blocks is what the chain agrees on; a number of days or
// years is that count multiplied by an assumption, and the assumption belongs next to the result. At
// the observed interval every duration implied below is five times longer than it reads.
//
// The value is deliberately NOT changed to match the observation: it feeds `DefaultEpochBlocks` in
// `x/emission`, so editing it moves the genesis app-hash. Aligning it is a reset-time arbitration,
// not a documentation fix, and the interval it would be aligned to is itself free to drift again.
const TargetBlockSeconds uint64 = 1

// Blocs converts a duration in seconds into a block count at the target interval.
// The division is INTEGER: a duration that is not a multiple of the interval is truncated down.
// Truncating a window is preferable to overshooting it — a window that is too long delays a guard,
// one that is too short fires it early, and firing early is recoverable where firing late is not.
func Blocs(secondes uint64) uint64 { return secondes / TargetBlockSeconds }

// Secondes converts a block count into the targeted duration in seconds. It is the inverse of Blocs
// up to truncation: `Secondes(Blocs(d)) <= d`.
func Secondes(blocs uint64) uint64 { return blocs * TargetBlockSeconds }
