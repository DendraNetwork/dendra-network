package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// HeldSummary returns the total, count and age of held payments. Since a sub-quorum defers instead of
// clawing back, "held is not lost" is the central promise of that path, and a promise nothing can
// contradict is worth nothing. This query provides the two results that would
// force the opposite conclusion:
//   - a growing `held_total`: deferral is blocking a significant share of the escrow;
//   - an `oldest_age_blocks` that only rises: the network is not healing and deferrals never clear
//     (the "if this message persists" of the logs, as a number that can be argued with).
//
// Age is read from HeldSince (strict twin of HeldFee). A held payment without a date (state written
// before the twin existed) counts in total and count but not in the age: no seniority is invented, only
// what is measured is exposed. The walk is O(number of held payments), not O(jobs), so it is bounded by
// the real load.
func (q queryServer) HeldSummary(ctx context.Context, req *types.QueryHeldSummaryRequest) (*types.QueryHeldSummaryResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	var total, count uint64
	oldestSince := uint64(0)
	oldestJob := ""
	hasDated := false
	if err := q.k.HeldFee.Walk(ctx, nil, func(jobId string, amt uint64) (bool, error) {
		total += amt
		count++
		if since, sErr := q.k.HeldSince.Get(ctx, jobId); sErr == nil {
			if !hasDated || since < oldestSince {
				oldestSince = since
				oldestJob = jobId
				hasDated = true
			}
		}
		return false, nil
	}); err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	resp := &types.QueryHeldSummaryResponse{HeldTotal: total, HeldCount: count}
	if hasDated {
		h := uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())
		if h > oldestSince {
			resp.OldestAgeBlocks = h - oldestSince
		}
		resp.OldestJobId = oldestJob
	}
	// T1, second pocket: escrowed dispute bonds. The bond of a deferred dispute (sub-quorum) stays with
	// the module without appearing anywhere, and "the bond stays claimable" is the kind of unverifiable
	// claim this query exists to remove, applied to the other pocket. A bond is escrowed iff the dispute
	// is open (marked, unresolved) and the field is unconsumed (the treasury and refund branches reset it
	// to 0). O(jobs) walk, accepted: a gRPC query outside consensus, the same cost as the ListJob that
	// the public proof endpoint already calls.
	if err := q.k.Job.Walk(ctx, nil, func(_ string, job types.Job) (bool, error) {
		if job.DisputeBond > 0 && jobIsDisputed(job.State) && !jobIsResolved(job.State) {
			resp.BondEscrowedTotal += job.DisputeBond
			resp.BondEscrowedCount++
		}
		return false, nil
	}); err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	// Third pocket: the jurors held by the exit guard. Under that guard, a seat anchored on an
	// unresolved audit locks its holder's stake. "Locked is not seized" is a promise of the same kind
	// as "held is not lost", and exit-after-vote must be decided on these numbers: measure before
	// loosening. The predicate mirrors hasOpenObligation (b), with the same "job resolved" filter and
	// the same effective fail closed (an unreadable job counts as locking), so the measurement states
	// what the guard does. `__humandispute` is skipped (its value is the disputer address, not seats).
	// Age is read from CommitteeSince (twin of anchorCommittee); an anchoring written before the twin
	// existed counts in the number but not in the age. O(anchored committees) walk, purged at
	// resolution (C1), so it is bounded by the audits actually open.
	locked := map[string]struct{}{}
	oldestAnchor := uint64(0)
	oldestAnchorJob := ""
	hasAnchorDate := false
	if err := q.k.AuditCommittee.Walk(ctx, nil, func(key, members string) (bool, error) {
		if strings.HasSuffix(key, humanDisputeSuffix) {
			return false, nil
		}
		if job, jErr := q.k.Job.Get(ctx, baseJobIdFromCommitteeKey(key)); jErr == nil && jobIsResolved(job.State) {
			return false, nil // audit closed: these seats hold nobody any more
		}
		for _, m := range strings.Split(members, ",") {
			if m != "" {
				locked[m] = struct{}{}
			}
		}
		if since, sErr := q.k.CommitteeSince.Get(ctx, key); sErr == nil {
			if !hasAnchorDate || since < oldestAnchor {
				oldestAnchor = since
				oldestAnchorJob = baseJobIdFromCommitteeKey(key)
				hasAnchorDate = true
			}
		}
		return false, nil
	}); err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	resp.JurorsLockedCount = uint64(len(locked))
	if hasAnchorDate {
		h := uint64(sdk.UnwrapSDKContext(ctx).BlockHeight())
		if h > oldestAnchor {
			resp.OldestLockAgeBlocks = h - oldestAnchor
		}
		resp.OldestLockJobId = oldestAnchorJob
	}
	// Sentinel: always non-zero, so always emitted even though the codec omits zero values. It is the
	// only way to make "measured 0" provable: without it, a report showing `jurors_locked: 0` cannot
	// tell "measured at zero" from "binary older than the field", for any pocket (the bond had the same
	// gap). Increment it for every pocket added to this query.
	resp.PocketsMeasured = heldSummaryPockets
	return resp, nil
}

// heldSummaryPockets is the number of pockets this binary measures in HeldSummary: 1 held fee
// (HeldFee), 2 escrowed bonds (T1), 3 locked jurors. The value is the client's read contract.
const heldSummaryPockets = 3
