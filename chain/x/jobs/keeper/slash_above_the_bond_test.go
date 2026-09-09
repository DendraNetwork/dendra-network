package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A SLASH ABOVE 100 % OF A BOND UNDERFLOWED A uint64, AND VALIDATION LET IT THROUGH.
//
// `Params.Validate` bounds nine bps parameters from above. `slash_leak_bps` carried a FLOOR only
// (2500, in optimistic mode) and `fee_burn_bps` had no bound at all. MEASURED before the fix, not
// reasoned about: `Validate` returned nil at 20000 for both; a permissionless `VerifySemantic` then
// succeeded; and the punished miner's bond went from 1000 to 18446744073709550616 while the Treasury
// was credited 2000 coins that do not exist.
//
// The consequence is not a display error. The audit jury draw reads the RAW stake — the assignment cap
// applies to the WORK draw, not to that one — so the slashed cheater would take every seat it is
// offered, and `payAvailability` sums the same field to split the epoch budget. The punishment turns
// the guilty party into the network's largest holder.
//
// A single percent-for-bps slip posts it: 80 meaning 80 % is 8000, and 80000 is what a percent-shaped
// habit writes. Four of the seven slash sites are permissionless, so once the parameter is in, anyone
// triggers it for the price of gas.
//
// ⚠️ WHY THE FIX BOUNDS TWO FIELDS AND NOT THE FOUR THAT CARRY NO DIRECT UPPER TEST. Measured field by
// field: `validator_reward_bps` IS bounded, by the SUM check `validator+team > 10000` — a search for
// `> 10000` on the field alone misses it. And `work_gate_bps` is deliberately ABOVE 10000: the live
// chain serves 15000, because it scales a subsidy against demand instead of splitting a total.
// Bounding it "for consistency" would have refused the running configuration. The rule is not "every
// bps is a share"; it is "a share of X cannot exceed X".
//
// ⛔ WHERE THE INVARIANT LIVES. The seven slash sites still subtract without clamping — only
// `antievasion.go` clamps, under a comment saying it is "unreachable for bps <= 10000; defensive", an
// assumption validation now actually enforces. The second case below writes the out-of-range parameter
// straight into the store, which no message can do any more, and shows what the handlers alone still
// do. Whoever relaxes the bound will find it waiting.

func sabParams(slash, burn uint64) types.Params {
	p := types.DefaultParams()
	p.SlashLeakBps = slash
	p.FeeBurnBps = burn
	return p
}

// (a) THE DOOR: a slash or a burn calibrated above 100 % is no longer a set governance can post.
func TestASlashAboveTheBondIsRefusedByValidation(t *testing.T) {
	d := types.DefaultParams()

	err := sabParams(20000, d.FeeBurnBps).Validate()
	require.Error(t, err, "a slash cannot exceed the bond it takes from")
	require.Contains(t, err.Error(), "slash_leak_bps")

	err = sabParams(d.SlashLeakBps, 20000).Validate()
	require.Error(t, err, "a burn cannot exceed the fee it is taken from")
	require.Contains(t, err.Error(), "fee_burn_bps")

	// The boundary itself stays legal: 10000 is "the whole bond", which is a governance choice, not an
	// invalid one. Bounding at 9999 would refuse a total slash that the arithmetic handles exactly.
	require.NoError(t, sabParams(10000, 10000).Validate(), "exactly 100 %% is a choice, not an overflow")
	require.NoError(t, d.Validate(), "and the shipped set is untouched")

	// ...and the fields that legitimately exceed 10000 are NOT caught. `work_gate_bps` is 15000 on the
	// live chain: a rule applied by shape rather than by meaning would have refused production.
	large := types.DefaultParams()
	large.WorkGateBps = 15000
	require.NoError(t, large.Validate(),
		"work_gate_bps scales a subsidy against demand; it is not a share of a total")
}

// (b) WHAT THE HANDLERS ALONE STILL DO, with the out-of-range parameter written straight into the
// store — a state no message can now produce. The subtraction is unguarded at this site, so the whole
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
		"the bond WRAPPED instead of emptying: the punished miner ends up richer than it started")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(2000), pools.Treasury,
		"and the Treasury is credited coins the bond never held")
}

// (c) THE CONTROL, at a legal calibration on the same fixture: the slash lands exactly, and a slash of
// the WHOLE bond empties it to zero rather than wrapping. Without this half, (a) could not be told
// apart from a validation that refuses everything.
func TestALegalSlashEmptiesTheBondWithoutWrapping(t *testing.T) {
	for nom, bps := range map[string]uint64{"80 %": 8000, "100 %": 10000} {
		t.Run(nom, func(t *testing.T) {
			f := initFixture(t)
			p := sabParams(bps, types.DefaultParams().FeeBurnBps)
			require.NoError(t, p.Validate())
			require.NoError(t, f.keeper.Params.Set(f.ctx, p))
			srv := keeper.NewMsgServerImpl(f.keeper)
			vsJob(t, f, "jOk"+nom, map[string]string{"oa": "1,2,3", "ob": "1,2,3", "oc": "-1,-2,-3"})

			_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{
				Creator: ssAddr20(t, f, "anyone-ok"), JobId: "jOk" + nom,
			})
			require.NoError(t, err)

			m, err := f.keeper.Miner.Get(f.ctx, "oc")
			require.NoError(t, err)
			require.Equal(t, uint64(1000)-uint64(1000)*bps/10000, m.Stake,
				"the bond decreases by exactly the calibrated share, and never below zero")
		})
	}
}
