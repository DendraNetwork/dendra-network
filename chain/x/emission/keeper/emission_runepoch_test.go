package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// epochCtx returns the context an epoch is run in, with a fresh event manager: this file asserts
// what this call emitted, not what the fixture accumulated.
func epochCtx(f *fixture, height int64) sdk.Context {
	return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(height).WithEventManager(sdk.NewEventManager())
}

// eventAttrs returns the attributes of the first event of type `typ`, and fails when there is none.
// Events are asserted, not only logs: a log line lives on one validator's disk and no operator
// tooling can subscribe to it, so a failure reported only in the logs stays invisible.
func eventAttrs(t *testing.T, ctx sdk.Context, typ string) map[string]string {
	t.Helper()
	for _, ev := range ctx.EventManager().Events() {
		if ev.Type != typ {
			continue
		}
		attrs := map[string]string{}
		for _, a := range ev.Attributes {
			attrs[a.Key] = a.Value
		}
		return attrs
	}
	t.Fatalf("event %q was not emitted (emitted: %v)", typ, eventTypes(ctx))
	return nil
}

func requireNoEvent(t *testing.T, ctx sdk.Context, typ string) {
	t.Helper()
	for _, ev := range ctx.EventManager().Events() {
		require.NotEqual(t, typ, ev.Type, "event %q must not be emitted here", typ)
	}
}

func eventTypes(ctx sdk.Context) []string {
	out := []string{}
	for _, ev := range ctx.EventManager().Events() {
		out = append(out, ev.Type)
	}
	return out
}

// `RunEpoch` moves the security tranche to fee_collector, decrements the Reserve, keeps the
// availability and work tranches inside the module, and preserves the accounting identity
// `reserve_left + WorkPool + AvailPool == initial_reserve - security_sent`. A fixture that passes a nil
// bank keeper cannot observe any of this, which is why the flow itself is asserted here.
func TestRunEpochSecurityFluxEM02(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000 // no burns -> zero demand -> the work flux is gated to 0
	// epoch_blocks=20 explicitly (the default is about one year of blocks), so the epoch fires at h=100.
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(2200, 5000, 2000, 15000, 20)))

	ctx := epochCtx(f, 100) // > epoch_blocks(20) -> epoch fires
	require.NoError(t, f.keeper.RunEpoch(ctx))

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(2_937_000_000000), r, "Reserve decremented by the released tranche")

	require.Equal(t, "217800000000udndr", f.bank.moved[authtypes.FeeCollectorName].String(),
		"only the security tranche leaves for fee_collector")

	av, err := f.keeper.AvailPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(145_200_000000), av, "the availability tranche is held inside the module")

	wp, err := f.keeper.WorkPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), wp, "work is gated to 0 without demand")

	// Accounting invariant: module balance == Reserve + WorkPool + AvailPool, only security leaves.
	require.Equal(t, reserve-217_800_000000, r+wp+av)

	requireNoEvent(t, ctx, "emission_epoch_deferred")
}

// An unfunded module account makes `RunEpoch` defer: it advances the epoch without touching the
// Reserve or halting, and reports it where a machine can see it. The event is the assertion that
// matters: a deferral that only reaches the log is a chain that has silently stopped emitting.
func TestRunEpochDefersWhenUnfundedEM03(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 0))
	f.bank.supply = 10_000_000_000000
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(2200, 5000, 2000, 15000, 20)))
	f.bank.fail = true // SendCoinsFromModuleToModule fails -> the release is deferred

	ctx := epochCtx(f, 100)
	require.NoError(t, f.keeper.RunEpoch(ctx), "best effort: no halt even when unfunded")

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, reserve, r, "Reserve unchanged (release deferred, retried at the next epoch)")

	// The event's attributes are what an operator has to act on: which height, why, how much was
	// owed, and which account to fund, since a reader cannot derive that address from the module name.
	attrs := eventAttrs(t, ctx, "emission_epoch_deferred")
	require.Equal(t, "100", attrs["height"])
	require.Equal(t, "security_transfer_failed", attrs["reason"])
	require.Equal(t, authtypes.NewModuleAddress(types.ModuleName).String(), attrs["module_account"],
		"the deferral names the account to fund")
	require.Equal(t, "363000000000", attrs["expected_release"], "the amount the epoch would have released")
	require.Equal(t, "217800000000", attrs["expected_security"], "the tranche that could not be transferred")

	requireNoEvent(t, ctx, "emission_epoch") // a deferred epoch must never look like a successful one

	// The retry is a full epoch away: `last_epoch` advances. This is pinned so that a change of cadence
	// is a deliberate change and never a side effect.
	lastEp, err := f.keeper.LastEpoch.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(100), lastEp)
}

// A zero security tranche must not buy a free pass. `Validate` only refuses
// `work_split_bps + avail_split_bps` above 10000, so a sum equal to 10000 is an accepted governance
// setting, and it leaves security at exactly zero, which removes the only bank call of the epoch. The
// pools would then be credited and the Reserve decremented with nothing read and nothing reported,
// while WorkPool and AvailPool are claimable: the counter would owe a miner coins the module does not
// hold.
func TestRunEpochRefusesUnbackedPoolCreditWhenSecurityIsZero(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	// Premise, pinned rather than assumed: governance can actually adopt this parameter set.
	p := types.NewParams(2200, 8000, 2000, 15000, 20)
	require.NoError(t, p.Validate(), "work_split + avail_split == 10000 is accepted -> security is 0")

	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 0))
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	// Real demand (supply fell since the last epoch), so the work tranche is non-zero: the credit
	// this test refuses has to be one that would otherwise happen.
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 10_000_000_000000))
	f.bank.supply = 9_999_000_000000 // demand = 1e9 udndr -> work = 1.5e9 (gate 15000 bps)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// One udndr short of the accounting the module would have to back (reserve + WorkPool +
	// AvailPool): the boundary is where a guard is worth testing.
	f.bank.spendable = reserve - 1

	ctx := epochCtx(f, 100)
	require.NoError(t, f.keeper.RunEpoch(ctx), "best effort: no halt")

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, reserve, r, "Reserve untouched: a deferral costs no coin")

	wp, err := f.keeper.WorkPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), wp, "no unbacked promise credited to the work pool")
	av, err := f.keeper.AvailPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), av, "no unbacked promise credited to the availability pool")

	attrs := eventAttrs(t, ctx, "emission_epoch_deferred")
	require.Equal(t, "module_account_cannot_back_accounting", attrs["reason"])
	require.Equal(t, "100", attrs["height"])
	require.Equal(t, authtypes.NewModuleAddress(types.ModuleName).String(), attrs["module_account"])
	require.Equal(t, "146700000000", attrs["expected_release"], "work 1.5e9 + avail 145.2e9, security 0")
	require.Equal(t, "0", attrs["expected_security"], "the zero tranche that removed the bank call")

	requireNoEvent(t, ctx, "emission_epoch")
	require.Nil(t, f.bank.moved, "nothing leaves the module on a deferral")
}

// The other half of the same guard: with the same zero-security parameters, a module account that
// does back the accounting emits normally. A provisioning check tested only on what it must refuse
// could hide a chain that never emits again.
func TestRunEpochCreditsPoolsWhenSecurityIsZeroAndAccountIsBacked(t *testing.T) {
	f := initFixture(t)
	const reserve uint64 = 3_300_000_000000
	require.NoError(t, f.keeper.Reserve.Set(f.ctx, reserve))
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 0))
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastEpoch.Set(f.ctx, 0))
	require.NoError(t, f.keeper.LastSupply.Set(f.ctx, 10_000_000_000000))
	f.bank.supply = 9_999_000_000000
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.NewParams(2200, 8000, 2000, 15000, 20)))

	f.bank.spendable = reserve // exactly what the accounting claims: accepted (the bound is inclusive)

	ctx := epochCtx(f, 100)
	require.NoError(t, f.keeper.RunEpoch(ctx))

	wp, err := f.keeper.WorkPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1_500_000000), wp, "work = 1.5x the 1e9 udndr demand")
	av, err := f.keeper.AvailPool.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(145_200_000000), av, "availability = 20 % of the release")

	r, err := f.keeper.Reserve.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, reserve-146_700_000000, r)
	// Nothing left the module (security is 0), so the identity holds with the balance unchanged.
	require.Equal(t, reserve, r+wp+av)

	require.Nil(t, f.bank.moved, "a zero security tranche transfers nothing")
	requireNoEvent(t, ctx, "emission_epoch_deferred")
	require.Equal(t, "146700000000", eventAttrs(t, ctx, "emission_epoch")["released"])
}
