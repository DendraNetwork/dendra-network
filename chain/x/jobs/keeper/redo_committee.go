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

// ADR-033 — ANCHORED RE-ADJUDICATION COMMITTEE (the ADR-032 flaw recurring on a DIFFERENT path).
//
// What this file closes. ADR-032 authenticated the voting set of the VERDICT TALLY. It did not touch
// the "fresh committee" of `AdjudicateDispute`, which remained SELF-APPOINTED: any registered miner
// outside the original committee could post a re-commit `<jobId>__redo__<id>` and enter the tally.
// The gate was `len(fresh) >= CommitteeSize`, i.e. 3 IDENTITIES, and the majority was computed over
// `totalRedoStake` = the stake of the SELF-SELECTED voters only. Three identities at `min_stake`
// therefore held 100 % of the electorate, in both directions:
//
//   - FALSE SLASH: divergent re-commits -> the honest primary loses `slash_leak_bps` of its bond.
//   - SELF-VINDICATION: a cheater that was ACTUALLY slashed copies its own commit onto 3 sybils ->
//     strict majority -> restitution from the Treasury + bond + reward paid to the "disputer"
//     (itself). This second direction is theft, not a grievance: ADR-032 was a damage cap, here the
//     attacker cashes in.
//
// The fix is the same invariant applied at the right place: *voting requires having been DRAWN*.
// The re-adjudication committee is drawn and ANCHORED when the dispute opens (on a seed posterior to
// the original commits, hence not grindable by the challenger), stake-weighted, and only its members
// count. Without an anchor the path fails closed: neither slash nor restitution.
//
// Implementation note: the anchor reuses the `AuditCommittee` collection under the key
// `<jobId>__redo`. The two namespaces cannot collide (a real jobId never ends in `__redo`), and this
// avoids a proto regeneration.

// redoCommitteeDomain — domain separation. MUST differ from `auditCommitteeDomain`: otherwise, for
// equal seed and job, knowing the audit committee would reveal the re-adjudication committee.
const redoCommitteeDomain = "dendra/redo-committee/v1|"

// redoAnchorKey — anchor key of the fresh committee.
func redoAnchorKey(jobId string) string { return jobId + "__redo" }

// humanDisputeKey — marks that a HUMAN dispute (and not VRF sampling) opened this job.
//
// Why an explicit mark rather than "no anchored committee". The latter test conflates two situations
// that nothing else distinguishes once the job is `+disputed`: (a) the job was NEVER put in doubt,
// someone simply challenged it; (b) the job WAS sampled but the draw returned nobody (network too
// small). In (b) the payment is genuinely unverified and ADR-028 wants its price back; in (a) there
// is nothing to take back. Relying on the mere absence of an anchor would treat (b) as (a) and
// disarm that guard.
// humanDisputeSuffix — a single derivation of the suffix (humanDisputeKey, baseJobIdFromCommitteeKey
// and the HeldSummary measurement filter all share it; two copies would eventually diverge).
const humanDisputeSuffix = "__humandispute"

func humanDisputeKey(jobId string) string { return jobId + humanDisputeSuffix }

// isHumanDispute — was this job opened by a human dispute? (mark posted when the dispute opens)
func (k Keeper) isHumanDispute(ctx context.Context, jobId string) bool {
	raw, err := k.AuditCommittee.Get(ctx, humanDisputeKey(jobId))
	return err == nil && strings.TrimSpace(raw) != ""
}

// anchorRedoCommittee draws and ANCHORS the re-adjudication committee of a disputed job.
// Eligible: registered miners EXCEPT the primary (a miner does not judge itself), EXCEPT the
// disputer (nobody judges its own accusation) and EXCEPT the ORIGINAL committee (independence /
// anti-collusion).
// commitAuthorsOf — the miners that ACTUALLY produced the commits under dispute.
//
// ⛔ THIS IS NOT `assignedCommittee`, AND THE DIFFERENCE IS THE WHOLE POINT. The anti-collusion
// exclusion was built from the DRAW, while `CreateCommit` -- in the module's own words -- "requires
// neither that the miner be assigned to the job nor that it was DRAWN onto the committee". So the set
// that produced the contested result is `{mid : Commit[jobId__mid] exists}`, and the adjudication
// re-reads exactly that set when it tallies. MEASURED
// (redo_excludes_the_assigned_not_the_authors_test.go): ten miners, drawn committee [co3 co2 co9], and
// `co0` -- assigned to nothing -- anchors a commit because nothing forbids it. It then sat on the redo
// jury that re-judged its own result, and the vote filter did not exclude it either.
//
// The two sets are UNIONED rather than swapped: a drawn member that stayed silent must still not judge
// the job it was summoned to, and dropping the draw to gain the authors would trade one hole for
// another.
func (k Keeper) commitAuthorsOf(ctx context.Context, jobId string) (map[string]bool, error) {
	authors := map[string]bool{}
	pfx := jobId + "__"
	if err := k.Commit.Walk(ctx, commitRange(pfx), func(key string, _ types.Commit) (bool, error) {
		mid := strings.TrimPrefix(key, pfx)
		// `<jobId>__redo__<mid>` and `<jobId>__verdict__<mid>` share the prefix and are NOT authorship
		// of the contested result: they are the re-judgement itself. Excluding their writers would make
		// the second round exclude its own jurors.
		if strings.HasPrefix(mid, "redo__") || strings.HasPrefix(mid, "verdict__") {
			return false, nil
		}
		if mid != "" {
			authors[mid] = true
		}
		return false, nil
	}); err != nil {
		return nil, err
	}
	return authors, nil
}

func (k Keeper) anchorRedoCommittee(ctx context.Context, jobId, primaryId, disputerId string) error {
	orig, err := k.assignedCommittee(ctx, jobId, CommitteeSize)
	if err != nil {
		// Original committee not re-derivable -> no anchor. The callers fail closed.
		return err
	}
	// ADR-037 — FROZEN POOL, SAME ANCHOR AS THE TWO OTHER DRAWS (the job's, posted by OpenJob).
	// This draw is the most exposed of the three: the DISPUTER picks both the job AND the height at
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
	var cands []minerWeight
	if err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if key == primaryId || (disputerId != "" && key == disputerId) || orig[key] || authors[key] {
			return false, nil
		}
		if !k.minerInFrozenPool(ctx, jobId, key, freeze) {
			return false, nil
		}
		cands = append(cands, minerWeight{id: key, stake: m.Stake})
		return false, nil
	}); err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// SEED — it must be UNPREDICTABLE TO THE DISPUTER, and that is not automatic.
	// In the default configuration (`committee_seed_source=0`), `committeeBaseSeed` returns the
	// fallback as-is; a fallback derived from the height alone would be entirely predictable. The
	// disputer picks both the job AND the height at which it broadcasts its transaction: it could
	// enumerate heights off-chain until the draw seats its accomplices, then broadcast at the right
	// one. The VRF audit path has no such degree of freedom — its height is imposed by the sampling.
	//
	// Fix: bind the fallback to the BLOCK HASH of the opening height, which the disputer does not know
	// when it signs. Failing that (vote extensions inactive, so no BlockHash recorded), do NOT anchor:
	// a fail-closed adjudication — whose outcome is bounded and non-punitive — is preferable to a
	// committee the accuser chose.
	fallback := ""
	if bh, ok := k.GetBlockHash(ctx, h); ok && len(bh) > 0 {
		fallback = "dispute:" + hex.EncodeToString(bh)
	}
	seed := k.committeeBaseSeed(ctx, fallback)
	if seed == "" {
		sdkCtx.Logger().Error("SECURITY: no unpredictable seed available when the dispute opened (neither decentralized VRF nor block hash) -> re-adjudication committee NOT anchored. Drawing on a guessable seed would let the accuser choose its own judges.", "job_id", jobId, "height", h)
		return nil
	}

	size := auditCommitteeDrawSize
	// A committee smaller than the decision threshold would make adjudication inert.
	if size < CommitteeSize {
		size = CommitteeSize
	}
	members := drawMembersWithDomain(redoCommitteeDomain, seed, jobId, cands, size)
	if len(members) == 0 {
		// No eligible miner (network too small): anchor nothing rather than an empty set, which would
		// read as "everybody". Callers fail closed. But staying SILENT would be a trap: on a small
		// network permissionless adjudication is then durably unavailable and nothing would say so —
		// the operator would believe the mechanism is active. The pool is
		// N-(primary+disputer+original committee), so at least 6 registered miners are required.
		sdkCtx.Logger().Error("SECURITY: candidate pool too small to draw a re-adjudication committee -> permissionless adjudication UNAVAILABLE on this job (the audit timeout remains the way out). More registered miners are required.",
			"job_id", jobId, "candidates", len(cands), "seats_wanted", size)
		// A log is visible to the node operator, but it is neither queryable nor scrapeable by the
		// exporter/Grafana — and "permissionless adjudication is unavailable" is exactly a condition
		// one wants to see in monitoring. Hence the event alongside the log.
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"redo_committee_unavailable",
			sdk.NewAttribute("job_id", jobId),
			sdk.NewAttribute("candidates", strconv.Itoa(len(cands))),
			sdk.NewAttribute("seats_wanted", strconv.Itoa(size)),
		))
		return nil
	}
	// anchorCommittee records the seats AND the anchoring height: a redo juror is held exactly like an
	// audit juror, so its lock must be measured against the same counter.
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

// refundDisputeBond returns its bond to the disputer when a committee WAS convened but returned no
// verdict: the failure is then the network's, and the invariant "an infrastructure failure is never
// billed to an honest party" applies to a disputer too.
//
// It does NOT cover a dispute without a committee (job never sampled): there, nobody was prevented
// from answering, the dispute is simply unfounded and its bond goes to the Treasury — handled
// upstream in `resolveDisputedAudit`. Conflating the two would turn the bond into a refundable
// deposit, hence grieving into a free action.
//
// Sets `job.DisputeBond` to 0: that is what makes the operation non-replayable, including if
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
		// BEST-EFFORT, LIKE EVERY OTHER PAYOUT REACHED FROM THE ENDBLOCKER. This was the ONE bank
		// transfer in the whole EndBlocker path that propagated its error, and the consequence is not
		// a failed refund: an error returned from EndBlock aborts the block, every validator replays
		// the same block and hits the same failure, and the chain STOPS for everyone -- over a single
		// dispute bond. `availability.go` states the rule for its own payout and the rest of this file
		// follows it; this call did not.
		// The bond is NOT lost by skipping: it stays in the module account and `job.DisputeBond` keeps
		// its value, so the amount owed is still recorded on the job and a later pass can pay it.
		// Silently swallowing would be the other mistake, so the failure is logged AND emitted.
		sdk.UnwrapSDKContext(ctx).Logger().Error("refundDisputeBond: bank transfer failed -> bond LEFT IN ESCROW, block not aborted (best-effort)",
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

// countRedoResponders returns how many ANCHORED members of the redo committee actually re-committed
// for this job. The security property is the ⌈2/3⌉ RATIO, so the seat count K=15 is a pure LIVENESS
// setting that must be calibrated on data rather than guessed. Emitting `responders / seats` on every
// dispute that reaches its deadline yields exactly that data.
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

// redoCommitteeAllowed reads the anchor. `ok=false` means NO anchor, and the caller MUST then refuse
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
