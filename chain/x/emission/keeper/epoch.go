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

// Reason codes carried by the `emission_epoch_deferred` event. They are CODES, not sentences: an
// operator matches on them and they must survive a rewording of the log line beside them. The
// underlying error, when there is one, goes to the log ONLY — an event is part of the block result, so
// every validator has to produce the same bytes, and the text of an error is written by the layer
// below this one.
const (
	deferReasonSecurityTransferFailed = "security_transfer_failed"
	deferReasonUnbackedAccounting     = "module_account_cannot_back_accounting"
	deferReasonPoolUnreadable         = "pool_counter_unreadable"
)

// EpochBlocks — number of blocks between two Reserve releases (fallback when genesis does not set
// `epoch_blocks`). reserve_release_bps is applied ONCE PER EPOCH: at 2200 the Reserve releases 22 % of
// what REMAINS every epoch (a decaying curve). Whether that reads as "per year" depends entirely on
// this constant — it is one year of blocks ONLY under the assumed block interval, which the consensus
// does not fix. Shorten the epoch and the same 2200 drains the Reserve far faster; that is why an
// observable devnet overrides BOTH through genesis (`emission.params`: short epoch_blocks, low
// reserve_release_bps). Never restate this rate per unit of time without naming the interval assumed.
//
// THE VALUE IS DERIVED, AND THAT IS THE WHOLE POINT. It used to be written `31_536_000`, with its
// meaning — "~1 year of blocks at ~1 s/block" — living in the neighbouring comment. A number whose
// meaning lives in a comment is a number that expires silently: if the real block interval moves to
// 2 s, the constant stays right to the digit and WRONG in meaning (the epoch becomes two years, so
// "22 %/year" becomes 22 % every two years) without any value, test or parameter changing.
//
// The assumption now lives in `x/chaintime`, in ONE place, stated for what it is: a TARGET, not a
// guarantee — CometBFT does not fix the block interval in consensus.
//
// THE NUMERIC VALUE IS UNCHANGED (365 × 86 400 / 1 = 31 536 000): this derivation does not touch
// consensus. `params_durations_test.go` pins it, precisely so that a future adjustment of
// `TargetBlockSeconds` shows up as the consensus change it would be.
const EpochBlocks uint64 = types.DefaultEpochBlocks

// GenesisReserveU — initial Reserve (udndr) = 3.3 M DNDR. Set at genesis; lazily initialized otherwise.
const GenesisReserveU uint64 = 3_300_000_000000

// RunEpoch is called from BeginBlock. Every EpochBlocks it releases a DECREASING slice of the Reserve
// (ADR-023) and allocates it to the three pools. This is deterministic on-chain ACCOUNTING: the
// Reserve counter (udndr) decreases and the pools accumulate; the actual MOVEMENT of coins — payment
// out of the module account — happens in the payout paths. The non-recoverable DEMAND that gates the
// WORK flow is MEASURED on-chain as the drop in supply since the last epoch (fee burns, read through
// bank below): the work flow ACTIVATES as soon as there is real demand, it is not pinned at 0.
func (k Keeper) RunEpoch(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := uint64(sdkCtx.BlockHeight())

	// GOVERNABLE emission parameters, read on-chain. Fall back to the compiled defaults when they are
	// not configured (empty genesis, or a chain launched before they existed) -> backward compatible,
	// no halt.
	//
	// `0` MEANS "EMIT NOTHING", NOT "NOT CONFIGURED". Testing `if pp.ReserveReleaseBps != 0` before
	// adopting the governed params inverts a governance decision: setting `reserve_release_bps = 0` is
	// the EXACT gesture of a governance that wants to STOP emission, and a value `Validate()` accepts,
	// yet it would make the whole param set be ignored in favour of the hardcoded defaults — 22 % of
	// the Reserve released PER EPOCH, with the splits and the vote gate overwritten along the way.
	//
	// The only legitimate fallback is the ABSENCE of params in the store, which `Params.Get` reports as
	// an ERROR. Once params EXIST, they are authoritative exactly as written.
	ep := DefaultParams()
	epochBlocks := EpochBlocks
	if pp, perr := k.Params.Get(ctx); perr == nil {
		ep = Params{ReserveReleaseBps: pp.ReserveReleaseBps, WorkSplitBps: pp.WorkSplitBps, AvailSplitBps: pp.AvailSplitBps, WorkGateBps: pp.WorkGateBps}
		epochBlocks = pp.EpochBlocks
		if ep.ReserveReleaseBps == 0 {
			sdkCtx.Logger().Info("emission: reserve_release_bps=0 -> NO release this epoch (a governed parameter, not a default)", "height", h)
		}
		if epochBlocks == 0 {
			// A zero epoch_blocks would make the epoch fire at EVERY block. Validate() now refuses it;
			// this guard covers state written before that hardening.
			sdkCtx.Logger().Error("emission: epoch_blocks=0 in store -> epoch DISABLED (falling back to the default is forbidden: it would emit at every block)", "height", h)
			return nil
		}
	}

	last, err := k.LastEpoch.Get(ctx)
	if err != nil {
		last = 0 // never executed
	}
	// `last + epochBlocks` OVERFLOWS in uint64 for an epochBlocks near 2^64: the sum wraps below `h`,
	// the condition turns false, and the epoch fires at EVERY BLOCK — the whole Reserve drains in a few
	// hundred blocks. A proposal meaning "never emit again" would produce maximal emission. Subtracting
	// instead of adding removes the sum, and with it any possibility of overflow.
	if h < last {
		// `last` is an ABSOLUTE height, restored verbatim by genesis. If the chain restarts BELOW the
		// last executed epoch — an export/import that starts again at 1, which is exactly what
		// `forZeroHeight` does — then `h < last` stays true for thousands of blocks and emission goes
		// quiet, with no error and no log line. The `x/jobs` module solved the same problem by carrying
		// REMAINING TIME rather than heights; here the datum is a starting point, not a deadline, so it
		// cannot be made relative — but it can refuse to stay silent. An epoch ahead of the chain is an
		// import inconsistency: name it, and restart from the current height rather than waiting hours
		// without a word.
		sdkCtx.Logger().Error("emission: last_epoch is AHEAD of the current height (state imported from a taller chain) -> realigning on the current height; without this realignment emission would stay silent and say nothing.",
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

	// Non-recoverable DEMAND = the drop in supply since the last epoch. Fee burns (FeeBurnBps) DESTROY
	// coins and, since minting is zero, supply can only fall, so its decline measures real demand. Read
	// DIRECTLY through bank, with no cross-module jobs->emission wiring. It gates the WORK flow at 1.5×
	// that demand (anti-self-dealing, ADR-017): no demand -> no work emitted; demand, i.e. settled jobs
	// that burn -> the work flow activates.
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

	// HONEST destination per flow (the model splits the released slice into work/avail/security):
	//   - SECURITY -> fee_collector, hence validators and delegators through x/distribution. This is
	//     the ONLY flow that is distributed.
	//   - AVAILABILITY + WORK -> STAY in the module account (the AvailPool/WorkPool counters), awaiting
	//     their own payout mechanism (availability registry / payment for work). They are NOT claimed
	//     to be paid to validators.
	// On-chain verifiable INVARIANT: balance(emission module) == Reserve + WorkPool + AvailPool, since
	// only security leaves the module. BEST EFFORT: an epoch that cannot be honoured is DEFERRED WITHOUT
	// LOSS — counters and reserve unchanged, no halt — and the slice is RECOMPUTED next cycle from the
	// intact Reserve.
	//
	// A GENESIS THAT DOES NOT FUND THE MODULE ACCOUNT PUTS THE CHAIN IN THAT DEFERRAL AT BLOCK 0 AND
	// KEEPS IT THERE until somebody funds the account: it is a state a network can spend its whole life
	// in, not a transient a launch procedure rules out. So it is reported at ERROR level like the other
	// anomalies of this file, AND as an event — a log line lives on one validator's disk, an event is in
	// the block and reaches anyone watching the chain. Nothing else would say it: this module registers
	// no invariant (no `RegisterInvariants` anywhere in the chain), so nothing audits the identity above
	// per block, and the only other event it emits is the success one below. From outside, a chain that
	// has stopped emitting looks exactly like a chain between two epochs.
	modAddr := authtypes.NewModuleAddress(types.ModuleName)
	if r.Security > 0 {
		sec := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(r.Security)))
		if err := k.bankKeeper.SendCoinsFromModuleToModule(ctx, types.ModuleName, authtypes.FeeCollectorName, sec); err != nil {
			return k.deferEpoch(ctx, h, r, deferReasonSecurityTransferFailed, err)
		}
	} else {
		// A ZERO SECURITY TRANCHE IS THE ONLY WAY THIS FUNCTION COMPLETES AN EPOCH WITHOUT ONE SINGLE
		// BANK CALL. `Validate` refuses `work_split_bps + avail_split_bps` ABOVE 10000, so the sum EQUAL
		// to 10000 is an accepted governance setting, and `EpochRelease` gives security the exact
		// remainder — zero. Nothing is then transferred, so nothing reads a balance, and the block below
		// credits WorkPool and AvailPool on the strength of a counter alone.
		//
		// Those two pools are CLAIMABLE (see keeper.go): crediting them is a PROMISE TO PAY REAL COINS.
		// A promise made against a balance nobody read is what breaks `balance == Reserve + WorkPool +
		// AvailPool` in silence — and it breaks it where it hurts most, since `PayWork`/`PayAvail` bound
		// each payout by the spendable balance and would then hand a miner ZERO while the counter still
		// shows what it is owed. The provision is therefore READ when no transfer proves it.
		//
		// What has to be backed is the identity itself. Nothing leaves the module this epoch, so the
		// balance required AFTER equals the one required BEFORE — the released slice merely moves from
		// the Reserve counter into the pool counters — which is `reserve + WorkPool + AvailPool`.
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

// deferEpoch reports an epoch that could NOT be honoured and skips it. Nothing is credited, nothing
// leaves, and the Reserve is NOT decremented — the whole slice is recomputed from the intact Reserve at
// the next cycle, so a deferral costs no coin. `last_epoch` still advances, which is what makes the
// retry a full epoch away rather than the next block; that cadence is a monetary choice and is not
// decided here.
//
// The EVENT is the point. An operator has no other way to learn that the chain has stopped emitting:
// the counters keep whatever they had, the block keeps being produced, and a query of the module state
// answers exactly what it answered yesterday. So the event names the height, the amount the epoch would
// have released, the security tranche it would have transferred, and the module ACCOUNT — the address a
// human has to fund or inspect, which no reader can derive from the module name alone.
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
	sdkCtx.Logger().Error("emission: EPOCH DEFERRED — nothing released, nothing credited; emission stays stopped until this is resolved",
		"height", h, "reason", reason, "module_account", modAddr,
		"expected_release", r.Released, "expected_security", r.Security, "err", causeText)

	return k.LastEpoch.Set(ctx, h) // skip one epoch, no loss: reserve/pools intact, slice recomputed next cycle
}

// poolValue reads a pool counter for the provisioning decision. A counter that was NEVER WRITTEN is
// worth zero — nothing has been credited yet — and that zero is measured, not assumed. ANY OTHER read
// error is a different statement: the value is UNKNOWN. An unknown counter must not become a permissive
// zero on the path that decides whether the module can back what it is about to promise, because a
// lower "required" is exactly the direction that lets the promise through.
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
