package keeper

import (
	"context"
	"encoding/hex"
	"math/bits"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// runAvailabilityEpoch is called on every block by EndBlock. When availability is enabled
// (avail_epoch_blocks>0) AND the height is an epoch boundary, it (1) PAYS OUT the AvailPool to the miners
// proven present during the previous epoch, weighted by bond, then (2) ROLLS the new epoch's challenge
// from the AppHash, which was unpredictable during the epoch that just ended. OFF by default.
func (k Keeper) runAvailabilityEpoch(ctx context.Context, h int64) error {
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil // no params -> nothing to do
	}
	eb := int64(params.AvailEpochBlocks)
	if eb <= 0 || h <= 0 || h%eb != 0 {
		return nil // availability OFF, or not an epoch boundary
	}
	epoch := h / eb

	// (1) ADR-022 (slashable liveness): slash the BONDED miners that did NOT prove their availability
	//     during the previous epoch, BEFORE payAvailability purges the presence records. Dormant while
	//     avail_slash_bps==0.
	if epoch >= 1 {
		if err := k.runAvailabilitySlash(ctx, epoch-1, params); err != nil {
			return err
		}
		if err := k.payAvailability(ctx, epoch-1, params.AvailPayoutBps, params.AvailRequireDemand, params.VerificationMode == 1); err != nil {
			return err
		}
	}

	// (2) Roll the new epoch's challenge: the AppHash of this block, unpredictable during the epoch that
	//     just ended.
	challenge := hex.EncodeToString(sdk.UnwrapSDKContext(ctx).HeaderInfo().AppHash)
	if challenge == "" {
		challenge = "genesis" // fallback when the AppHash is empty (very start of the chain)
	}
	return k.AvailChallenge.Set(ctx, challenge)
}

// payAvailability distributes a FRACTION (payoutBps) of the AvailPool among the miners proven present at
// `epoch`, WEIGHTED BY BOND. The weighting is what makes it anti-sybil: splitting a stake across
// identities does not multiply the revenue. Shares are computed in big.Int to rule out overflow, and the
// integer-division remainder stays in the pool. Presence records for the epoch are purged whether or not
// a payout happened.
func (k Keeper) payAvailability(ctx context.Context, epoch int64, payoutBps uint64, requireDemand bool, requireVrf bool) error {
	type present struct {
		operator string
		stake    uint64
	}
	var miners []present
	var allKeys []collections.Pair[int64, string]
	var totalStake uint64
	rng := collections.NewPrefixedPairRange[int64, string](epoch)
	if err := k.Available.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		allKeys = append(allKeys, key)
		if m, e := k.Miner.Get(ctx, key.K2()); e == nil {
			// With avail_require_demand set, only miners that served REAL demand (demand>0) are paid, so
			// liveness alone — echoing the AppHash, farmable WITHOUT a GPU — earns nothing. The presence
			// record is purged below regardless.
			if requireDemand && m.Demand == 0 {
				return false, nil
			}
			// ADR-022, defence at payout time: no vrf_pubkey in incentivised mode means no share, even if
			// a stale presence record lingered. The primary gate is in ProveAvailability; this is the
			// second line.
			if requireVrf && m.VrfPubkey == "" {
				return false, nil
			}
			miners = append(miners, present{operator: m.Operator, stake: m.Stake})
			totalStake += m.Stake
		}
		return false, nil
	}); err != nil {
		return err
	}

	// Budget = a fraction of the current AvailPool, distributed pro rata to bond.
	if payoutBps > 0 && totalStake > 0 {
		poolBal, err := k.emissionKeeper.AvailPoolBalance(ctx)
		if err != nil {
			return err
		}
		// Budget stays in math.Int with an EXPLICIT guard before any uint64 conversion: never negative,
		// and bounded by poolBal since payoutBps<=10000. The split itself is done in big integers.
		budgetI := math.NewIntFromUint64(poolBal).Mul(math.NewIntFromUint64(payoutBps)).QuoRaw(10000)
		if !budgetI.IsNegative() && budgetI.IsPositive() {
			totalI := math.NewIntFromUint64(totalStake)
			for _, p := range miners {
				shareI := budgetI.Mul(math.NewIntFromUint64(p.stake)).Quo(totalI)
				if !shareI.IsPositive() || !shareI.IsUint64() {
					continue
				}
				share := shareI.Uint64()
				opBz, e := k.addressCodec.StringToBytes(p.operator)
				if e != nil {
					continue
				}
				if _, e := k.emissionKeeper.PayAvail(ctx, sdk.AccAddress(opBz), share); e != nil {
					// BEST-EFFORT, like every other payout in the EndBlocker (slashCheatedPrimary,
					// clawbackPayment, releaseHeld). Propagating this error would HALT THE CHAIN over a
					// single failed availability payout. Skip the miner, not the block.
					sdk.UnwrapSDKContext(ctx).Logger().Error("payAvailability: PayAvail failed -> miner skipped (best-effort)",
						"operator", p.operator, "epoch", epoch, "err", e.Error())
					continue
				}
			}
		}
	}

	// Purge the epoch's presence records, paid or not.
	for _, key := range allKeys {
		if err := k.Available.Remove(ctx, key); err != nil {
			return err
		}
	}
	return nil
}

// runAvailabilitySlash implements ADR-022: SLASHABLE LIVENESS, all bonded miners, proceeds BURNED. There is
// deliberately no solo semantic check — a public reference answer would be precomputable and would not
// prove that a GPU exists. Output quality stays guarded by the WORK audit and by AvailRequireDemand. At an
// epoch boundary, for EVERY bonded miner, being absent from the previous epoch's presence records — that
// is, not having answered the VRF proof on the unpredictable challenge IN TIME — counts as one FAILURE.
//
// SLIDING-WINDOW BITMASK. Per miner, a 64-bit BITMASK of the recent epochs (bit 0 = current epoch, bit i =
// i epochs ago), shifted left on every epoch processed. A slash fires when POPCOUNT(mask & window W) >=
// avail_fail_k, i.e. at least k absences in ANY span of W epochs, exactly. A tumbling window only counted
// within FIXED spans, so a chronic absentee could place (k-1) absences before a boundary and (k-1) after
// it WITHOUT being slashed — gaming the reset — and sustain (k-1)/W absence forever with the right timing.
// The sliding window removes that: there is no boundary left to play.
//
// Storage reuses AvailFailCount (now the bitmask) and AvailFailWindowStart (now the last epoch processed).
// Both are uint64, so no proto regeneration is required; the semantics may change because the slash is
// DORMANT everywhere and there is no armed state to migrate. CONSTRAINT: avail_fail_window <= 64 when
// armed, the capacity of the mask, enforced by Validate.
//
// The slash is bounded by min(stake*avail_slash_bps/10000, avail_slash_max) and BURNED: there is no victim
// to compensate here, so deterrence stays purely deflationary and no beneficiary has an incentive to
// over-slash. The mask is reset after a slash, granting k epochs of grace and bounding the slash rate.
// avail_fail_k>1 is the anti-false-positive rule: an isolated network or latency failure never slashes.
// The whole mechanism is DORMANT while avail_slash_bps==0, and Validate demands the complete machinery at
// activation. Epoch 0 (bootstrap, no challenge rolled yet) is exempt. Note that k and W MUST be
// recalibrated against a tumbling window, because the false-positive rate per evaluated epoch changes:
// see tokenomics/avail_slash_calibration_sliding.py and the artefact
// bench-results/avail-slash-calibration-sliding.json.
//
// LIMITS, stated plainly: (a) deterrence is ECONOMIC, not absolute — an adversary willing to absorb
// recurring slashes (5 % plus the unpaid absence) can sustain a density above (k-1)/W; (b) an unbonded
// miner carries NO obligation, and its off-bond epochs slide in as zeros, ageing exactly like presence, so
// unbonding is no laundering shortcut over being present; (c) ROTATING a minerId resets the mask, which
// existing friction limits (Demand reset and probation), and binding the mask to the operator instead
// would penalise legitimate multi-GPU setups.
func (k Keeper) runAvailabilitySlash(ctx context.Context, epoch int64, params types.Params) error {
	if params.AvailSlashBps == 0 || params.AvailFailK == 0 || params.AvailFailWindow == 0 {
		return nil // dormant
	}
	if epoch < 1 {
		return nil // epoch 0 = bootstrap: no challenge was rolled before the first boundary, nobody could prove
	}

	// Presence proofs delivered in time during the epoch that just ended.
	present := make(map[string]struct{})
	if err := k.Available.Walk(ctx, collections.NewPrefixedPairRange[int64, string](epoch),
		func(key collections.Pair[int64, string]) (bool, error) {
			present[key.K2()] = struct{}{}
			return false, nil
		}); err != nil {
		return err
	}

	e := uint64(epoch) // epoch being processed (>= 1)
	w := params.AvailFailWindow
	var winMask uint64
	if w >= 64 {
		w = 64 // bitmask capacity; Validate bounds the armed value to <= 64, this clamp is defensive
		winMask = ^uint64(0)
	} else {
		winMask = uint64(1)<<w - 1
	}
	var toSlash []string

	// First pass: update the bitmasks (sliding window). Miner entries are NOT mutated during the walk.
	if err := k.Miner.Walk(ctx, nil, func(minerId string, m types.Miner) (bool, error) {
		if m.Stake == 0 || m.Stake < params.MinStake {
			return false, nil // not bonded -> no availability obligation; skipped epochs slide in as zeros
		}
		var mask uint64
		last, errL := k.AvailFailWindowStart.Get(ctx, minerId)
		if errL == nil && last < e {
			gap := e - last // >= 1; greater than 1 if the miner was unbonded (epochs without obligation are zeros)
			if prev, errC := k.AvailFailCount.Get(ctx, minerId); errC == nil && gap < 64 {
				mask = prev << gap
			}
		}
		// errL != nil (first time) or last >= e (should not happen: epochs strictly increase) -> mask = 0
		if _, proved := present[minerId]; !proved {
			mask |= 1 // absent at epoch e
		}
		mask &= winMask // only the last W epochs count; older ones expire as the window slides
		if uint64(bits.OnesCount64(mask)) >= params.AvailFailK {
			toSlash = append(toSlash, minerId)
			mask = 0 // reset after a slash: k epochs of grace before another one, bounding the rate
		}
		// Anti-churn: a CLEAN miner (mask=0) stores NOTHING — its trace is purged instead of rewritten
		// every epoch, since no entry is equivalent to a zero mask (see errL above). The honest majority
		// costs NO state write per epoch; only miners with recent absences carry an entry.
		if mask == 0 {
			if errL == nil { // a trace existed -> purge it
				if err := k.AvailFailCount.Remove(ctx, minerId); err != nil {
					return true, err
				}
				if err := k.AvailFailWindowStart.Remove(ctx, minerId); err != nil {
					return true, err
				}
			}
			return false, nil
		}
		if err := k.AvailFailCount.Set(ctx, minerId, mask); err != nil {
			return true, err
		}
		if err := k.AvailFailWindowStart.Set(ctx, minerId, e); err != nil {
			return true, err
		}
		return false, nil
	}); err != nil {
		return err
	}

	// Second pass: apply the slashes (mutate Miner and BURN) AFTER the walk.
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	for _, minerId := range toSlash {
		m, err := k.Miner.Get(ctx, minerId)
		if err != nil {
			continue // miner disappeared in the meantime
		}
		amt := m.Stake * params.AvailSlashBps / 10000
		if mx := params.AvailSlashMax; mx > 0 && amt > mx {
			amt = mx
		}
		if amt == 0 {
			continue
		}
		m.Stake -= amt
		if err := k.Miner.Set(ctx, minerId, m); err != nil {
			return err
		}
		// BURN from the module account that holds the bonds, keeping the books balanced (sum of stakes ==
		// module balance) and the effect deflationary. BEST-EFFORT: propagating a burn failure would HALT
		// THE CHAIN inside the EndBlocker. The stake is already decremented, so a failed burn leaves the
		// module OVER-collateralised (balance > sum of stakes), which is the safe direction: no miner ends
		// up under-backed. Log and skip; do not stop the block.
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName,
			sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(amt)))); err != nil {
			sdkCtx.Logger().Error("ADR-022 availability: BurnCoins failed -> not burned (best-effort, module over-collateralised)",
				"miner", minerId, "amount", amt, "epoch", epoch, "err", err.Error())
			continue
		}
		sdkCtx.Logger().Info("ADR-022 availability: miner slashed for missed liveness, amount burned",
			"miner", minerId, "amount", amt, "epoch", epoch)
	}
	return nil
}
