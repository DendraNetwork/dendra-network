package keeper

import (
	"context"
	"encoding/hex"
	"fmt"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// EndBlock -- DEFERRED COMMITTEE REVEAL (H6, real anti-grinding).
//
// At every block, the seed of every beacon whose reveal height has been reached is frozen from the
// AppHash of the CURRENT block. That AppHash results from executing blocks LATER than the job's open,
// so the creator could not predict it when choosing its jobId, and committee grinding becomes
// ineffective. Until the seed is frozen, CreateCommit and assignedCommittee refuse — there is no
// fallback to a grindable seed. With no pending job (delay == 0, the default) the iteration is empty
// and the cost is negligible.
func (k Keeper) EndBlock(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// Block-wide seed: AppHash (unpredictable at open time) + height. Per-job uniqueness comes from
	// "|jobId" inside assignedCommittee, so two jobs revealed at the same block keep distinct committees.
	ah := hex.EncodeToString(sdkCtx.HeaderInfo().AppHash)
	if ah == "" {
		ah = "genesis" // fallback for an empty AppHash at the very start of the chain -- same guard as availability.go
	}
	seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%s:%d", ah, h))

	// Collect the jobs due at THIS height (prefix = h), then apply: no mutation during the Walk.
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingReveal.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if b, err := k.Beacon.Get(ctx, jobId); err == nil && b.Seed == "" {
			b.Seed = seed
			if err := k.Beacon.Set(ctx, jobId, b); err != nil {
				return err
			}
		}
		if err := k.PendingReveal.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}

	// ADR-025 — OPTIMISTIC AUDIT DRAW. For every optimistically settled job scheduled at THIS height,
	// the chain challenges itself with probability audit_sample_bps: audit ⇔ H(seed‖jobId) mod 10000 <
	// bps. `seed` (the current block's AppHash, a decentralised VRF) is LATER than the commit, hence
	// unpredictable when the miner answers — same anti-grinding property as the deferred reveal.
	// DORMANT: a no-op when verification_mode != 1, since PendingAudit is only fed by settleOptimistic,
	// which is itself mode 1.
	if err := k.runOptimisticAudit(ctx, h, seed); err != nil {
		return err
	}
	// ADR-025 (liveness) — auto-resolves audits that were never honoured once their deadline is reached
	// (dormant when audit_resolve_timeout=0).
	if err := k.runAuditResolveTimeout(ctx, h); err != nil {
		return err
	}
	// PLAN-V2-FEE-HOLD §B — second, APPEAL deadline: the permissionless late reveal of an honest primary
	// that was offline. DORMANT when appeal_window=0 (PendingAppealResolve is then never fed).
	if err := k.runAppealResolveTimeout(ctx, h); err != nil {
		return err
	}

	// ADR-034 — EVICTION of inert miners. The protocol knew how to punish a PAID miner
	// (`silence_slash` lives in `clawbackPayment`) but not how to evict one that produces NOTHING:
	// never being paid, it was never reached, while still occupying jury seats and capturing draws. The
	// criterion is OBJECTIVE, the removal NON-DISCRETIONARY, and the bond is RETURNED.
	// Swept once per epoch only: the cost is proportional to the registry, not to traffic.
	if eb := int64(100); eb > 0 && h%eb == 0 {
		if err := k.pruneInactiveMiners(ctx, h); err != nil {
			return err
		}
		// REPLAY of retention releases still due after a failed transfer. Same cadence as the prune: the
		// cost is proportional to the number of stuck releases, never to traffic.
		if err := k.retryHeldReleases(ctx); err != nil {
			return err
		}
	}

	// Phase 1b -- availability: at every epoch boundary, pay out the AvailPool to the miners proven
	// present (bond-weighted), then roll the challenge. OFF by default (avail_epoch_blocks=0).
	return k.runAvailabilityEpoch(ctx, h)
}
