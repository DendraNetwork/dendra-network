package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// THE GOVERNANCE TRAP: a parameter whose "stop" value produced the maximum.
//
// A guard of the form `if pp.ReserveReleaseBps != 0` before adopting the governed params treats 0 as
// "not configured". But 0 is EXACTLY what governance sets to suspend emission, and `Validate()`
// accepts it. The whole voted parameter set was then ignored and replaced by the hard-coded defaults
// — 22 % of the Reserve released PER EPOCH, with the voted splits and gate overwritten along the way.
// The decision produced its own opposite, with no error and no log line.
//
// Symmetrically, an unbounded `epoch_blocks` fails in two directions: at 0 the deadline falls on every
// block, and near 2^64 the sum `last + epochBlocks` overflows, the comparison inverts, and the
// deadline falls on every block AS WELL. "Never emit again" yielded maximal emission.

// (A) 0 must stay 0. This test is about VALIDATION: a zero value is an admissible setting, and that
// is precisely what made it dangerous while it also served as a sentinel.
func TestEmissionZeroReleaseIsAValidSettingNotASentinel(t *testing.T) {
	p := types.DefaultParams()
	p.ReserveReleaseBps = 0
	require.NoError(t, p.Validate(),
		"reserve_release_bps=0 is the normal way to suspend emission: Validate must accept it")
}

// (B) BOTH DESTRUCTIVE VALUES OF `epoch_blocks` ARE REJECTED.
// Without these bounds, governance had two ways to obtain maximal emission while believing it was
// doing the opposite — and neither was detectable before the Reserve had been drained.
func TestEmissionEpochBlocksBoundsRejectBothRunawayValues(t *testing.T) {
	p := types.DefaultParams()

	// 0 must stay VALID: the default genesis (`GenesisState{}`, `config.yml`) sets no emission param,
	// so everything is zero there. Rejecting it would panic InitGenesis when a fresh chain starts —
	// trading a value leak for a chain that will not boot. The protection lives at RUNTIME (epoch.go):
	// 0 means emission disabled, never a fallback to 22 %.
	p.EpochBlocks = 0
	require.NoError(t, p.Validate(),
		"epoch_blocks=0 means \"not configured\": admissible at genesis, neutralized at runtime")

	p.EpochBlocks = 1 << 40 // "never": overflows last+epochBlocks -> the condition inverts
	require.Error(t, p.Validate(),
		"a huge epoch_blocks overflows the deadline computation and produces the INVERSE of the intent")

	p.EpochBlocks = 300 // the actual testnet setting
	require.NoError(t, p.Validate(), "a normal operating value must pass")
}

// (C) THE DEADLINE COMPARISON MUST NOT OVERFLOW, whatever value the store holds.
// This reproduces the arithmetic of both forms: the additive one overflows, the subtractive one
// cannot. A test exercising only reasonable values would be blind to the single case that matters.
func TestEmissionEpochDeadlineArithmeticCannotOverflow(t *testing.T) {
	const h, last = uint64(1000), uint64(900)

	ancien := func(epochBlocks uint64) bool { return h < last+epochBlocks } // true = "not time yet"
	nouveau := func(epochBlocks uint64) bool { return h < last || h-last < epochBlocks }

	// Operating value: both forms agree (no regression).
	require.Equal(t, ancien(300), nouveau(300))
	require.True(t, nouveau(300), "100 blocks elapsed out of 300 -> not time yet")

	// Hostile value: the additive form overflows and declares the deadline REACHED.
	const enorme = ^uint64(0) - 100
	require.False(t, ancien(enorme), "the additive form overflows -> an epoch on every block")
	require.True(t, nouveau(enorme), "the subtractive form holds: the deadline is NOT reached")
}
