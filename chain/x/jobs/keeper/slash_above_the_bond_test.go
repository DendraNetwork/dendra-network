package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A slash above 100 % of a bond would underflow a uint64, so validation must refuse it.
//
// `Params.Validate` bounds the bps parameters from above. With only a floor on `slash_leak_bps` (2500, in
// optimistic mode) and no bound at all on `fee_burn_bps`, `Validate` would return
// nil at 20000 for both, a permissionless `VerifySemantic` would then succeed, and the punished
// miner's bond would go from 1000 to 18446744073709550616, with 2000 nonexistent coins credited to the Treasury.
//
// This is more than a display error. The audit jury draw reads the raw stake (the assignment cap
// applies to the work draw, not to that one), so the slashed cheater would take every seat offered,
// and `payAvailability` sums the same field to split the epoch budget. The punishment would turn the
// guilty party into the network's largest holder.
//
// A single percent-for-bps slip posts it: 80 % is 8000, and 80000 is what a percent-shaped habit
// writes. Four of the seven slash sites are permissionless, so once the parameter is in, anyone
// triggers it for the price of gas.
//
// The fix bounds two fields, not the four that have no direct upper test, as measured field by field:
// `validator_reward_bps` is bounded by the sum check `validator+team > 10000` (a search for `> 10000`
// on the field alone misses it), and `work_gate_bps` is above 10000 on purpose: the live chain serves
// 15000, because it scales a subsidy against demand instead of splitting a total, and bounding it for
// consistency would have refused the running configuration. A share of X cannot exceed X; not every
// bps is a share.
//
// Where the invariant lives: the seven slash sites still subtract without clamping. Only
// `antievasion.go` clamps, under a comment saying it is "unreachable for bps <= 10000; defensive", an
// assumption validation now enforces. The second case below writes the out-of-range parameter straight
// into the store, which no message can do any more, and shows what the handlers alone still do, so
// whoever relaxes the bound will find it.

func sabParams(slash, burn uint64) types.Params {
	p := types.DefaultParams()
	p.SlashLeakBps = slash
	p.FeeBurnBps = burn
	return p
}

// (a) At validation: a slash or a burn calibrated above 100 % is no longer a set governance can post.
func TestASlashAboveTheBondIsRefusedByValidation(t *testing.T) {
	d := types.DefaultParams()

	err := sabParams(20000, d.FeeBurnBps).Validate()
	require.Error(t, err, "a slash cannot exceed the bond it takes from")
	require.Contains(t, err.Error(), "slash_leak_bps")

	err = sabParams(d.SlashLeakBps, 20000).Validate()
	require.Error(t, err, "a burn cannot exceed the fee it is taken from")
	require.Contains(t, err.Error(), "fee_burn_bps")

	// The boundary itself stays legal: 10000 is "the whole bond", a governance choice and not an invalid
	// one. Bounding at 9999 would refuse a total slash that the arithmetic handles exactly.
	require.NoError(t, sabParams(10000, 10000).Validate(), "exactly 100 %% is a choice, not an overflow")
	require.NoError(t, d.Validate(), "and the shipped set is untouched")

	// The fields that legitimately exceed 10000 are not caught. `work_gate_bps` is 15000 on the live
	// chain, and a rule applied by shape rather than by meaning would have refused production.
	large := types.DefaultParams()
	large.WorkGateBps = 15000
	require.NoError(t, large.Validate(),
		"work_gate_bps scales a subsidy against demand; it is not a share of a total")
}

// (b) What the handlers alone still do, with the out-of-range parameter written straight into the
// store, a state no message can now produce. The subtraction is unguarded at this site, so the whole
// protection is the clause in (a).
func TestTheHandlersThemselvesStillUnderflowIfTheBoundIsBypassed(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, sabParams(20000, types.DefaultParams().FeeBurnBps)))
	srv := keeper.NewMsgServerImpl(f.keeper)
	vsJob(t, f, "jSAB", map[string]string{"sa": "1,2,3", "sb": "1,2,3", "sc": "-1,-2,-3"})

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
		Creator: ssAddr20(t, f, "anyone-sab"), JobId: "jSAB",
	})
	require.NoError(t, err, "the handler does not re-check the parameter: it applies it")

	m, err := f.keeper.Miner.Get(f.ctx, "sc")
	require.NoError(t, err)
	require.Greater(t, m.Stake, uint64(1000),
		"the bond wrapped instead of emptying: the punished miner ends up richer than it started")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(2000), pools.Treasury,
		"and the Treasury is credited coins the bond never held")
}

// (c) The control, at a legal calibration on the same fixture: the slash lands exactly, and a slash of
// the whole bond empties it to zero instead of wrapping. Without this half, (a) could not be told apart
// from a validation that refuses everything.
func TestALegalSlashEmptiesTheBondWithoutWrapping(t *testing.T) {
	for name, bps := range map[string]uint64{"80 %": 8000, "100 %": 10000} {
		t.Run(name, func(t *testing.T) {
			f := initFixture(t)
			p := sabParams(bps, types.DefaultParams().FeeBurnBps)
			require.NoError(t, p.Validate())
			require.NoError(t, f.keeper.Params.Set(f.ctx, p))
			srv := keeper.NewMsgServerImpl(f.keeper)
			vsJob(t, f, "jOk"+name, map[string]string{"oa": "1,2,3", "ob": "1,2,3", "oc": "-1,-2,-3"})

			_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
				Creator: ssAddr20(t, f, "anyone-ok"), JobId: "jOk" + name,
			})
			require.NoError(t, err)

			m, err := f.keeper.Miner.Get(f.ctx, "oc")
			require.NoError(t, err)
			require.Equal(t, uint64(1000)-uint64(1000)*bps/10000, m.Stake,
				"the bond decreases by exactly the calibrated share, and never below zero")
		})
	}
}
