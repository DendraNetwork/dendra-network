package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// AssignedCommittee (ADR-034) exposes the ASSIGNED committee of a job, ORDERED by the consensus sort:
// members[0] is the primary that settleOptimistic pays, and the committee of size k is the k-element
// prefix (the prefix property of a total order).
//
// This query EXISTS TO KILL THE REPLICA. While it did not exist, the client re-derived the draw in
// Python, and the two copies diverged twice: once on an empty seed, so the miner that was served was
// never paid, and once on pure-hash versus hash/stake ordering, masked for as long as all stakes were
// equal. The client now ASKS instead of modelling the protocol.
//
// REFUSAL SEMANTICS, intentional and documented in the proto: unknown job -> NotFound; seed not yet
// revealed -> FailedPrecondition. NEVER an empty list to mean "I do not know": a failure that is
// indistinguishable from the normal case is precisely the defect that cost the network every
// settlement it had.
func (q queryServer) AssignedCommittee(ctx context.Context, req *types.QueryAssignedCommitteeRequest) (*types.QueryAssignedCommitteeResponse, error) {
	if req == nil || strings.TrimSpace(req.JobId) == "" {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	has, err := q.k.Job.Has(ctx, req.JobId)
	if err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	if !has {
		return nil, status.Error(codes.NotFound, "unknown job")
	}
	members, err := q.k.assignedCommitteeOrdered(ctx, req.JobId, CommitteeSize)
	if err != nil {
		// Seed not revealed yet (deferred reveal still pending): the caller must WAIT and ask again —
		// not draw the committee itself, and not treat "empty" as a committee.
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if len(members) == 0 {
		// No miner registered: say why, rather than returning an ambiguous empty list.
		return nil, status.Error(codes.FailedPrecondition, "no registered miner -> no committee can be derived")
	}
	return &types.QueryAssignedCommitteeResponse{Members: members}, nil
}

// AuditCommittee (ADR-032) exposes the JURY ANCHORED on a job — the only list `auditVerdictTally`
// accepts a verdict from.
//
// ⛔ WHY IT EXISTS. Until 2026-08-11 this anchor had NO reader: `audit_committee/value/<jobId>` was
// reachable only through a raw `abci_query` with the prefix hardcoded by the caller. The one query
// with a similar name, `AssignedCommittee` above, answers who was assigned the WORK — and the
// confusion is not hypothetical: a 3-member work committee was read as the jury and declared unable
// to reach quorum, while the anchored jury had 12 members. A miner could not learn it was summoned,
// and nobody could audit a jury.
//
// REFUSAL SEMANTICS, same discipline as its neighbour: unknown job -> NotFound; no anchor ->
// NotFound with a message that says WHICH of the two facts holds. "No jury was drawn" and "the jury
// is empty" are different statements, and returning an empty list for the first would make the
// distinction unreadable: the same discipline that forbids reading an unreadable answer as a zero.
func (q queryServer) AuditCommittee(ctx context.Context, req *types.QueryAuditCommitteeRequest) (*types.QueryAuditCommitteeResponse, error) {
	if req == nil || strings.TrimSpace(req.JobId) == "" {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	has, err := q.k.Job.Has(ctx, req.JobId)
	if err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	if !has {
		return nil, status.Error(codes.NotFound, "unknown job")
	}
	// The redo jury (ADR-033) is anchored under a DISTINCT key. It is read through the same
	// primitive rather than a second query: one anchor, one reader, no naming convention recopied
	// into a caller that would drift the day the suffix changes.
	key := req.JobId
	if req.Redo {
		key = redoAnchorKey(req.JobId)
	}
	raw, err := q.k.AuditCommittee.Get(ctx, key)
	if err != nil {
		return nil, status.Error(codes.NotFound,
			"no jury anchored for this job (the audit was never opened, or was deferred for want of an eligible pool) — this is NOT an empty jury")
	}
	members := make([]string, 0, 16)
	for _, m := range strings.Split(raw, ",") {
		if m = strings.TrimSpace(m); m != "" {
			members = append(members, m)
		}
	}
	// The seating height is the anchor's twin. Its absence is reported as zero rather than as a
	// failure: an anchor written before the twin existed is old state, not a broken read, and
	// refusing the whole answer over it would hide the members the caller actually asked for.
	h, hErr := q.k.CommitteeSince.Get(ctx, key)
	if hErr != nil {
		h = 0
	}
	return &types.QueryAuditCommitteeResponse{Members: members, AnchoredHeight: h}, nil
}

// AuditDeferred (ADR-034 audit partition) returns the jobs SELECTED by the audit draw whose opening is
// DEFERRED — the AuditSelected marker: seed unavailable, or juror pool below the floor. This is the
// `deferred_no_jury` class of the audit partition, and the only one that cannot be derived from
// `list-job`, since a deferred job is indistinguishable from a job that was never sampled. Without it
// the partition sum cannot be checked, and a monitoring gate would read "0 false slashes" on a network
// that audits nothing.
func (q queryServer) AuditDeferred(ctx context.Context, req *types.QueryAuditDeferredRequest) (*types.QueryAuditDeferredResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	var ids []string
	if err := q.k.AuditSelected.Walk(ctx, nil, func(jobId string) (bool, error) {
		ids = append(ids, jobId)
		return false, nil
	}); err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	// `audits_opened` is the INDEPENDENT source the partition test checks against: a monotonic counter
	// incremented at opening, whether by a human dispute or by a first selection. Absent on a chain
	// older than the counter -> 0.
	opened, err := q.k.AuditsOpened.Get(ctx)
	if err != nil {
		opened = 0
	}
	return &types.QueryAuditDeferredResponse{JobIds: ids, AuditsOpened: opened}, nil
}
