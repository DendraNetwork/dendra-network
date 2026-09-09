package keeper

import (
	"context"
	"fmt"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
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
// enoughEligibleMinersToVerify — REFUSES to open a job that could never be verified.
//
// ⛔ WHY THIS IS A PRE-CONDITION AND NOT A WARNING. The pool a job draws from is frozen at its birth
// and never widens (ADR-037), and the audit jury comes out of that same pool minus the primary. So a
// job born while fewer than `audit_min_quorum + 1` miners are eligible is unverifiable FOREVER — not
// "until miners arrive". Measured on the chain destroyed on 2026-08-13: 42 jobs out of 51 in exactly
// that state, holding 272 406 udndr that no verdict, no timeout and no governance message could
// release. `deferAuditForSmallJury` then re-queues them without bound, which is correct on its own
// terms and never terminates.
//
// ⚠️ The floor is `audit_min_quorum + 1`, and the `+ 1` is load-bearing: the jury EXCLUDES the miner
// under audit, so `quorum` jurors require `quorum + 1` eligible miners. Two neighbouring thresholds
// are not derived from one another — that mistake was made publicly before, on this very number.
//
// ⛔ IT COUNTS AT THE ANCHOR, WITH THE POOL'S OWN PREDICATE, AND BOTH HALVES WERE WRONG FOR A DAY.
// This guard used to count `minerVitalAt(height)` at the OPENING height while the freeze went to
// `frozenPoolAnchor(height)` = h-1: it certified a population the frozen pool would not contain. A
// miner registered in the very block that opens the job was COUNTED here and EXCLUDED there, so a job
// could be born with `quorum + 1` announced and `quorum` available — the exact failure this function
// exists to forbid. Measured at quorum=4: "PRECHECK(h=100) err=<nil> | FREEZE=99 pool_members=4 need=5".
//
// AND PASSING THE ANCHOR ALONE DOES NOT FIX IT, which is the part that is easy to get wrong. Vitality
// is `height <= reg || height-reg <= window` (miner_vitality.go): a miner registered at 100 is vital
// at 99, because nothing can be stale before it exists. Membership is `reg <= freeze`
// (`minerInFrozenPool`). The two predicates are each right in their own role and point in OPPOSITE
// directions, so counting with only one of them is wrong at every height. Measured: "guard called AT
// THE ANCHOR (99): err=<nil> ; newcomer vitalAt(anchor)=true inFrozenPool=false".
//
// So it applies the SAME TWO PREDICATES the draw applies, in the same order, at the same anchor —
// `audit_committee.go` filters on `minerInFrozenPool` then `eligibleAsJurorInWindow`. Counting anything
// else means counting a jury that will not be drawn.
func (k Keeper) enoughEligibleMinersToVerify(ctx context.Context, jobId string, anchor uint64) error {
	p, err := k.Params.Get(ctx)
	if err != nil {
		return errorsmod.Wrap(sdkerrors.ErrLogic, "params unreadable: cannot tell whether this job could ever be verified")
	}
	// ⛔ THE FLOOR COMES FROM THE FUNCTION THE DRAW CONSULTS, NEVER FROM THE RAW PARAMETER.
	// This read `int(p.AuditMinQuorum) + 1` and ABSTAINED at zero, on the stated grounds that "there is
	// no number to compare against here". There is one: `effectiveSlashFloor` returns `auditSlashFloor()`
	// = 4 at zero, and it is the function the draw asks. So admission demanded nothing while the draw
	// demanded four. MEASURED (quorum_zero_pool_floor_test.go): in optimistic mode a job with four
	// eligible miners was ACCEPTED, its escrow taken and its pool frozen -- and pools freeze at birth and
	// never widen (ADR-037), so that job could never reach a verdict. The SAME pool with the quorum
	// governed to 4 -- the value the zero already selects -- was REFUSED. Two spellings of one number,
	// opposite answers.
	// The `+ 1` is unchanged and keeps its own reason: the primary is excluded from its own audit, so a
	// floor of N jurors needs N + 1 eligible miners. Two neighbouring thresholds are not derived from one
	// another; this one is derived from the floor the draw applies, which is the only one that decides.
	need := effectiveSlashFloor(p) + 1
	// Params read ONCE, exactly as the draw does: `eligibleAsJuror` would re-read them per miner.
	window := effectiveJurorFreshness(p)
	n := 0
	if err := k.Miner.Walk(ctx, nil, func(key string, _ types.Miner) (bool, error) {
		if !k.minerInFrozenPool(ctx, jobId, key, anchor) {
			return false, nil
		}
		if !k.eligibleAsJurorInWindow(ctx, key, anchor, window) {
			return false, nil
		}
		n++
		return n >= need, nil // stop as soon as the floor is cleared: no reason to walk the rest
	}); err != nil {
		return errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if n < need {
		return errorsmod.Wrapf(sdkerrors.ErrInvalidRequest,
			"job REFUSED: %d eligible miner(s) in the pool this job would freeze (anchored at height %d), %d needed (effective audit floor=%d, plus the miner under audit which the jury excludes). "+
				"A job opened now would freeze this pool permanently and could never reach a verdict — the refusal is what keeps it from being lost silently. "+
				"A miner registering in THIS block is not in it: the pool closes on the block before",
			n, anchor, need, effectiveSlashFloor(p))
	}
	return nil
}

// frozenPoolAnchor — the last height whose miner registrations belong to a pool frozen at `h`.
//
// ONE RULE, TWO CALL SITES, AND THE SAME REASON BOTH TIMES: a pool frozen at `h` must contain exactly
// the miners that were already there BEFORE block `h` — never one that appears DURING it. Block `h`
// is composed by a proposer, so "earlier in the same block" is proposer-chosen, not a fact of
// history. The anchor therefore sits strictly below `h`.
//
//	· InitGenesis (genesis.go): both ADR-037 anchors, because on a normal restart the InitChain
//	  header IS the first finalised block, so `initH` ties with a `create-miner` landing in it;
//	· OpenJob (msg_server_open_job.go): the job's own freeze, because the proposer can read the
//	  pending MsgOpenJob and insert its own `create-miner` into the very block that opens the job.
//
// The two are the same defect seen from two places, and `minerInFrozenPool` is `reg <= freeze`, so
// in both a TIE is enough — the newcomer does not need to be earlier, only equal:
//
//	AT IMPORT — the SDK puts the InitChain header at `initial_height = N+1` on a normal restart, and
//	N+1 is ALSO the first block CometBFT finalises, so a `create-miner` landing in it is dated exactly
//	the freeze. The exported genesis publishes `beaconMap`, so the seeds are known in advance: one
//	ground address, one transaction, and the attacker sits in the frozen pool of EVERY imported job.
//
//	AT OpenJob — with `committee_reveal_delay = 0` the beacon seed is fixed IN the opening block, a
//	configuration `Params.Validate` accepts. A proposer reading the pending MsgOpenJob therefore knows
//	the committee before it proposes, and one `create-miner` of its own in the same block is dated
//	exactly the freeze: it grinds an id offline until the draw puts it first, and the job it will be
//	paid for is the one it just let in. Reproduced: `AssignedCommitteeOrdered[0]` == the ground id.
//
// Shifting the anchor down by one closes it without a special case anywhere else:
//   - imported miner (anchor) <= imported job (anchor)            -> still eligible, as they must be;
//   - newcomer at the first tx height > anchor                     -> excluded, which is the fix;
//   - imported miner (anchor) <= a NEW job opened later (>= initH) -> still eligible on new work.
//
// `--for-zero-height` gives `initH = 0` while the first block is 1, so 0 is ALREADY strictly below it
// and the clamp keeps it — no underflow on a uint64 anchor, and no behaviour change on the path the
// runbook prescribes. The value names an ordinal, not necessarily a block anyone lived through; only
// its ORDER against registration heights is ever read.
//
// ⚠️ ONE SIDE EFFECT, STATED — AND CITED FROM THE FUNCTION THAT APPLIES IT, NOT FROM A CONSTANT.
// `MinerRegisteredHeight` also starts the eviction grace window, so an imported miner is dated one
// block earlier. The window is `effectivePruneWindow(p)` (miner_vitality.go): it returns the GOVERNED
// `miner_prune_blocks` whenever that is > 0, and only falls back to the compiled default. So the cost
// is one block in ABSOLUTE terms always, but its PROPORTION is whatever governance has set — measured
// at a governed window of 1, an imported miner's grace goes from 2 blocks to 1, which is half of it,
// not one part in 604800. The eviction still lands strictly after the import block in every case
// measured; what changes is how big "one block" is, and that is not ours to assume.
func frozenPoolAnchor(h int64) uint64 {
	if h <= 0 {
		return 0
	}
	return uint64(h - 1)
}

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
