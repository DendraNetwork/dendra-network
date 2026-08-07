package keeper

import (
	"context"
	"fmt"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// ADR-037 — FROZEN SELECTION POOL
//
// THE DEFECT THIS FILE CLOSES. The three draws used to re-read the LIVE pool (`k.Miner.Walk`) on every
// call, with no snapshot. The ordering is stake-weighted (`lessByStakeScore`), and that weighting does
// resist SPLITTING a stake: at constant total stake, splitting into 2/4/16 identities lowers the
// probability of taking slot 0 from 51.4% to 42.1 / 40.3 / 38.6%. Multiplying identities costs.
//
// It does NOT resist choosing the IDENTIFIER. Once the seed is public, an adversary tries `id` values
// offline until `h = sha256(seed|id)` is tiny; the score `h/stake` then wins with a negligible stake.
// With 1 stake against 1,000,000: 3.5% at 2^16 tries, 38% at 2^20, 94% at 2^24, ~100% at 2^40 — and at
// 2^40 the attacker holding 1 stake still wins 99.9% of the time against an honest miner holding a
// billion.
//
//	Hashing is free, stake is not. Any economic weighting is void if the adversary picks its
//	identifier AFTER seeing the seed.
//
// THE RULE, single and shared by the three draws:
//
//	m is drawable for job  <=>  MinerRegisteredHeight[m] <= JobPoolFreezeHeight[job]
//
// WHY NOT ONE ANCHOR PER DRAW — the obvious shape, which would yield larger juries.
// Absent a decentralized seed, `committeeBaseSeed` returns the fallback in every degraded case
// (`decentralized_seed.go`), and that fallback is `AppHash|height` (`abci.go`). The AppHash carried by
// the header of block H is the one of H-1: it is known BEFORE H is proposed. And `contributors=1` is
// the topology assumed at launch, so the fallback is the NORMAL regime. An attacker grinds its `id`
// offline and submits `create-miner` IN block H; an anchor set at H with `<=` accepts it. A per-draw
// anchor is only safe if one re-proves, FOR EVERY DRAW, that the seed is not derivable at the freeze
// height — a proof that depends on the seed SOURCE, which has already changed twice. Prefer the anchor
// that needs no hypothesis over the one that yields larger juries.
//
// WHY NOT `Job.DisputeHeight` — the first design, which added no state.
// `genesis.go` explicitly allows a NEGATIVE value, justified by "no business code tests its value".
// This predicate IS that business code: reusing a field whose safety is conditioned on nobody reading
// it removes the condition without touching the conclusion. And after an import the predicate was
// false FOR EVERYONE: `MinerRegisteredHeight = initH` for every miner, `DisputeHeight = initH - elapsed`
// for every disputed job, so `registered <= dispute` was false everywhere -> empty committees ->
// unbounded deferral. No red test, no error: audits that never close.
//
// THE GENERAL RULE (ADR-037 §3.b): two anchors that are COMPARED must be written AND re-armed by the
// SAME code, at the SAME height. That is what rules out `DisputeHeight`, and it is invisible from
// either of the two sites one reads. `InitGenesis` therefore carries an equality guard on the two
// init heights.

// jobPoolFreezeHeight returns the pool freeze height of `jobId`.
//
// NO DEFAULT, IN EITHER DIRECTION. A failed READ never grants a green light on a security criterion,
// and a query failure carries a sentinel DISTINCT from the type's zero value. Anchor absent => ERROR.
// Never "permissive": that would reopen exactly the hole. Never "restrictive" either: a silently
// emptied committee reads as a healthy network, and the deferral rule turns it into an unbounded
// deferral. A draw that cannot establish its anchor is DEFERRED, with an explicit reason.
func (k Keeper) jobPoolFreezeHeight(ctx context.Context, jobId string) (uint64, error) {
	h, err := k.JobPoolFreezeHeight.Get(ctx, jobId)
	if err != nil {
		return 0, fmt.Errorf("pool freeze height MISSING for job %q (ADR-037): draw DEFERRED, neither permissive nor restrictive. A job is only born through OpenJob, which sets this anchor; an imported job receives it at init", jobId)
	}
	return h, nil
}

// minerInFrozenPool reports whether `minerId` was registered before (or at) this job's pool freeze.
//
// `<=` IS LOAD-BEARING, NOT COSMETIC. The whole imported population carries `registered == freeze`
// (both anchors are re-armed to `initH` by the same code): with `<`, it would become ENTIRELY
// ineligible, on EVERY imported job, without a single error. A dedicated test locks the equality
// against a future "simplification".
//
// A MINER WITHOUT A REGISTRATION HEIGHT IS EXCLUDED, AND SAID SO. `create-miner` always dates, and
// import re-arms; a miner without a date is a state no normal path produces. It is not INCLUDED — a
// failed read never grants a green light on a security criterion. Nor does it FAIL the whole draw: a
// single abnormal entry would then freeze EVERY committee, which would hand out a stop switch. It is
// excluded and LOGGED: neither silent nor fatal.
func (k Keeper) minerInFrozenPool(ctx context.Context, jobId, minerId string, freeze uint64) bool {
	reg, err := k.MinerRegisteredHeight.Get(ctx, minerId)
	if err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"SECURITY: miner WITHOUT a registration height -> EXCLUDED from the frozen pool (ADR-037). No normal path produces this state (create-miner dates, InitGenesis re-arms): abnormal entry or incomplete migration.",
			"miner_id", minerId, "job_id", jobId)
		return false
	}
	return reg <= freeze
}
