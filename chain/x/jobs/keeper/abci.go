package keeper

import (
	"context"
	"encoding/hex"
	"fmt"
	"strconv"

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

	// EXPIRY OF UNSERVED JOBS — the pass that gives the client escrow a way out.
	// Collected first, applied after the Walk like every other pass here: mutating a collection while
	// iterating it is undefined.
	// BOUNDED, AND THE RANGE IS WHAT MAKES THE BOUND SAFE.
	//
	// The first version walked the exact height `h` with no cardinality limit, in an EndBlocker that
	// has NO GAS to stop it. Every appointment is written by an OpenJob, so a block full of OpenJob
	// lands that many refunds on a single height 100_000 blocks later -- each one a job read plus a
	// bank transfer, unbounded, in a block nothing can interrupt.
	//
	// Capping alone would have STRANDED the overflow: entries left at `h` are never looked at again
	// once the pass moves to `h+1`, which loses exactly the escrow this file exists to return. So the
	// range is `K1 <= h` (every appointment DUE, not only today's) and the cap is safe: what does not
	// fit stays where it is and is picked up by the next block, oldest first, since the pair codec
	// orders by height. The backlog drains at a bounded rate instead of arriving all at once.
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

	// BOUNDED AND DRAINING, FOR THE REASON THE DEFERRAL BELOW MAKES UNAVOIDABLE.
	//
	// This walk used to take the exact height `h` with no cap, which was survivable only because a
	// batch could never exceed one block's worth of OpenJob. The deferral added below breaks that:
	// deferring by a fixed stride makes every batch of the same residue class LAND ON THE SAME HEIGHT
	// and MERGE. Measured on a fabricated chain with a persistently non-decentralized seed:
	// 50 -> 100 -> 150 -> 200 -> 250 entries processed in a single EndBlock, growing without bound —
	// the exact defect the expiry pass closes twenty lines above, reintroduced by its own fix.
	//
	// Same shape as the expiry pass, and for the same reason: the range covers every DUE height
	// (`K1 <= h`), so capping cannot strand anything — what does not fit stays where it is and the
	// next block takes it, oldest first.
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
	// THE FREEZE MUST NOT ACCEPT A SEED THE PROPOSER COULD HAVE CHOSEN.
	//
	// This loop used to write `b.Seed = seed` without ever asking where `seed` came from, twelve
	// lines above the audit draw that refuses in exactly that case. The asymmetry was the whole
	// vulnerability: a proposer operating a miner can compute BOTH candidate seeds before proposing
	// -- the vote-extension aggregate it holds locally, and the fallback `AppHash(h-1):h`, which it
	// knows in advance -- derive the committee under each, and simply OMIT the extension envelope
	// when the fallback gives it the primary seat. Omitting costs nothing: ProcessProposal accepts a
	// block without the envelope. So the WORK committee was selectable at will while the AUDIT
	// committee was protected, and the work draw is the one that decides who gets paid.
	//
	// Deferral, not rejection, for the reason `audit_sampling.go` states: rejecting the block hands
	// the attacker a halt switch (propose without injection in a loop, get rejected in a loop).
	// Removing the BENEFIT costs the attacker everything and the network only latency -- the beacon
	// stays unfrozen, no committee is drawn, nothing moves, and the backlog drains as soon as an
	// honest proposer injects. Same wide stride, for the same write-amplification reason.
	if p, pErr := k.Params.Get(ctx); pErr == nil && p.CommitteeSeedSource == 1 && len(due) > 0 {
		if _, decentralized := k.committeeBaseSeedSourced(ctx, seed); !decentralized {
			for _, key := range due {
				// REMOVED AT ITS OWN HEIGHT. The range covers `K1 <= h`, so a late entry does not
				// necessarily live at `h`: a Remove on `Join(h, jobId)` would take nothing away, and the
				// rescheduling would then create a DUPLICATE -- immortal and cumulative.
				if err := k.PendingReveal.Remove(ctx, key); err != nil {
					return err
				}
				// AN APPOINTMENT WHOSE BEACON IS GONE IS CONSUMED, NOT DEFERRED.
				// The normal path below removes the key even when `Beacon.Get` fails; the deferral loop
				// never asked, so an orphaned entry (beacon deleted, job refunded) was rescheduled for
				// ever and swelled every merged batch.
				if _, bErr := k.Beacon.Get(ctx, key.K2()); bErr != nil {
					continue
				}
				if err := k.PendingReveal.Set(ctx, collections.Join(h+auditDeferStride, key.K2())); err != nil {
					return err
				}
			}
			sdkCtx.Logger().Error("SECURITY: no decentralized VRF seed at this height -> NO committee freeze. Reveals deferred; no job is assigned. Usual cause: no validator has anchored a VRF key yet, or vote-extensions are not enabled. If this message persists, the network assigns NOTHING.",
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
		// The seed has just landed: this is the FIRST moment the work committee is derivable, hence the
		// only one where anchoring it means anything (ADR-045, 12). Before it, there was nothing to anchor.
		k.anchorWorkCommittee(ctx, jobId)
		// At the key's REAL height, for the same reason as above.
		if err := k.PendingReveal.Remove(ctx, key); err != nil {
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
