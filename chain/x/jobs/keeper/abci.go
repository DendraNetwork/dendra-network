package keeper

import (
	"context"
	"encoding/hex"
	"fmt"
	"strconv"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// EndBlock runs the deferred committee reveal (H6, real anti-grinding) and the other per-block passes.
//
// At every block, the seed of every beacon whose reveal height has been reached is frozen from the
// AppHash of the current block. That AppHash results from executing blocks later than the job's open,
// so the creator could not predict it when choosing its jobId, and committee grinding gains nothing.
// Until the seed is frozen, CreateCommit and assignedCommittee refuse, with no fallback to a grindable
// seed. With no pending job (delay == 0, the default) the iteration is empty and the cost negligible.
func (k Keeper) EndBlock(ctx context.Context) error {
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// Block-wide seed: AppHash (unpredictable at open time) + height. Per-job uniqueness comes from
	// "|jobId" inside assignedCommittee, so two jobs revealed at the same block keep distinct committees.
	ah := hex.EncodeToString(sdkCtx.HeaderInfo().AppHash)
	if ah == "" {
		ah = "genesis" // fallback for an empty AppHash at the very start of the chain, the same guard as availability.go
	}
	seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%s:%d", ah, h))

	// Expiry of unserved jobs: the pass that gives the client escrow a way out.
	// Entries are collected first and applied after the Walk, like every other pass here: mutating a
	// collection while iterating it is undefined.
	// The pass is bounded, and the range is what makes the bound safe.
	//
	// The first version walked the exact height `h` with no cardinality limit, in an EndBlocker that
	// has no gas to stop it. Every appointment is written by an OpenJob, so a block full of OpenJob
	// lands that many refunds on a single height 100_000 blocks later, each one a job read plus a bank
	// transfer, unbounded, in a block nothing can interrupt.
	//
	// A cap alone would have stranded the overflow: entries left at `h` are never looked at again
	// once the pass moves to `h+1`, which loses the escrow this pass exists to return. So the range is
	// `K1 <= h` (every appointment due, not only today's) and the cap is safe: what does not fit stays
	// where it is and the next block picks it up, oldest first, since the pair codec orders by height.
	// The backlog drains at a bounded rate instead of arriving all at once.
	var expired []collections.Pair[int64, string]
	if err := k.PendingJobExpiry.Walk(ctx,
		new(collections.Range[collections.Pair[int64, string]]).
			EndExclusive(collections.PairPrefix[int64, string](h+1)),
		func(key collections.Pair[int64, string]) (bool, error) {
			expired = append(expired, key)
			return len(expired) >= jobExpiryMaxPerBlock, nil
		}); err != nil {
		return err
	}
	for _, key := range expired {
		if err := k.PendingJobExpiry.Remove(ctx, key); err != nil {
			return err
		}
		k.expireUnservedJob(ctx, key.K2(), h)
	}

	// Bounded and draining, for the reason the deferral below makes unavoidable.
	//
	// A walk of the exact height `h` with no cap works only while a batch never exceeds one block's
	// worth of OpenJob. The deferral below breaks that: deferring by a fixed
	// stride makes every batch of the same residue class land on the same height and merge. On a
	// fabricated chain whose seed stays centralized, an uncapped walk processes 50 -> 100 -> 150 -> 200 ->
	// 250 entries in a single EndBlock, growing without bound, the defect the expiry pass above closes.
	//
	// It has the same shape as the expiry pass, for the same reason: the range covers every due height
	// (`K1 <= h`), so the cap cannot strand anything; what does not fit stays where it is and the next
	// block takes it, oldest first.
	var due []collections.Pair[int64, string]
	if err := k.PendingReveal.Walk(ctx,
		new(collections.Range[collections.Pair[int64, string]]).
			EndExclusive(collections.PairPrefix[int64, string](h+1)),
		func(key collections.Pair[int64, string]) (bool, error) {
			due = append(due, key)
			return len(due) >= revealMaxPerBlock, nil
		}); err != nil {
		return err
	}
	// The freeze must not accept a seed the proposer could have chosen.
	//
	// Writing `b.Seed = seed` without asking where `seed` comes from, a few lines above
	// the audit draw that refuses in that case, would open a hole: a proposer that
	// runs a miner can compute both candidate seeds before proposing (the vote-extension aggregate it
	// holds locally, and the fallback `AppHash(h-1):h`, which it knows in advance), derive the committee
	// under each, and omit the extension envelope when the fallback gives it the primary seat. Omitting
	// costs nothing, since ProcessProposal accepts a block without the envelope. The work committee would
	// then be selectable at will while the audit committee is protected, and the work draw decides
	// who gets paid.
	//
	// Deferral rather than rejection, for the reason `audit_sampling.go` gives: rejecting the block
	// hands the attacker a halt switch (propose without injection in a loop, get rejected in a loop).
	// Removing the benefit costs the attacker everything and the network only latency: the beacon stays
	// unfrozen, no committee is drawn, nothing moves, and the backlog drains as soon as an honest
	// proposer injects. Same wide stride, for the same write-amplification reason.
	if p, pErr := k.Params.Get(ctx); pErr == nil && p.CommitteeSeedSource == 1 && len(due) > 0 {
		if _, decentralized := k.committeeBaseSeedSourced(ctx, seed); !decentralized {
			for _, key := range due {
				// Removed at its own height. The range covers `K1 <= h`, so a late entry does not
				// necessarily live at `h`: a Remove on `Join(h, jobId)` would remove nothing, and the
				// rescheduling would then create a duplicate that never dies and accumulates.
				if err := k.PendingReveal.Remove(ctx, key); err != nil {
					return err
				}
				// An appointment whose beacon is gone is consumed, not deferred.
				// The normal path below removes the key even when `Beacon.Get` fails; the deferral loop
				// never checked, so an orphaned entry (beacon deleted, job refunded) was rescheduled
				// forever and swelled every merged batch.
				if _, bErr := k.Beacon.Get(ctx, key.K2()); bErr != nil {
					continue
				}
				if err := k.PendingReveal.Set(ctx, collections.Join(h+auditDeferStride, key.K2())); err != nil {
					return err
				}
			}
			sdkCtx.Logger().Error("SECURITY: no decentralized VRF seed at this height -> no committee freeze. Reveals deferred; no job is assigned. Usual cause: no validator has anchored a VRF key yet, or vote-extensions are not enabled. If this message persists, the network assigns nothing.",
				"height", h, "jobs_deferred", len(due), "next_attempt", h+auditDeferStride)
			sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
				"committee_freeze_deferred",
				sdk.NewAttribute("height", strconv.FormatInt(h, 10)),
				sdk.NewAttribute("jobs", strconv.Itoa(len(due))),
				sdk.NewAttribute("reason", "seed is not decentralized"),
			))
			due = nil
		}
	}
	for _, key := range due {
		jobId := key.K2()
		if b, err := k.Beacon.Get(ctx, jobId); err == nil && b.Seed == "" {
			b.Seed = seed
			if err := k.Beacon.Set(ctx, jobId, b); err != nil {
				return err
			}
		}
		// The seed has just landed: this is the first moment the work committee is derivable, and the
		// only one where anchoring it means anything (ADR-045, 12).
		k.anchorWorkCommittee(ctx, jobId)
		// At the key's real height, for the same reason as above.
		if err := k.PendingReveal.Remove(ctx, key); err != nil {
			return err
		}
	}

	// ADR-025: optimistic audit draw. For every optimistically settled job scheduled at this height,
	// the chain challenges itself with probability audit_sample_bps: audit iff
	// H(seed || jobId) mod 10000 < bps. `seed` (the current block's AppHash, or a decentralised VRF)
	// comes after the commit, so it is unpredictable when the miner answers, the same anti-grinding
	// property as the deferred reveal. Dormant: a no-op when verification_mode != 1, since
	// PendingAudit is only fed by settleOptimistic, which is itself mode 1.
	if err := k.runOptimisticAudit(ctx, h, seed); err != nil {
		return err
	}
	// ADR-025 (liveness): resolve audits that were never honoured once their deadline is reached
	// (dormant when audit_resolve_timeout=0).
	if err := k.runAuditResolveTimeout(ctx, h); err != nil {
		return err
	}
	// ADR-028 appeal: a second deadline, for the permissionless late reveal of an honest
	// primary that was offline. Dormant when appeal_window=0 (PendingAppealResolve is then never fed).
	if err := k.runAppealResolveTimeout(ctx, h); err != nil {
		return err
	}

	// ADR-034: eviction of inert miners. The protocol knew how to punish a paid miner (`silence_slash`
	// lives in `clawbackPayment`) but not how to evict one that produces nothing: never paid, it was
	// never reached, while still occupying jury seats and capturing draws. The criterion is objective,
	// the removal non-discretionary, and the bond is returned.
	// Swept once per epoch only: the cost is proportional to the registry, not to traffic.
	if eb := int64(100); eb > 0 && h%eb == 0 {
		if err := k.pruneInactiveMiners(ctx, h); err != nil {
			return err
		}
		// Replay of retention releases still due after a failed transfer. Same cadence as the prune: the
		// cost is proportional to the number of stuck releases, never to traffic.
		if err := k.retryHeldReleases(ctx); err != nil {
			return err
		}
	}

	// Phase 1b, availability: at every epoch boundary, pay out the AvailPool to the miners proven
	// present (bond-weighted), then roll the challenge. Off by default (avail_epoch_blocks=0).
	return k.runAvailabilityEpoch(ctx, h)
}
