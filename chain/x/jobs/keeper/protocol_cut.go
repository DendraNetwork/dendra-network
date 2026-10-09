package keeper

import (
	"bytes"
	"context"
	"errors"
	"fmt"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-048 items 3 and 4 — THE PROTOCOL CUT AND THE SUBSIDY RIGHT OF A JOB FOLLOW THE MINER'S PAYMENT.
//
// THE DEFECT. Every optimistic job takes a cut (`protocol_fee_bps`) split three ways. Until epoch 14 the
// three shares were added to `Pools.Validators/Team/Treasury` at settlement — counters, with no code
// anywhere that paid them out. The public split ("7.5 to the validators, 3 to the team") described coins
// that stayed in the module account for good. And a refund reversed "the cut" from whichever sub-pool
// held enough, in a fixed order, so refunding one job drew on the shares of others.
//
// THE RULE NOW, ONE LIFECYCLE FOR EVERY AMOUNT THE MODULE RETAINS:
//   - settlement RETAINS each share under the job's id (`retainCut`), next to the miner's retention
//     (`HeldFee`) and the deferred burn (`HeldBurn`);
//   - finality PAYS them (`payRetainedCut`, called by `releaseHeld` on the branch that burns): the
//     validators' share to fee_collector, which x/distribution pays to stakers; the team's share to
//     `team_address`; the treasury's share into `Pools.Treasury`, which is where it always lived;
//   - a refund RETURNS them to the client (`refundRetainedCut`), this job's shares and nothing else.
//
// The subsidy right (`Demand`) follows the same path: recorded at settlement, credited at finality,
// dropped on a refund. A miner can no longer claim, during the audit window, a subsidy that a refund
// would then fail to take back.
//
// ⚠️ PAYING OUT IS IRREVERSIBLE, AND THAT IS WHY IT WAITS FOR FINALITY. Coins sent to fee_collector are
// distributed at the next block; coins sent to the team are gone. A slash decided AFTER finality (a
// human dispute opened inside `dispute_window`) can therefore no longer return the cut from the module:
// what was paid out is owed by the miner's bond, exactly like the part of a partial hold already paid to
// the miner. That is the rule of `slashCheatedPrimary` (`owed = fee - refundedRetained`). ⛔ This comment
// first claimed the rule applied everywhere; the money review found that `AdjudicateDispute` sent its
// whole slash to the Treasury and never applied it, so a cheat proven after finality refunded the client
// nothing. It now applies it too (`retainedTotal`), BEFORE the whistleblower's reward.

// retainCut — records the three shares of `jobId`'s cut. Zero shares are not written: proto3 would not
// distinguish them anyway, and `Get` on an absent key is the zero the readers already expect.
func (k Keeper) retainCut(ctx context.Context, jobId string, validators, team, treasury uint64) error {
	for _, e := range []struct {
		m   collections.Map[string, uint64]
		amt uint64
	}{{k.HeldValidatorShare, validators}, {k.HeldTeamShare, team}, {k.HeldTreasuryShare, treasury}} {
		if e.amt == 0 {
			continue
		}
		if err := e.m.Set(ctx, jobId, e.amt); err != nil {
			return err
		}
	}
	return nil
}

// payRetainedCut — FINALITY: pays the retained shares of `job` and credits its subsidy right.
//
// Every step follows the module's rule for retained money (`releaseHeld`, ADR-034 M8): the key is removed
// only AFTER the transfer succeeds. A failed step keeps its key and the function returns true, and the
// caller queues the job in `PendingHeldRelease`, whose sweep calls `releaseHeld` again; each step is
// idempotent because a paid share has no key left. The queue is the CALLER's to manage, because
// `releaseHeld` also clears it on its own paths — a mark set here and cleared there would be a retry
// that never happens. Never an error: this runs in the EndBlocker, where an error halts the chain.
func (k Keeper) payRetainedCut(ctx context.Context, job *types.Job) (retry bool) {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	coin := func(a uint64) sdk.Coins { return sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(a))) }

	// (1) validators -> fee_collector -> x/distribution.
	if v, err := k.HeldValidatorShare.Get(ctx, job.JobId); err == nil {
		if v == 0 {
			_ = k.HeldValidatorShare.Remove(ctx, job.JobId)
		} else if sErr := k.bankKeeper.SendCoinsFromModuleToModule(ctx, types.ModuleName, authtypes.FeeCollectorName, coin(v)); sErr == nil {
			_ = k.HeldValidatorShare.Remove(ctx, job.JobId)
		} else {
			sdkCtx.Logger().Error("ADR-048: validators' share NOT sent to fee_collector -> kept retained and queued for retry",
				"job_id", job.JobId, "amount", v, "err", sErr.Error())
			retry = true
		}
	}

	// (2) team -> team_address. Without an address the share stays in the module, counted in
	// `Pools.Team` as it was before epoch 14: an operator who has not named a recipient keeps the old
	// behaviour rather than having one invented for it.
	if t, err := k.HeldTeamShare.Get(ctx, job.JobId); err == nil {
		p, pErr := k.Params.Get(ctx)
		switch {
		case t == 0:
			_ = k.HeldTeamShare.Remove(ctx, job.JobId)
		case pErr != nil:
			retry = true // the recipient cannot be read; nothing moves until it can
		case p.TeamAddress == "":
			if k.addToPools(ctx, func(pl *types.Pools) { pl.Team += t }) {
				_ = k.HeldTeamShare.Remove(ctx, job.JobId)
			} else {
				retry = true
			}
		default:
			toBz, aErr := k.addressCodec.StringToBytes(p.TeamAddress)
			if aErr != nil {
				sdkCtx.Logger().Error("ADR-048: team_address unreadable -> team share kept retained", "job_id", job.JobId, "err", aErr.Error())
				retry = true
			} else if sErr := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coin(t)); sErr == nil {
				_ = k.HeldTeamShare.Remove(ctx, job.JobId)
			} else {
				sdkCtx.Logger().Error("ADR-048: team share NOT sent -> kept retained and queued for retry",
					"job_id", job.JobId, "amount", t, "err", sErr.Error())
				retry = true
			}
		}
	}

	// (3) treasury -> Pools.Treasury. The coins already sit in the module; finality only gives them their
	// owner, as the epoch-13 code did at settlement.
	if tr, err := k.HeldTreasuryShare.Get(ctx, job.JobId); err == nil {
		if tr == 0 || k.addToPools(ctx, func(pl *types.Pools) { pl.Treasury += tr }) {
			_ = k.HeldTreasuryShare.Remove(ctx, job.JobId)
		} else {
			retry = true
		}
	}

	// (4) the subsidy right. A primary that is not registered right now keeps its right pending, as
	// `releaseHeld` keeps its retention: an eviction is not a forfeiture, and the miner may come back.
	if d, err := k.HeldDemand.Get(ctx, job.JobId); err == nil {
		if m, mErr := k.Miner.Get(ctx, job.MinerId); mErr == nil {
			m.Demand += d
			if sErr := k.Miner.Set(ctx, m.MinerId, m); sErr == nil {
				_ = k.HeldDemand.Remove(ctx, job.JobId)
			} else {
				retry = true
			}
		} else {
			retry = true
		}
	}

	return retry
}

// refundRetainedCut — REFUND: returns `job`'s retained shares to its client and drops its subsidy right.
// Returns what reached the client. A share the client cannot receive (unreadable address) goes to the
// Treasury, the module's rule for every refund: never lost, never left without an owner.
//
// `hadRight` tells the caller whether a subsidy right was still PENDING. When it was not, the job either
// never earned one (self-dealing) or was already final and credited it — the caller tells the two apart.
func (k Keeper) refundRetainedCut(ctx context.Context, job *types.Job) (sent uint64, hadRight bool) {
	for _, m := range []collections.Map[string, uint64]{k.HeldValidatorShare, k.HeldTeamShare, k.HeldTreasuryShare} {
		amt, err := m.Get(ctx, job.JobId)
		if err != nil {
			continue
		}
		_ = m.Remove(ctx, job.JobId)
		if amt == 0 {
			continue
		}
		s := k.refundClient(ctx, job, amt, amt)
		sent += s
		k.addTreasury(ctx, amt-s)
	}
	if has, err := k.HeldDemand.Has(ctx, job.JobId); err == nil && has {
		_ = k.HeldDemand.Remove(ctx, job.JobId)
		hadRight = true
	}
	return sent, hadRight
}

// checkTeamAddressPrefix — `Params.Validate` checks that `team_address` is a well-formed account
// address, prefix-agnostic on purpose (it runs where the chain's prefix is not set). The keeper KNOWS the
// prefix, so it checks the rest wherever params enter the store — genesis and MsgUpdateParams. An address
// with a foreign prefix would validate, and every finality would then fail to pay the team, keep its share
// retained and queue the job for a retry swept every epoch, for ever: a governance typo turned into a
// growing queue. Found by the parameter review of the relaunch batch.
func (k Keeper) checkTeamAddressPrefix(p types.Params) error {
	if p.TeamAddress == "" {
		return nil
	}
	bz, err := k.addressCodec.StringToBytes(p.TeamAddress)
	if err != nil {
		return fmt.Errorf("team_address %q is not an address of this chain: %w", p.TeamAddress, err)
	}
	// A MODULE ACCOUNT of this chain has the right prefix and the same consequence: the bank refuses to
	// credit the blocked ones (fee_collector, distribution, the bonded pools, gov), and this module's own
	// accounts would pay the team share to itself. Found by the adversarial review of the relaunch.
	for _, name := range teamAddressForbiddenModules {
		if bytes.Equal(bz, authtypes.NewModuleAddress(name)) {
			return fmt.Errorf("team_address %q is the %q module account, which cannot receive the team share", p.TeamAddress, name)
		}
	}
	return nil
}

// teamAddressForbiddenModules — module accounts that must never be the team address.
var teamAddressForbiddenModules = []string{
	authtypes.FeeCollectorName, "distribution", "bonded_tokens_pool", "not_bonded_tokens_pool", "gov",
	"mint", types.ModuleName, "emission", "nft", "transfer", "interchainaccounts",
}

// retainedTotal — everything the module still retains for `jobId`: the miner's retention, the deferred
// burn and the three shares of the cut. What a client paid and is NOT in this sum has already left the
// module (to the miner, to stakers, to the team, or burned), which is exactly what a proven cheat owes
// back out of the miner's bond. A read error counts the entry as retained: over-counting the retention
// only lowers what is taken from the bond, never the reverse.
func (k Keeper) retainedTotal(ctx context.Context, jobId string) uint64 {
	var total uint64
	for _, m := range []collections.Map[string, uint64]{k.HeldFee, k.HeldBurn, k.HeldValidatorShare, k.HeldTeamShare, k.HeldTreasuryShare} {
		v, err := m.Get(ctx, jobId)
		if err == nil {
			total += v
		} else if !errors.Is(err, collections.ErrNotFound) {
			return ^uint64(0)
		}
	}
	return total
}

// addToPools — read-modify-write of the Pools item. Returns false when the write fails, so the caller
// keeps its retained key and retries instead of losing the amount between the two stores.
//
// Only an ABSENT item starts from zero. Any other read error refuses: rewriting Pools from an empty value
// because it could not be decoded would erase every counter it holds to add one amount.
func (k Keeper) addToPools(ctx context.Context, f func(*types.Pools)) bool {
	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if !errors.Is(err, collections.ErrNotFound) {
			return false
		}
		pools = types.Pools{}
	}
	f(&pools)
	return k.Pools.Set(ctx, pools) == nil
}
