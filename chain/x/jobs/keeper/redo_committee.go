package keeper

import (
	"context"
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// Anchored re-adjudication committee (the self-appointed jury flaw recurring on a different path).
//
// The anchored audit jury authenticated the voting set of the verdict tally. It did not touch the "fresh committee" of
// `AdjudicateDispute`, which remained self-appointed: any registered miner outside the original
// committee could post a re-commit `<jobId>__redo__<id>` and enter the tally. The gate was
// `len(fresh) >= CommitteeSize`, that is 3 identities, and the majority was computed over
// `totalRedoStake`, the stake of the self-selected voters only. Three identities at `min_stake`
// therefore held 100 % of the electorate, in both directions:
//
//   - False slash: divergent re-commits, and the honest primary loses `slash_leak_bps` of its bond.
//   - Self-vindication: a cheater that was actually slashed copies its own commit onto 3 sybils, gets
//     a strict majority, and collects restitution from the Treasury + bond + reward paid to the
//     "disputer" (itself). This second direction is theft: the verdict-side attack only did damage, here the attacker
//     cashes in.
//
// The fix is the same invariant applied in the right place: voting requires having been drawn. The
// re-adjudication committee is drawn and anchored when the dispute opens (on a seed that comes after
// the original commits, so the challenger cannot grind it), stake-weighted, and only its members
// count. Without an anchor the path fails closed: neither slash nor restitution.
//
// Implementation note: the anchor reuses the `AuditCommittee` collection under the key
// `<jobId>__redo`. The two namespaces cannot collide (a real jobId never ends in `__redo`), and this
// avoids a proto regeneration.

// redoCommitteeDomain separates the domains and must differ from `auditCommitteeDomain`: otherwise,
// for equal seed and job, knowing the audit committee would reveal the re-adjudication committee.
const redoCommitteeDomain = "dendra/redo-committee/v1|"

// redoAnchorKey is the anchor key of the fresh committee.
func redoAnchorKey(jobId string) string { return jobId + "__redo" }

// humanDisputeSuffix is the single derivation of the suffix (humanDisputeKey,
// baseJobIdFromCommitteeKey and the HeldSummary measurement filter all share it; two copies would
// eventually diverge). humanDisputeKey marks that a human dispute, not VRF sampling, opened the job.
//
// An explicit mark is used instead of "no anchored committee", because that test confuses two
// situations nothing else distinguishes once the job is `+disputed`: (a) the job was never put in
// doubt and someone simply challenged it; (b) the job was sampled but the draw returned nobody
// (network too small). In (b) the payment is really unverified and the anti-evasion rule wants its price back; in
// (a) there is nothing to take back. Relying on the mere absence of an anchor would treat (b) as (a)
// and disarm that guard.
const humanDisputeSuffix = "__humandispute"

func humanDisputeKey(jobId string) string { return jobId + humanDisputeSuffix }

// isHumanDispute reports whether this job was opened by a human dispute (mark posted when the dispute opens).
func (k Keeper) isHumanDispute(ctx context.Context, jobId string) bool {
	raw, err := k.AuditCommittee.Get(ctx, humanDisputeKey(jobId))
	return err == nil && strings.TrimSpace(raw) != ""
}

// commitAuthorsOf returns the miners that actually produced the commits under dispute.
//
// This differs from `assignedCommittee`, and the difference matters. The anti-collusion exclusion was
// built from the draw, while `CreateCommit` "requires neither that the miner be assigned to the job nor
// that it was drawn onto the committee". The set that produced the contested result is
// `{mid : Commit[jobId__mid] exists}`, and the adjudication reads exactly that set when it tallies.
// Measured (redo_excludes_the_assigned_not_the_authors_test.go): ten miners, drawn committee
// [co3 co2 co9], and `co0`, assigned to nothing, anchors a commit because nothing forbids it. It then
// sat on the redo jury that judged its own result again, and the vote filter did not exclude it
// either.
//
// The two sets are combined rather than swapped: a drawn member that stayed silent must still not
// judge the job it was summoned to, and dropping the draw to gain the authors would trade one hole for
// another.
func (k Keeper) commitAuthorsOf(ctx context.Context, jobId string) (map[string]bool, error) {
	authors := map[string]bool{}
	pfx := jobId + "__"
	if err := k.Commit.Walk(ctx, commitRange(pfx), func(key string, _ types.Commit) (bool, error) {
		if mid := strings.TrimPrefix(key, pfx); contestedSuffix(mid) {
			authors[mid] = true
		}
		return false, nil
	}); err != nil {
		return nil, err
	}
	return authors, nil
}

// contestedSuffix reports whether the part of a commit key after `<jobId>__` names the author of a
// contested commit. `<jobId>__redo__<mid>` and `<jobId>__verdict__<mid>` share the prefix and are not
// authorship of the contested result: they are the second judgement itself, and excluding their writers
// would make the second round exclude its own jurors.
func contestedSuffix(mid string) bool {
	return mid != "" && !strings.HasPrefix(mid, "redo__") && !strings.HasPrefix(mid, "verdict__")
}

// contestedAuthor reports whether `commitAuthorsOf(jobId)` holds `mid`, with one point read: the walk
// visits the key `<jobId>__<mid>` exactly when it is stored, and `contestedSuffix` filters both.
// `juryAnswer` reads it per re-commit and per seat, where a walk would decode every commit of the job.
func (k Keeper) contestedAuthor(ctx context.Context, jobId, mid string) (bool, error) {
	if !contestedSuffix(mid) {
		return false, nil
	}
	return k.Commit.Has(ctx, jobId+"__"+mid)
}

// anchorRedoCommittee draws and anchors the re-adjudication committee of a disputed job.
// Eligible: registered miners except the primary (a miner does not judge itself), except the disputer
// (nobody judges its own accusation) and except the original committee (independence and
// anti-collusion).
func (k Keeper) anchorRedoCommittee(ctx context.Context, jobId, primaryId, disputerId string) error {
	orig, err := k.assignedCommittee(ctx, jobId, k.workCommitteeSize(ctx))
	if err != nil {
		// Original committee not derivable: no anchor. The callers fail closed.
		return err
	}
	// Frozen pool, the same anchor as the two other draws (the job's, posted by OpenJob).
	// This draw is the most exposed of the three: the disputer picks both the job and the height at
	// which it broadcasts its transaction. Without the freeze it would only have to register ground
	// accomplices just before signing.
	freeze, fErr := k.jobPoolFreezeHeight(ctx, jobId)
	if fErr != nil {
		return fErr
	}
	authors, aErr := k.commitAuthorsOf(ctx, jobId)
	if aErr != nil {
		return aErr
	}
	// Presence is evaluated at the height the dispute is drawn. With unreadable params,
	// presence is unmeasured and the draw size stays at its compiled value, the ungoverned behaviour.
	prm, prmErr := k.Params.Get(ctx)
	hNow := uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())
	jf := judgeFilterOf(prm, prmErr == nil)
	var cands []minerWeight
	if err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if key == primaryId || (disputerId != "" && key == disputerId) || orig[key] || authors[key] {
			return false, nil
		}
		if !k.minerInFrozenPool(ctx, jobId, key, freeze) {
			return false, nil
		}
		if prmErr == nil && !k.minerPresentAt(ctx, key, hNow, prm) {
			return false, nil
		}
		// Epoch 15: the re-adjudication jury is drawn among declared judges that answer, with
		// the same predicate as the audit jury (judge.go), after the filters this draw already had.
		if !k.eligibleAsJudgeAt(ctx, key, hNow, jf) {
			return false, nil
		}
		cands = append(cands, minerWeight{id: key, stake: m.Stake})
		return false, nil
	}); err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// Seed: it must be unpredictable to the disputer, which is not automatic.
	// In the default configuration (`committee_seed_source=0`), `committeeBaseSeed` returns the
	// fallback as is, and a fallback derived from the height alone would be predictable. The
	// disputer picks both the job and the height at which it broadcasts its transaction: it could
	// enumerate heights off-chain until the draw seats its accomplices, then broadcast at the right
	// one. The VRF audit path has no such freedom, since the sampling imposes its height.
	//
	// The fallback is therefore bound to the block hash of the opening height, which the disputer
	// does not know when it signs. Failing that (vote extensions inactive, so no BlockHash recorded),
	// nothing is anchored: a fail-closed adjudication, whose outcome is bounded and non-punitive, is
	// preferable to a committee the accuser chose.
	fallback := ""
	if bh, ok := k.GetBlockHash(ctx, h); ok && len(bh) > 0 {
		fallback = "dispute:" + hex.EncodeToString(bh)
	}
	seed := k.committeeBaseSeed(ctx, fallback)
	if seed == "" {
		sdkCtx.Logger().Error("SECURITY: no unpredictable seed available when the dispute opened (neither decentralized VRF nor block hash) -> re-adjudication committee not anchored. Drawing on a guessable seed would let the accuser choose its own judges.", "job_id", jobId, "height", h)
		return nil
	}

	size := auditCommitteeDrawSize
	if prmErr == nil {
		size = effectiveAuditDrawSize(prm)
	}
	// A committee smaller than the decision threshold would make adjudication inert. The floor is the
	// redo quorum floor, kept apart from `CommitteeSize` because it must not follow a governed work
	// committee down (policy_params.go).
	if size < redoQuorumFloor {
		size = redoQuorumFloor
	}
	members := drawMembersWithDomain(redoCommitteeDomain, seed, jobId, cands, size)
	if len(members) == 0 {
		// No eligible miner (network too small): anchor nothing rather than an empty set, which would
		// read as "everybody". Callers fail closed. Staying silent would be a trap, though: on a small
		// network permissionless adjudication is then unavailable for a long time and nothing would say
		// so, so the operator would believe the mechanism is active. The pool is
		// N-(primary+disputer+original committee), so at least 6 registered miners are required.
		sdkCtx.Logger().Error("SECURITY: candidate pool too small to draw a re-adjudication committee -> permissionless adjudication unavailable on this job (the audit timeout remains the way out). More registered miners are required.",
			"job_id", jobId, "candidates", len(cands), "seats_wanted", size)
		// A log is visible to the node operator but cannot be queried or scraped by the exporter, and
		// "permissionless adjudication is unavailable" is a condition monitoring should see. Hence the
		// event alongside the log.
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"redo_committee_unavailable",
			sdk.NewAttribute("job_id", jobId),
			sdk.NewAttribute("candidates", strconv.Itoa(len(cands))),
			sdk.NewAttribute("seats_wanted", strconv.Itoa(size)),
		))
		return nil
	}
	// anchorCommittee records the seats and the anchoring height: a redo juror is held like an audit
	// juror, so its lock must be measured against the same counter.
	if err := k.anchorCommittee(ctx, redoAnchorKey(jobId), strings.Join(members, ",")); err != nil {
		return err
	}
	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"redo_committee_anchored",
		sdk.NewAttribute("job_id", jobId),
		sdk.NewAttribute("seats", strconv.Itoa(len(members))),
		sdk.NewAttribute("committee", strings.Join(members, ",")),
	))
	return nil
}

// refundDisputeBond returns its bond to the disputer when a committee was convened but returned no
// verdict: the failure is then the network's, and "an infrastructure failure is never billed to an
// honest party" applies to a disputer too.
//
// It does not cover a dispute without a committee (job never sampled): there, nobody was prevented
// from answering, the dispute is simply unfounded and its bond goes to the Treasury, handled upstream
// in `resolveDisputedAudit`. Treating the two alike would turn the bond into a refundable deposit, and
// grieving into a free action.
//
// It sets `job.DisputeBond` to 0, which makes the operation non-replayable, including if
// `AdjudicateDispute` ran afterwards (it would see no bond left to refund or confiscate).
// No-op when `dispute_bond=0` (the current setting) or when the disputer is unknown.
func (k Keeper) refundDisputeBond(ctx context.Context, job *types.Job) error {
	if job.DisputeBond == 0 || job.Disputer == "" {
		return nil
	}
	bz, err := k.addressCodec.StringToBytes(job.Disputer)
	if err != nil {
		return nil // unreadable address: nothing is burned, the funds stay with the module
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(job.DisputeBond)))
	if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(bz), coins); err != nil {
		// Best-effort, like every other payout reached from the EndBlocker. This was the one bank
		// transfer in the EndBlocker path that propagated its error, and the consequence was more than
		// a failed refund: an error returned from EndBlock aborts the block, every validator replays the
		// same block and hits the same failure, and the chain stops for everyone over a single dispute
		// bond. `availability.go` states the rule for its own payout and the rest of this file follows it.
		// The bond is not lost by skipping: it stays in the module account and `job.DisputeBond` keeps
		// its value, so the amount owed is still recorded on the job and a later pass can pay it.
		// Swallowing the error silently would be the other mistake, so the failure is logged and emitted.
		sdk.UnwrapSDKContext(ctx).Logger().Error("refundDisputeBond: bank transfer failed -> bond left in escrow, block not aborted (best-effort)",
			"job_id", job.JobId, "disputer", job.Disputer, "amount", job.DisputeBond, "err", err.Error())
		sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
			"dispute_bond_refund_failed",
			sdk.NewAttribute("job_id", job.JobId),
			sdk.NewAttribute("disputer", job.Disputer),
			sdk.NewAttribute("amount", fmt.Sprintf("%d", job.DisputeBond)),
		))
		return nil
	}
	job.DisputeBond = 0
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
		"dispute_bond_refunded",
		sdk.NewAttribute("job_id", job.JobId),
		sdk.NewAttribute("disputer", job.Disputer),
	))
	return nil
}

// countRedoResponders returns how many anchored members of the redo committee actually re-committed
// for this job. The security property is the ⌈2/3⌉ ratio, so the seat count K=15 is a liveness
// setting to calibrate on data rather than guess. Emitting `responders / seats` on every dispute that
// reaches its deadline yields that data.
func (k Keeper) countRedoResponders(ctx context.Context, jobId string, allowed map[string]bool) int {
	prefix := jobId + "__redo__"
	n := 0
	_ = k.Commit.Walk(ctx, commitRange(prefix), func(key string, _ types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) && allowed[strings.TrimPrefix(key, prefix)] {
			n++
		}
		return false, nil
	})
	return n
}

// redoCommitteeAllowed reads the anchor. `ok=false` means no anchor, and the caller must then refuse
// any decision that moves capital (hard slash as well as restitution).
func (k Keeper) redoCommitteeAllowed(ctx context.Context, jobId string) (map[string]bool, bool) {
	raw, err := k.AuditCommittee.Get(ctx, redoAnchorKey(jobId))
	if err != nil || strings.TrimSpace(raw) == "" {
		return nil, false
	}
	allowed := make(map[string]bool, 16)
	for _, id := range strings.Split(raw, ",") {
		if id = strings.TrimSpace(id); id != "" {
			allowed[id] = true
		}
	}
	if len(allowed) == 0 {
		return nil, false
	}
	return allowed, true
}
