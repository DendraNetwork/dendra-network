package keeper

import (
	"context"
	"errors"
	"strconv"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// Reason codes carried by the `emission_epoch_deferred` event. They are codes rather than sentences:
// operators match on them, and they must survive a rewording of the log line next to them. The
// underlying error, when there is one, goes to the log only: an event is part of the block result,
// so every validator has to produce the same bytes, and the text of an error comes from the layer
// below.
const (
	deferReasonSecurityTransferFailed = "security_transfer_failed"
	deferReasonUnbackedAccounting     = "module_account_cannot_back_accounting"
	deferReasonPoolUnreadable         = "pool_counter_unreadable"
)

// EpochBlocks is the number of blocks between two Reserve releases (the fallback when genesis does
// not set `epoch_blocks`). reserve_release_bps is applied once per epoch: at 2200 the Reserve releases
// 22 % of what remains every epoch (a decaying curve). Whether that reads as "per year" depends on
// this constant, which is one year of blocks only under the assumed block interval, and the consensus
// does not fix that interval. Shorten the epoch and the same 2200 drains the Reserve far faster, which
// is why an observable devnet overrides both through genesis (`emission.params`: short epoch_blocks,
// low reserve_release_bps). Never restate this rate per unit of time without naming the assumed
// interval.
//
// The value is derived. Written as `31_536_000`, with its meaning ("about 1 year of blocks at about
// 1 s/block") in a neighbouring comment, it could go stale silently: if the real block interval moved
// to 2 s, the constant would stay right to the digit and wrong in meaning. The assumption lives in one
// place, `x/chaintime`, stated as a target that consensus does not fix.
//
// The numeric value is unchanged (365 x 86 400 / 1 = 31 536 000), so this derivation does not touch
// consensus. `params_durations_test.go` pins it, so that a future change of `TargetBlockSeconds` shows
// up as the consensus change it would be.
const EpochBlocks uint64 = types.DefaultEpochBlocks

// GenesisReserveU is the initial Reserve (udndr), 3.3 M DNDR. It is set at genesis and initialized lazily otherwise.
const GenesisReserveU uint64 = 3_300_000_000000

// RunEpoch is called from BeginBlock. Every EpochBlocks it releases a decreasing slice of the Reserve
// and allocates it to the three pools. This is deterministic on-chain accounting: the
// Reserve counter (udndr) decreases and the pools accumulate, while the actual movement of coins out
// of the module account happens in the payout paths. The non-recoverable demand that gates the work
// flow is measured on-chain as the drop in supply since the last epoch (fee burns, read through bank
// below), so the work flow activates as soon as there is real demand.
func (k Keeper) RunEpoch(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := uint64(sdkCtx.BlockHeight())

	// Governable emission parameters, read on-chain, with the compiled defaults as a fallback when
	// they are not configured (empty genesis, or a chain launched before they existed), so older
	// chains keep running.
	//
	// A zero means "emit nothing"; it does not mean "not configured". Testing
	// `if pp.ReserveReleaseBps != 0` before adopting the governed params would invert a governance
	// decision: `reserve_release_bps = 0` is how governance stops emission, and `Validate()` accepts
	// it, yet it would make the whole param set be ignored in favour of the hardcoded defaults (22 %
	// of the Reserve released per epoch, with the splits and the vote gate overwritten as well).
	//
	// The only legitimate fallback is the absence of params in the store, which `Params.Get` reports
	// as an error. Once params exist, they apply exactly as written.
	ep := DefaultParams()
	epochBlocks := EpochBlocks
	if pp, perr := k.Params.Get(ctx); perr == nil {
		ep = Params{ReserveReleaseBps: pp.ReserveReleaseBps, WorkSplitBps: pp.WorkSplitBps, AvailSplitBps: pp.AvailSplitBps, WorkGateBps: pp.WorkGateBps}
		epochBlocks = pp.EpochBlocks
		if ep.ReserveReleaseBps == 0 {
			sdkCtx.Logger().Info("emission: reserve_release_bps=0 -> no release this epoch (a governed parameter, not a default)", "height", h)
		}
		if epochBlocks == 0 {
			// A zero epoch_blocks would make the epoch fire at every block. Validate() refuses it now;
			// this guard covers state written before that check.
			sdkCtx.Logger().Error("emission: epoch_blocks=0 in store -> epoch disabled (falling back to the default is forbidden: it would emit at every block)", "height", h)
			return nil
		}
	}

	last, err := k.LastEpoch.Get(ctx)
	if err != nil {
		last = 0 // never executed
	}
	// `last + epochBlocks` overflows in uint64 for an epochBlocks near 2^64: the sum wraps below `h`,
	// the condition turns false, and the epoch fires at every block, draining the whole Reserve in a
	// few hundred blocks. A proposal meaning "never emit again" would produce maximal emission.
	// Subtracting instead of adding removes the sum, and with it any overflow.
	if h < last {
		// `last` is an absolute height, restored as is by genesis. If the chain restarts below the last
		// executed epoch (an export/import that starts again at 1, as `forZeroHeight` does), `h < last`
		// stays true for thousands of blocks and emission goes quiet with no error and no log line.
		// `x/jobs` solved the same problem by carrying remaining time instead of heights; here the value
		// is a starting point, not a deadline, so it cannot be made relative, but it can refuse to stay
		// silent. An epoch ahead of the chain is an import inconsistency: report it and restart from the
		// current height instead of waiting hours without a word.
		sdkCtx.Logger().Error("emission: last_epoch is ahead of the current height (state imported from a taller chain) -> realigning on the current height; without this realignment emission would stay silent and say nothing.",
			"last_epoch", last, "height", h)
		if err := k.LastEpoch.Set(ctx, h); err != nil {
			return err
		}
		return nil
	}
	if h-last < epochBlocks {
		return nil // not yet due
	}

	reserve, err := k.Reserve.Get(ctx)
	if err != nil {
		reserve = GenesisReserveU // lazy init when genesis did not set it
	}
	if reserve == 0 {
		return k.LastEpoch.Set(ctx, h) // Reserve exhausted: nothing to release
	}

	// Non-recoverable demand is the drop in supply since the last epoch. Fee burns (FeeBurnBps)
	// destroy coins and, since minting is zero, supply can only fall, so its decline measures real
	// demand. It is read directly through bank, with no jobs->emission wiring, and gates the work flow
	// at 1.5x that demand (anti-self-dealing): no demand means no work emitted, and settled
	// jobs that burn activate the work flow.
	curSupply := k.bankKeeper.GetSupply(ctx, "udndr").Amount.Uint64()
	lastSupply, lsErr := k.LastSupply.Get(ctx)
	if lsErr != nil || lastSupply == 0 {
		lastSupply = curSupply // first epoch (or uninitialized) -> delta 0
	}
	var demand uint64
	if lastSupply > curSupply {
		demand = lastSupply - curSupply
	}
	r := EpochRelease(reserve, demand, ep)

	// Destination per flow (the model splits the released slice into work/avail/security):
	//   - Security goes to fee_collector, hence to validators and delegators through x/distribution.
	//     It is the only flow that is distributed.
	//   - Availability and work stay in the module account (the AvailPool/WorkPool counters) until
	//     their own payout mechanism (availability registry, payment for work) pays them out. They
	//     are not claimed to be paid to validators.
	// Invariant, verifiable on-chain: balance(emission module) == Reserve + WorkPool + AvailPool,
	// since only security leaves the module. Best effort: an epoch that cannot be honoured is
	// deferred without loss (counters and reserve unchanged, no halt), and the slice is recomputed
	// next cycle from the intact Reserve.
	//
	// A genesis that does not fund the module account puts the chain in that deferral from block 0
	// and keeps it there until somebody funds the account: a network can spend its whole life in that
	// state. It is therefore reported at error level like the other anomalies of this file, and as
	// an event, because a log line lives on one validator's disk while an event is in the block and
	// reaches anyone watching the chain. Nothing else would report it: this module registers no
	// invariant (there is no `RegisterInvariants` anywhere in the chain), so nothing audits the
	// identity above per block, and the only other event it emits is the success event below. From
	// outside, a chain that has stopped emitting looks like a chain between two epochs.
	modAddr := authtypes.NewModuleAddress(types.ModuleName)
	if r.Security > 0 {
		sec := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(r.Security)))
		if err := k.bankKeeper.SendCoinsFromModuleToModule(ctx, types.ModuleName, authtypes.FeeCollectorName, sec); err != nil {
			return k.deferEpoch(ctx, h, r, deferReasonSecurityTransferFailed, err)
		}
	} else {
		// A zero security tranche is the only way this function completes an epoch without a single bank
		// call. `Validate` refuses `work_split_bps + avail_split_bps` above 10000, so a sum equal to 10000
		// is an accepted setting, and `EpochRelease` gives security the exact remainder, zero. Nothing is
		// transferred, so nothing reads a balance, and the block below would credit WorkPool and
		// AvailPool on the strength of a counter alone.
		//
		// Those two pools are claimable (see keeper.go), so crediting them is a promise to pay real
		// coins. A promise made against a balance nobody read silently breaks
		// `balance == Reserve + WorkPool + AvailPool`, and `PayWork`/`PayAvail` bound each payout by the
		// spendable balance, so they would hand a miner zero while the counter still shows what it is
		// owed. The provision is therefore read when no transfer proves it.
		//
		// What has to be backed is the identity itself. Nothing leaves the module this epoch, so the
		// balance required after equals the one required before (the released slice only moves from the
		// Reserve counter into the pool counters): `reserve + WorkPool + AvailPool`.
		work, werr := poolValue(ctx, k.WorkPool)
		if werr != nil {
			return k.deferEpoch(ctx, h, r, deferReasonPoolUnreadable, werr)
		}
		avail, aerr := poolValue(ctx, k.AvailPool)
		if aerr != nil {
			return k.deferEpoch(ctx, h, r, deferReasonPoolUnreadable, aerr)
		}
		required := math.NewIntFromUint64(reserve).
			Add(math.NewIntFromUint64(work)).
			Add(math.NewIntFromUint64(avail))
		if k.bankKeeper.SpendableCoins(ctx, modAddr).AmountOf("udndr").LT(required) {
			return k.deferEpoch(ctx, h, r, deferReasonUnbackedAccounting, nil)
		}
	}

	if err := addItem(ctx, k.WorkPool, r.Work); err != nil {
		return err
	}
	if err := addItem(ctx, k.AvailPool, r.Avail); err != nil {
		return err
	}
	if err := addItem(ctx, k.SecurityPool, r.Security); err != nil {
		return err
	}
	if err := k.Reserve.Set(ctx, r.NewReserve); err != nil {
		return err
	}
	if err := k.LastEpoch.Set(ctx, h); err != nil {
		return err
	}
	if err := k.LastSupply.Set(ctx, curSupply); err != nil { // remember the supply for the next delta
		return err
	}

	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"emission_epoch",
		sdk.NewAttribute("height", strconv.FormatUint(h, 10)),
		sdk.NewAttribute("released", strconv.FormatUint(r.Released, 10)),
		sdk.NewAttribute("avail", strconv.FormatUint(r.Avail, 10)),
		sdk.NewAttribute("security", strconv.FormatUint(r.Security, 10)),
		sdk.NewAttribute("reserve_left", strconv.FormatUint(r.NewReserve, 10)),
	))
	sdkCtx.Logger().Info("emission: Reserve released — security distributed, availability and work retained in the module",
		"height", h, "released", r.Released, "work_retained", r.Work, "avail_retained", r.Avail, "security_distributed", r.Security, "demand", demand, "reserve_left", r.NewReserve)
	return nil
}

// deferEpoch reports an epoch that could not be honoured and skips it. Nothing is credited, nothing
// leaves, and the Reserve is not decremented: the whole slice is recomputed from the intact Reserve at
// the next cycle, so a deferral costs no coin. `last_epoch` still advances, which puts the retry a
// full epoch away rather than at the next block; that cadence is a monetary choice and is not decided
// here.
//
// The event matters most. An operator has no other way to learn that the chain has stopped emitting:
// the counters keep their values, blocks keep coming, and a query of the module state answers what it
// answered yesterday. So the event names the height, the amount the epoch would have released, the
// security tranche it would have transferred, and the module account, the address a human has to
// fund or inspect, which no reader can derive from the module name alone.
func (k Keeper) deferEpoch(ctx context.Context, h uint64, r Release, reason string, cause error) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	modAddr := authtypes.NewModuleAddress(types.ModuleName).String()

	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"emission_epoch_deferred",
		sdk.NewAttribute("height", strconv.FormatUint(h, 10)),
		sdk.NewAttribute("reason", reason),
		sdk.NewAttribute("module_account", modAddr),
		sdk.NewAttribute("expected_release", strconv.FormatUint(r.Released, 10)),
		sdk.NewAttribute("expected_security", strconv.FormatUint(r.Security, 10)),
	))

	causeText := ""
	if cause != nil {
		causeText = cause.Error()
	}
	sdkCtx.Logger().Error("emission: epoch deferred — nothing released, nothing credited; emission stays stopped until this is resolved",
		"height", h, "reason", reason, "module_account", modAddr,
		"expected_release", r.Released, "expected_security", r.Security, "err", causeText)

	return k.LastEpoch.Set(ctx, h) // skip one epoch, no loss: reserve/pools intact, slice recomputed next cycle
}

// poolValue reads a pool counter for the provisioning decision. A counter that was never written is
// worth zero (nothing has been credited yet), and that zero is measured. Any other read error means
// the value is unknown, and an unknown counter must not become a permissive zero on the path that
// decides whether the module can back what it is about to promise: a lower "required" amount is the
// direction that lets the promise through.
func poolValue(ctx context.Context, item collections.Item[uint64]) (uint64, error) {
	v, err := item.Get(ctx)
	if errors.Is(err, collections.ErrNotFound) {
		return 0, nil
	}
	return v, err
}

// addItem increments an Item[uint64], treating an uninitialized item as 0.
func addItem(ctx context.Context, item collections.Item[uint64], amt uint64) error {
	cur, err := item.Get(ctx)
	if err != nil {
		cur = 0
	}
	return item.Set(ctx, cur+amt)
}
