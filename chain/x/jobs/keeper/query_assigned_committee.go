package keeper

import (
	"context"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// AssignedCommittee (ADR-034) exposes the assigned committee of a job, ordered by the consensus sort:
// members[0] is the primary that settleOptimistic pays, and the committee of size k is the k-element
// prefix (the prefix property of a total order).
//
// This query removes the need for a client-side replica. Before it, the client derived the draw again
// in its own code, and the two copies diverged twice: once on an empty seed, so the miner that served
// was never paid, and once on pure-hash versus hash/stake ordering, hidden for as long as all stakes
// were equal. The client now asks instead of modelling the protocol.
//
// Refusal semantics, intentional and documented in the proto: unknown job -> NotFound; seed not yet
// revealed -> FailedPrecondition. Never an empty list to mean "I do not know": a failure that cannot be
// told apart from the normal case once cost the network every settlement it had.
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
	members, err := q.k.assignedCommitteeOrdered(ctx, req.JobId, q.k.workCommitteeSize(ctx))
	if err != nil {
		// Seed not revealed yet (deferred reveal still pending): the caller must wait and ask again,
		// without drawing the committee itself or treating "empty" as a committee.
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if len(members) == 0 {
		// No miner registered: say why, rather than returning an ambiguous empty list.
		return nil, status.Error(codes.FailedPrecondition, "no registered miner -> no committee can be derived")
	}
	return &types.QueryAssignedCommitteeResponse{Members: members}, nil
}

// AuditCommittee (ADR-032) exposes the jury anchored on a job, the only list `auditVerdictTally`
// accepts a verdict from.
//
// Without it, `audit_committee/value/<jobId>` is reachable only through a raw `abci_query` with the
// prefix hardcoded by the caller. The query with a similar name, `AssignedCommittee` above, answers
// who was assigned the work, and the two are easy to confuse: a 3-member work committee was once read
// as the jury and declared unable to reach quorum, while the anchored jury had 12 members. Without
// this query a miner cannot learn it was summoned, and nobody can audit a jury.
//
// Refusal semantics, same as its neighbour: unknown job -> NotFound; no anchor -> NotFound with a
// message that says which of the two holds. "No jury was drawn" and "the jury is empty" are different
// statements, and returning an empty list for the first would hide the difference, like reading an
// unreadable answer as a zero.
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
	// The redo jury (ADR-033) is anchored under a separate key. It is read through the same primitive
	// rather than a second query: one anchor, one reader, and no naming convention copied into a caller
	// that would drift the day the suffix changes.
	key := req.JobId
	if req.Redo {
		key = redoAnchorKey(req.JobId)
	}
	raw, err := q.k.AuditCommittee.Get(ctx, key)
	if err != nil {
		return nil, status.Error(codes.NotFound,
			"no jury anchored for this job (the audit was never opened, or was deferred for want of an eligible pool) — this is not an empty jury")
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

// AuditDeferred (ADR-034 audit partition) returns the jobs selected by the audit draw whose opening is
// deferred for want of a jury, the AuditSelected marker (a draw waiting for a seed carries none). This
// is the `deferred_no_jury` class of the audit partition, and the only one that cannot be derived from
// `list-job`, since a deferred job looks like a job that was never sampled. Without it the partition
// sum cannot be checked, and a monitoring gate would read "0 false slashes" on a network that audits
// nothing.
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
	// `audits_opened` is the independent source the partition test checks against: a monotonic counter
	// incremented at opening, whether by a human dispute or by a first selection. It is 0 on a chain
	// older than the counter.
	opened, err := q.k.AuditsOpened.Get(ctx)
	if err != nil {
		opened = 0
	}
	return &types.QueryAuditDeferredResponse{JobIds: ids, AuditsOpened: opened}, nil
}
