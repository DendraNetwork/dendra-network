package keeper

import (
	"context"
	"fmt"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// ADR-037: frozen selection pool.
//
// Without a snapshot, the three draws would read the live pool (`k.Miner.Walk`) on every call. The
// ordering is stake-weighted (`lessByStakeScore`), and that weighting does resist splitting a stake: at
// constant total stake, splitting into 2/4/16 identities lowers the probability of taking slot 0 from
// 51.4% to 42.1 / 40.3 / 38.6%. Multiplying identities costs.
//
// It does not resist choosing the identifier. Once the seed is public, an adversary tries `id` values
// offline until `h = sha256(seed|id)` is tiny, and the score `h/stake` then wins with a negligible
// stake. With 1 stake against 1,000,000: 3.5% at 2^16 tries, 38% at 2^20, 94% at 2^24, about 100% at
// 2^40, and at 2^40 the attacker holding 1 stake still wins 99.9% of the time against an honest miner
// holding a billion.
//
//	Hashing is free and stake is not. Any economic weighting is void if the adversary picks
//	its identifier after seeing the seed.
//
// The rule, single and shared by the three draws:
//
//	m is drawable for job  <=>  MinerRegisteredHeight[m] <= JobPoolFreezeHeight[job]
//
// Why not one anchor per draw, the obvious shape, which would give larger juries: without a
// decentralized seed, `committeeBaseSeed` returns the fallback in every degraded case
// (`decentralized_seed.go`), and that fallback is `AppHash|height` (`abci.go`). The AppHash carried by
// the header of block H is the one of H-1, known before H is proposed, and `contributors=1` is the
// topology assumed at launch, so the fallback is the normal regime. An attacker grinds its `id` offline
// and submits `create-miner` in block H, and an anchor set at H with `<=` accepts it. A per-draw anchor
// is only safe if one proves again, for every draw, that the seed is not derivable at the freeze
// height, a proof that depends on the seed source, which has already changed twice. The anchor that
// needs no hypothesis is preferred over the one that gives larger juries.
//
// Why not `Job.DisputeHeight`, the first design, which added no state: `genesis.go` explicitly allows
// a negative value, justified by "no business code tests its value". This predicate is that business
// code, so reusing the field would remove the condition its safety rests on. And after an import the
// predicate was false for everyone: `MinerRegisteredHeight = initH` for every miner and
// `DisputeHeight = initH - elapsed` for every disputed job, so `registered <= dispute` was false
// everywhere, giving empty committees and unbounded deferral, with no red test and no error.
//
// The general rule (ADR-037 §3.b): two anchors that are compared must be written and re-armed by the
// same code, at the same height. That rules out `DisputeHeight`, and it is invisible from either of
// the two sites taken alone. `InitGenesis` therefore writes both from a single value,
// `frozenPoolAnchor`.

// jobPoolFreezeHeight returns the pool freeze height of `jobId`.
//
// There is no default in either direction. A failed read never grants a green light on a security
// check, and a query failure carries a sentinel distinct from the type's zero value: a missing anchor
// is an error. A permissive default would reopen the hole. A restrictive one would be no better: a
// silently emptied committee reads as a healthy network, and the deferral rule turns it into an
// unbounded deferral. A draw that cannot establish its anchor is deferred, with an explicit reason.
func (k Keeper) jobPoolFreezeHeight(ctx context.Context, jobId string) (uint64, error) {
	h, err := k.JobPoolFreezeHeight.Get(ctx, jobId)
	if err != nil {
		return 0, fmt.Errorf("pool freeze height missing for job %q (ADR-037): draw deferred, neither permissive nor restrictive. A job is only born through OpenJob, which sets this anchor; an imported job receives it at init", jobId)
	}
	return h, nil
}

// enoughEligibleMinersToVerify refuses to open a job that could never be verified.
//
// It is a precondition rather than a warning because the pool a job draws from is frozen at its birth
// and never widens (ADR-037), and the audit jury comes out of that same pool minus the primary. A job
// born while fewer than `audit_min_quorum + 1` miners are eligible can never be verified, however many
// miners arrive later. On a chain where this happened, 42 of 51 jobs were stuck in that state, holding
// 272 406 udndr that no verdict, timeout or governance message could release. `deferAuditForSmallJury`
// then queues them again without bound, which is correct on its own terms and never terminates.
//
// The floor is `audit_min_quorum + 1`, and the `+ 1` matters: the jury excludes the miner under audit,
// so `quorum` jurors require `quorum + 1` eligible miners. Two neighbouring thresholds are not derived
// from one another.
//
// It counts at the anchor, with the pool's own predicate. Counting `minerVitalAt(height)` at the
// opening height, while the freeze goes to `frozenPoolAnchor(height)` = h-1, would certify a population
// the frozen pool does not contain: a miner registered in the block that opens the job would be counted
// here and excluded there, so a job could be born with `quorum + 1` announced and `quorum` available.
// At quorum=4, such a precheck at h=100 passes while the pool frozen at 99 holds 4 members for a need of 5.
//
// Passing the anchor alone does not fix it. Vitality is `height <= reg || height-reg <= window`
// (miner_vitality.go): a miner registered at 100 is vital at 99, because nothing can be stale before
// it exists. Membership is `reg <= freeze` (`minerInFrozenPool`). Each predicate is right in its own
// role and they point in opposite directions, so counting with only one of them is wrong at every
// height. Called at the anchor (99) with vitality alone, the guard passes: the newcomer is vital at the
// anchor and outside the frozen pool.
//
// So it applies the same two predicates as the draw, in the same order, at the same anchor
// (`audit_committee.go` filters on `minerInFrozenPool` then `eligibleAsJurorInWindow`). Counting
// anything else counts a jury that will not be drawn.
func (k Keeper) enoughEligibleMinersToVerify(ctx context.Context, jobId string, anchor uint64) error {
	p, err := k.Params.Get(ctx)
	if err != nil {
		return errorsmod.Wrap(sdkerrors.ErrLogic, "params unreadable: cannot tell whether this job could ever be verified")
	}
	// The floor comes from the function the draw consults, never from the raw parameter.
	// Reading `int(p.AuditMinQuorum) + 1` here would abstain at zero, on the grounds that "there is
	// no number to compare against". There is one: `effectiveSlashFloor` returns
	// `auditSlashFloor()` = 4 at zero, and it is the function the draw asks. Admission would demand
	// nothing while the draw demands four: in optimistic mode a job with four eligible miners would be
	// accepted, its escrow taken and its pool frozen, and since pools freeze at birth and never widen
	// (ADR-037), that job could never reach a verdict. The same pool with the quorum governed to 4, the
	// value the zero already selects, is refused: two spellings of one number would get opposite
	// answers. quorum_zero_pool_floor_test.go locks both cases.
	// The `+ 1` keeps its own reason: the primary is excluded from its own audit, so a floor of N
	// jurors needs N + 1 eligible miners. This threshold is derived from the floor the draw applies,
	// the only one that decides.
	need := effectiveSlashFloor(p) + 1
	// ADR-048 item 1: below the jury floor, a job can still open when nothing can be lost by it.
	//
	// The refusal above protects against one thing: a retention no jury can ever judge, held
	// forever. ADR-044's unwind removes that outcome: at `audit_unwind_blocks > 0` such a retention
	// goes back to the client once it is that old, the miner neither paid nor slashed. The refusal
	// then bought nothing and cost everything: a network below five live miners could serve no job
	// at all, so it could never grow past five.
	//
	// The floor drops to one eligible miner (someone must be there to serve) under three conditions,
	// all required, because each closes a different way of losing money below the floor:
	//   - optimistic mode: the only mode whose audit can unwind;
	//   - `audit_unwind_blocks > 0`: the unwind is armed, so an unjudgeable retention ends;
	//   - `hold_bps = 10000`: the whole miner payment is retained until finality. At a partial hold
	//     the part paid at settlement reaches the miner before any audit, and below the floor no jury
	//     could ever claw it back, an unverified payment the unwind cannot undo.
	// Below the floor an audited job therefore ends in exactly one way: the client is refunded after
	// `audit_unwind_blocks`. That is the announced price of opening early, and the client pays it.
	if p.VerificationMode == 1 && p.AuditUnwindBlocks > 0 && p.HoldBps >= 10000 {
		need = 1
	}
	// Params are read once, as the draw does: `eligibleAsJuror` would read them again per miner.
	window := effectiveJurorFreshness(p)
	n := 0
	if err := k.Miner.Walk(ctx, nil, func(key string, _ types.Miner) (bool, error) {
		if !k.minerInFrozenPool(ctx, jobId, key, anchor) {
			return false, nil
		}
		if !k.eligibleAsJurorInWindow(ctx, key, anchor, window) {
			return false, nil
		}
		// ADR-048 item 5: the same presence test the work draw applies at the same anchor: a miner
		// whose machine is off is not counted towards a pool it could not serve from.
		if !k.minerPresentAt(ctx, key, anchor, p) {
			return false, nil
		}
		n++
		return n >= need, nil // stop as soon as the floor is cleared: no reason to walk the rest
	}); err != nil {
		return errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if n < need {
		return errorsmod.Wrapf(sdkerrors.ErrInvalidRequest,
			"job refused: %d eligible miner(s) (and present, when availability windows are armed) in the pool this job would freeze (anchored at height %d), %d needed (effective audit floor=%d, plus the miner under audit which the jury excludes; one suffices when audit_unwind_blocks and a full hold are armed). "+
				"A job opened now would freeze this pool permanently and could never reach a verdict — the refusal is what keeps it from being lost silently. "+
				"A miner registering in this block is not in it: the pool closes on the block before",
			n, anchor, need, effectiveSlashFloor(p))
	}
	return nil
}

// frozenPoolAnchor returns the last height whose miner registrations belong to a pool frozen at `h`.
//
// One rule, two call sites, and the same reason both times: a pool frozen at `h` must contain exactly
// the miners that were already there before block `h`, never one that appears during it. Block `h`
// is composed by a proposer, so "earlier in the same block" is the proposer's choice, not a fact of
// history. The anchor therefore sits strictly below `h`.
//
//   - InitGenesis (genesis.go): both ADR-037 anchors, because on a normal restart the InitChain
//     header is the first finalised block, so `initH` ties with a `create-miner` landing in it;
//   - OpenJob (msg_server_open_job.go): the job's own freeze, because the proposer can read the
//     pending MsgOpenJob and insert its own `create-miner` into the block that opens the job.
//
// The two are the same defect seen from two places, and `minerInFrozenPool` is `reg <= freeze`, so
// in both a tie is enough: the newcomer does not need to be earlier, only equal:
//
//	At import: the SDK puts the InitChain header at `initial_height = N+1` on a normal restart,
//	and N+1 is also the first block CometBFT finalises, so a `create-miner` landing in it is dated
//	exactly the freeze. The exported genesis publishes `beaconMap`, so the seeds are known in
//	advance: one ground address, one transaction, and the attacker sits in the frozen pool of
//	every imported job.
//
//	At OpenJob: with `committee_reveal_delay = 0` the beacon seed is fixed in the opening block, a
//	configuration `Params.Validate` accepts. A proposer reading the pending MsgOpenJob therefore
//	knows the committee before it proposes, and one `create-miner` of its own in the same block is
//	dated exactly the freeze: it grinds an id offline until the draw puts it first, and the job it
//	will be paid for is the one it just entered. Reproduced: `AssignedCommitteeOrdered[0]` == the
//	ground id.
//
// Shifting the anchor down by one closes it without a special case anywhere else:
//   - imported miner (anchor) <= imported job (anchor): still eligible, as they must be;
//   - newcomer at the first tx height > anchor: excluded, which is the fix;
//   - imported miner (anchor) <= a new job opened later (>= initH): still eligible on new work.
//
// `--for-zero-height` gives `initH = 0` while the first block is 1, so 0 is already strictly below it
// and the clamp keeps it: no underflow on a uint64 anchor, and no behaviour change on the path the
// runbook prescribes. The value names an ordinal, not necessarily a block anyone lived through; only
// its order against registration heights is ever read.
//
// One side effect, cited from the function that applies it: `MinerRegisteredHeight` also starts the
// eviction grace window, so an imported miner is dated one block earlier. The window is
// `effectivePruneWindow(p)` (miner_vitality.go), which returns the governed `miner_prune_blocks`
// whenever that is > 0 and only falls back to the compiled default otherwise. The cost is always one
// block in absolute terms, but its proportion is whatever governance has set: measured at a governed
// window of 1, an imported miner's grace goes from 2 blocks to 1, half of it, not one part in 604800.
// The eviction still lands strictly after the import block in every case measured; only the relative
// size of "one block" changes, and it cannot be assumed.
func frozenPoolAnchor(h int64) uint64 {
	if h <= 0 {
		return 0
	}
	return uint64(h - 1)
}

// minerInFrozenPool reports whether `minerId` was registered before (or at) this job's pool freeze.
//
// The `<=` matters: the whole imported population carries `registered == freeze` (both anchors are
// re-armed from the same value by the same code), so with `<` it would become entirely ineligible, on
// every imported job, without a single error. A dedicated test locks the equality.
//
// A miner without a registration height is excluded, and the exclusion is logged. `create-miner`
// always dates and the import re-arms, so a miner without a date is a state no normal path produces.
// It is not included, since a failed read never grants a green light on a security check. Nor does it
// fail the whole draw: a single abnormal entry would then freeze every committee and hand out a stop
// switch. It is excluded and logged, neither silent nor fatal.
func (k Keeper) minerInFrozenPool(ctx context.Context, jobId, minerId string, freeze uint64) bool {
	reg, err := k.MinerRegisteredHeight.Get(ctx, minerId)
	if err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"SECURITY: miner without a registration height -> excluded from the frozen pool (ADR-037). No normal path produces this state (create-miner dates, InitGenesis re-arms): abnormal entry or incomplete migration.",
			"miner_id", minerId, "job_id", jobId)
		return false
	}
	return reg <= freeze
}
