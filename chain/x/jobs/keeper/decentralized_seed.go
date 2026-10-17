package keeper

import (
	"context"
	"encoding/hex"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// SetDecentralizedSeed records the decentralized VRF seed (aggregated output of the vote extensions)
// at a given height. The app PreBlocker calls it, deterministically on every node (same injected
// commit, same anchored keys, same aggregation).
func (k Keeper) SetDecentralizedSeed(ctx context.Context, height int64, seed []byte) error {
	return k.DecentralizedSeed.Set(ctx, height, seed)
}

// GetDecentralizedSeed returns the decentralized seed at a height (ok=false when absent).
func (k Keeper) GetDecentralizedSeed(ctx context.Context, height int64) ([]byte, bool) {
	s, err := k.DecentralizedSeed.Get(ctx, height)
	if err != nil {
		return nil, false
	}
	return s, true
}

// SetDecentralizedSeedContributors records the number of contributors (validators with a valid VRF
// proof) to the decentralized seed at a height. The PreBlocker sets it (aggregateSeed), and it feeds
// the anti-regression floor.
func (k Keeper) SetDecentralizedSeedContributors(ctx context.Context, height int64, n uint64) error {
	return k.DecentralizedSeedContributors.Set(ctx, height, n)
}

// GetDecentralizedSeedContributors returns the number of contributors at a height (0/false when absent).
func (k Keeper) GetDecentralizedSeedContributors(ctx context.Context, height int64) (uint64, bool) {
	n, err := k.DecentralizedSeedContributors.Get(ctx, height)
	if err != nil {
		return 0, false
	}
	return n, true
}

// MinVrfContributorPowerBps is the dynamic ⌈2N/3⌉ floor expressed in voting power (bps of the total
// power of the last commit): the decentralized seed is trustworthy only if the validators that
// contributed to it weigh >= 2/3 of the power, the same bar as BFT consensus itself. A cardinal floor
// can be griefed (a sybil of dust-stake validators inflates N, ⌈2N/3⌉ becomes unreachable, and the
// chain falls back to the legacy seed for good, so a third party switches anti-grinding off); measured
// in power, dust weighs nothing. It is a compiled constant (making it governable requires a new proto
// field).
// It is not gated by committee_min_vrf_contributors. That parameter counts heads and is governed; this
// bar measures power and is not. A governed count must not be able to lift a BFT power bar, so the two
// are evaluated side by side, and every seed taken on the decentralized path faces this one.
const MinVrfContributorPowerBps = 6667

// SetDecentralizedSeedContributorPower records the power share (bps) of the valid VRF contributors at a
// height. The PreBlocker sets it at the same site as the seed, never one without the other, so it cannot go stale.
func (k Keeper) SetDecentralizedSeedContributorPower(ctx context.Context, height int64, bps uint64) error {
	return k.DecentralizedSeedContributorPower.Set(ctx, height, bps)
}

// GetDecentralizedSeedContributorPower returns the power share (bps) at a height (ok=false when absent).
func (k Keeper) GetDecentralizedSeedContributorPower(ctx context.Context, height int64) (uint64, bool) {
	bps, err := k.DecentralizedSeedContributorPower.Get(ctx, height)
	if err != nil {
		return 0, false
	}
	return bps, true
}

// DeleteDecentralizedSeedContributors prunes an old height (sliding anti-bloat window, see V8-N1).
//
// This is the fourth collection the pruning covers. The contributor count is written at every
// height, so without this delete it would grow for the life of the chain while its three neighbours
// stay bounded: roughly one entry per block, forever, in a collection whose growth nothing would
// report.
func (k Keeper) DeleteDecentralizedSeedContributors(ctx context.Context, height int64) error {
	return k.DecentralizedSeedContributors.Remove(ctx, height)
}

// DeleteDecentralizedSeedContributorPower prunes an old height (sliding anti-bloat window, see V8-N1).
func (k Keeper) DeleteDecentralizedSeedContributorPower(ctx context.Context, height int64) error {
	return k.DecentralizedSeedContributorPower.Remove(ctx, height)
}

// committeeBaseSeed returns the base seed of the committee. When committee_seed_source==1
// (decentralized VRF) and a decentralized seed exists at the current height (set by the PreBlocker from
// the aggregated vote extensions of all validators), that seed is used (hex), and committee randomness
// becomes unforgeable and controlled by no single actor. Otherwise it returns `fallback` (legacy
// behaviour: height:time at open, AppHash at reveal). The default, 0, is legacy, so end-to-end flows
// are intact.
func (k Keeper) committeeBaseSeed(ctx context.Context, fallback string) string {
	seed, _ := k.committeeBaseSeedSourced(ctx, fallback)
	return seed
}

// committeeBaseSeedSourced does the same, and also reports whether the returned seed is the
// decentralized one (`true`) or the fallback (`false`).
//
// The caller needs to know because the fallback is `AppHash(H-1):h`, which the proposer of block h knows
// before proposing, and `ProcessProposal` accepts a proposal without vote-extension injection. A hostile
// proposer can therefore choose, at each block it proposes, between the decentralized seed
// (unpredictable) and a seed it has already computed, and propose only when the draw suits it. A
// missing seed can be deliberate, not only an infrastructure accident.
//
// Rejecting the block would let the attacker halt the chain in a loop. Instead, nothing is drawn when
// the seed is not decentralized, so the omission stops paying, since it no longer moves any draw.
func (k Keeper) committeeBaseSeedSourced(ctx context.Context, fallback string) (string, bool) {
	p, err := k.Params.Get(ctx)
	if err != nil || p.CommitteeSeedSource != 1 {
		return fallback, false
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	if seed, ok := k.GetDecentralizedSeed(ctx, h); ok && len(seed) > 0 {
		// Contributor floor: a seed that is present but under-decentralized must not draw a committee in
		// rewarded mode, otherwise a minority controls the randomness. There are two bars, and they answer
		// different attacks, so they are independent:
		//   (1) count >= committee_min_vrf_contributors, governed, dormant at 0. It is the only bar that
		//       refuses a seed produced by one validator on a set small enough for that validator to carry
		//       the majority of the power, a seed that satisfies (2) by construction.
		//   (2) contributor power >= ⌈2/3⌉ of the commit power (MinVrfContributorPowerBps), the BFT bar,
		//       immune to dust sybils and not conditioned on (1): a governed head count set to zero must not
		//       disarm a power bar that no parameter is meant to reach.
		if min := p.CommitteeMinVrfContributors; min > 0 {
			if n, _ := k.GetDecentralizedSeedContributors(ctx, h); n < min {
				sdkCtx.Logger().Error("SECURITY: decentralized VRF seed UNDER-DECENTRALIZED (contributors below the static floor) -> LEGACY fallback (anti-grinding inactive for this draw); too few validators have anchored and supplied their VRF key.", "height", h, "contributors", n, "min_required", min)
				return fallback, false
			}
		}
		// The power share is written at the same site as the seed, so its absence is not "no opinion": it
		// says the seed came from code that never measured the share. A security bar reads that as a
		// failure, never as a pass.
		if bps, ok := k.GetDecentralizedSeedContributorPower(ctx, h); !ok || bps < MinVrfContributorPowerBps {
			sdkCtx.Logger().Error("SECURITY: decentralized VRF seed UNDER-WEIGHTED (contributor power below 2/3 of the commit) -> LEGACY fallback (anti-grinding inactive for this draw); heavy validators have not anchored or supplied their VRF key.", "height", h, "contributor_power_bps", bps, "min_required_bps", uint64(MinVrfContributorPowerBps))
			return fallback, false
		}
		return hex.EncodeToString(seed), true
	}
	// VRF bootstrap, against a silent regression: committee_seed_source=1 requires a decentralized seed.
	// Its absence means anti-grinding is inactive for this committee draw, so the legacy fallback is
	// never taken silently. The alert is visible and the fallback stands, so liveness is not blocked and
	// the chain does not halt. Typical causes: no validator has anchored its VRF key yet
	// (register-validator-vrf-key), or vote extensions are inactive. On a rewarded testnet this must not
	// persist; monitoring this alert is a precondition for opening.
	sdkCtx.Logger().Error("SECURITY: committee_seed_source=1 but no decentralized VRF seed at this height -> LEGACY fallback (anti-grinding inactive for this draw). Has any validator anchored its VRF key, and are vote extensions active?", "height", h)
	return fallback, false
}

// SetBlockHash (VE-01) records the block hash at a height (set by the PreBlocker when vote extensions
// are active). It serves as the block-bound VRF alpha and for deterministic re-verification.
func (k Keeper) SetBlockHash(ctx context.Context, height int64, hash []byte) error {
	return k.BlockHash.Set(ctx, height, hash)
}

// GetBlockHash (VE-01) returns the block hash at a height (ok=false when absent, so callers fall back to chaining).
func (k Keeper) GetBlockHash(ctx context.Context, height int64) ([]byte, bool) {
	h, err := k.BlockHash.Get(ctx, height)
	if err != nil {
		return nil, false
	}
	return h, true
}

// DeleteBlockHash (V8-N1) prunes the hash of an old height (sliding anti-bloat window on the KV store).
// No-op when absent. Only h and h-1 are read (alpha and chaining fallback), so pruning far behind is safe.
func (k Keeper) DeleteBlockHash(ctx context.Context, height int64) error {
	return k.BlockHash.Remove(ctx, height)
}

// DeleteDecentralizedSeed (V8-N1) prunes the seed of an old height (sliding anti-bloat window).
// No-op when absent. The seed is read only at the current height (committee) and at h-1 (alpha fallback).
func (k Keeper) DeleteDecentralizedSeed(ctx context.Context, height int64) error {
	return k.DecentralizedSeed.Remove(ctx, height)
}
