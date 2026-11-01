package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// The governance trap: a parameter whose "stop" value produced the maximum.
//
// A guard of the form `if pp.ReserveReleaseBps != 0` before adopting the governed params treats 0 as
// "not configured". But 0 is what governance sets to suspend emission, and `Validate()` accepts it.
// The whole voted parameter set was then ignored and replaced by the hard-coded defaults (22 % of the
// Reserve released per epoch, with the voted splits and gate overwritten as well): the decision
// produced its opposite, with no error and no log line.
//
// Likewise, an unbounded `epoch_blocks` fails in two directions: at 0 the deadline falls on every
// block, and near 2^64 the sum `last + epochBlocks` overflows, the comparison inverts, and the
// deadline also falls on every block. "Never emit again" yielded maximal emission.

// (A) 0 must stay 0. This test is about validation: a zero value is an admissible setting, which is
// what made it dangerous while it also served as a sentinel.
func TestEmissionZeroReleaseIsAValidSettingNotASentinel(t *testing.T) {
	p := types.DefaultParams()
	p.ReserveReleaseBps = 0
	require.NoError(t, p.Validate(),
		"reserve_release_bps=0 is the normal way to suspend emission: Validate must accept it")
}

// (B) Both destructive values of `epoch_blocks` are rejected.
// Without these bounds, governance had two ways to get maximal emission while believing it was doing
// the opposite, and neither showed before the Reserve was drained.
func TestEmissionEpochBlocksBoundsRejectBothRunawayValues(t *testing.T) {
	p := types.DefaultParams()

	// 0 must stay valid: the default genesis (`GenesisState{}`, `config.yml`) sets no emission param,
	// so everything is zero there, and rejecting it would panic InitGenesis when a fresh chain starts.
	// The protection lives at runtime (epoch.go): 0 means emission disabled, never a fallback to 22 %.
	p.EpochBlocks = 0
	require.NoError(t, p.Validate(),
		"epoch_blocks=0 means \"not configured\": admissible at genesis, neutralized at runtime")

	p.EpochBlocks = 1 << 40 // "never": overflows last+epochBlocks -> the condition inverts
	require.Error(t, p.Validate(),
		"a huge epoch_blocks overflows the deadline computation and produces the inverse of the intent")

	p.EpochBlocks = 300 // the actual testnet setting
	require.NoError(t, p.Validate(), "a normal operating value must pass")
}

// (C) The deadline comparison must not overflow, whatever value the store holds.
// This reproduces the arithmetic of both forms: the additive one overflows, the subtractive one
// cannot. A test of reasonable values only would miss the one case that matters.
func TestEmissionEpochDeadlineArithmeticCannotOverflow(t *testing.T) {
	const h, last = uint64(1000), uint64(900)

	oldForm := func(epochBlocks uint64) bool { return h < last+epochBlocks } // true = "not time yet"
	newForm := func(epochBlocks uint64) bool { return h < last || h-last < epochBlocks }

	// Operating value: both forms agree (no regression).
	require.Equal(t, oldForm(300), newForm(300))
	require.True(t, newForm(300), "100 blocks elapsed out of 300 -> not time yet")

	// Hostile value: the additive form overflows and declares the deadline reached.
	const huge = ^uint64(0) - 100
	require.False(t, oldForm(huge), "the additive form overflows -> an epoch on every block")
	require.True(t, newForm(huge), "the subtractive form holds: the deadline is not reached")
}
