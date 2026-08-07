package keeper

import (
	"context"
	"encoding/hex"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// SetDecentralizedSeed records the decentralized VRF seed (aggregated output of the vote extensions)
// at a given height. Called by the app PreBlocker — deterministic on every node (same injected commit
// + same anchored keys + same aggregation).
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

// SetDecentralizedSeedContributors records the NUMBER of contributors (validators with a valid VRF
// proof) to the decentralized seed at a height. Set by the PreBlocker (aggregateSeed). Feeds the
// anti-regression floor.
func (k Keeper) SetDecentralizedSeedContributors(ctx context.Context, height int64, n uint64) error {
	return k.DecentralizedSeedContributors.Set(ctx, height, n)
}

// GetDecentralizedSeedContributors returns the NUMBER of contributors at a height (0/false when absent).
func (k Keeper) GetDecentralizedSeedContributors(ctx context.Context, height int64) (uint64, bool) {
	n, err := k.DecentralizedSeedContributors.Get(ctx, height)
	if err != nil {
		return 0, false
	}
	return n, true
}

// MinVrfContributorPowerBps — DYNAMIC ⌈2N/3⌉ floor expressed in VOTING POWER (bps of the total power
// of the last commit): the decentralized seed is trustworthy only if the validators that contributed to
// it weigh >= 2/3 of the power — the same bar as BFT consensus itself. A CARDINAL floor is griefable
// (a sybil of dust-stake validators inflates N -> ⌈2N/3⌉ becomes unreachable -> permanent legacy
// fallback, i.e. anti-grinding switched off by a third party); measured in POWER, dust weighs nothing.
// A constant for now (making it governable requires a proto field, cf. PLAN-REGEN §5).
// IT IS NOT GATED BY committee_min_vrf_contributors. That parameter counts HEADS and is governed; this
// bar measures POWER and is not. A governed count must not be able to lift a BFT power bar, so the two
// are evaluated side by side: every seed taken on the decentralized path faces this one.
const MinVrfContributorPowerBps = 6667

// SetDecentralizedSeedContributorPower records the power share (bps) of the valid VRF contributors at a
// height. Set by the PreBlocker at the SAME site as the seed (never one without the other -> no staleness).
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

// DeleteDecentralizedSeedContributorPower prunes an OLD height (sliding anti-bloat window, cf. V8-N1).
func (k Keeper) DeleteDecentralizedSeedContributorPower(ctx context.Context, height int64) error {
	return k.DecentralizedSeedContributorPower.Remove(ctx, height)
}

// committeeBaseSeed returns the BASE seed of the committee. When committee_seed_source==1
// (DECENTRALIZED VRF) AND a decentralized seed exists at the current height (set by the PreBlocker from
// the aggregated vote extensions of ALL validators), that seed is used (hex) — committee randomness
// then becomes unforgeable and controlled by no single actor. Otherwise -> `fallback` (legacy
// behaviour: height:time at open, AppHash at reveal). Default 0 = legacy, so end-to-end flows are intact.
func (k Keeper) committeeBaseSeed(ctx context.Context, fallback string) string {
	seed, _ := k.committeeBaseSeedSourced(ctx, fallback)
	return seed
}

// committeeBaseSeedSourced — same thing, but ALSO reports whether the returned seed is the
// decentralized one (`true`) or the fallback (`false`).
//
// Why the caller needs to know. The fallback is `AppHash(H-1):h`: the proposer of block h knows it
// BEFORE proposing. And `ProcessProposal` accepts a proposal WITHOUT vote-extension injection. A
// hostile proposer can therefore choose, at each block it proposes, between the decentralized seed
// (unpredictable) and a seed it has already computed — and propose only when the draw suits it.
// Missing seeds are not only an infrastructure accident: the omission can be DELIBERATE.
//
// The answer is not to reject the block (rejecting in a loop would halt the chain for a reason the
// attacker controls): it is to DRAW NOTHING when the seed is not decentralized. The omission then
// stops paying, since it no longer moves any draw.
func (k Keeper) committeeBaseSeedSourced(ctx context.Context, fallback string) (string, bool) {
	p, err := k.Params.Get(ctx)
	if err != nil || p.CommitteeSeedSource != 1 {
		return fallback, false
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	if seed, ok := k.GetDecentralizedSeed(ctx, h); ok && len(seed) > 0 {
		// CONTRIBUTOR FLOOR: a seed that is PRESENT but UNDER-DECENTRALIZED must NOT draw a committee in
		// rewarded mode, otherwise a minority controls the randomness. Two bars, and they answer
		// different attacks, so they are INDEPENDENT:
		//   (1) COUNT >= committee_min_vrf_contributors — governed, dormant at 0. It is the only bar that
		//       refuses a seed produced by ONE validator on a set small enough for that validator to
		//       carry the majority of the power: such a seed satisfies (2) by construction.
		//   (2) contributor POWER >= ⌈2/3⌉ of the commit power (MinVrfContributorPowerBps) — the BFT bar,
		//       immune to dust sybils, and NOT conditioned on (1): a governed count of heads set to zero
		//       must not disarm a power bar that no parameter is meant to reach.
		if min := p.CommitteeMinVrfContributors; min > 0 {
			if n, _ := k.GetDecentralizedSeedContributors(ctx, h); n < min {
				sdkCtx.Logger().Error("SECURITY: decentralized VRF seed UNDER-DECENTRALIZED (contributors below the static floor) -> LEGACY fallback (anti-grinding INACTIVE for this draw); too few validators have anchored and supplied their VRF key.", "height", h, "contributors", n, "min_required", min)
				return fallback, false
			}
		}
		// The power share is written at the SAME site as the seed, so its ABSENCE is not "no opinion": it
		// says the seed came from code that never measured the share. A security bar reads that as a
		// failure, never as a pass.
		if bps, ok := k.GetDecentralizedSeedContributorPower(ctx, h); !ok || bps < MinVrfContributorPowerBps {
			sdkCtx.Logger().Error("SECURITY: decentralized VRF seed UNDER-WEIGHTED (contributor power below 2/3 of the commit) -> LEGACY fallback (anti-grinding INACTIVE for this draw); HEAVY validators have not anchored or supplied their VRF key.", "height", h, "contributor_power_bps", bps, "min_required_bps", uint64(MinVrfContributorPowerBps))
			return fallback, false
		}
		return hex.EncodeToString(seed), true
	}
	// VRF BOOTSTRAP — anti SILENT REGRESSION: committee_seed_source=1 REQUIRES a decentralized seed.
	// Its ABSENCE means anti-grinding is INACTIVE for THIS committee draw, so the legacy fallback is
	// NEVER taken SILENTLY. The alert is VISIBLE and the fallback stands, so liveness is not blocked —
	// no halt. Typical causes: no validator has anchored its VRF key yet (register-validator-vrf-key),
	// or vote extensions are inactive. On a REWARDED testnet this MUST NOT persist; monitoring this
	// alert is a precondition for opening.
	sdkCtx.Logger().Error("SECURITY: committee_seed_source=1 but NO decentralized VRF seed at this height -> LEGACY fallback (anti-grinding INACTIVE for this draw). Has any validator anchored its VRF key, and are vote extensions active?", "height", h)
	return fallback, false
}

// SetBlockHash (VE-01) records the block hash at a height (set by the PreBlocker when vote extensions
// are active). Serves as the block-bound VRF alpha and for deterministic re-verification.
func (k Keeper) SetBlockHash(ctx context.Context, height int64, hash []byte) error {
	return k.BlockHash.Set(ctx, height, hash)
}

// GetBlockHash (VE-01) returns the block hash at a height (ok=false when absent -> fall back to chaining).
func (k Keeper) GetBlockHash(ctx context.Context, height int64) ([]byte, bool) {
	h, err := k.BlockHash.Get(ctx, height)
	if err != nil {
		return nil, false
	}
	return h, true
}

// DeleteBlockHash (V8-N1) prunes the hash of an OLD height (sliding anti-bloat window on the KV store).
// No-op when absent. Only h and h-1 are read (alpha + chaining fallback), so pruning far behind is safe.
func (k Keeper) DeleteBlockHash(ctx context.Context, height int64) error {
	return k.BlockHash.Remove(ctx, height)
}

// DeleteDecentralizedSeed (V8-N1) prunes the seed of an OLD height (sliding anti-bloat window).
// No-op when absent. The seed is read only at the current height (committee) and at h-1 (alpha fallback).
func (k Keeper) DeleteDecentralizedSeed(ctx context.Context, height int64) error {
	return k.DecentralizedSeed.Remove(ctx, height)
}
