package keeper

import (
	"context"

	"cosmossdk.io/collections"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// export_test.go holds test hooks, compiled only by `go test` and never into the binary.
//
// The tests live in the external `keeper_test` package, so they cannot see unexported methods. This
// file exposes only what they need without widening the module's public surface in production, the
// standard Go pattern for testing an internal primitive.

// DrawAuditCommitteeForTest exposes `drawAuditCommittee`.
// It is tested directly because the determinism of the draw is a consensus property: two validators
// that drew different lists would anchor different values and fork the chain. That cannot be checked
// through behaviour; it is checked on the function itself.
func (k Keeper) DrawAuditCommitteeForTest(ctx context.Context, seed, jobId, primaryId, disputerId string) ([]string, error) {
	return k.drawAuditCommittee(ctx, seed, jobId, primaryId, disputerId)
}

// AuditDeferStrideForTest exposes the audit deferral stride.
//
// A test that hard-coded the value ("the job must be at h+1") would assert the setting rather than the
// property, and would break as soon as the stride is adjusted for cost reasons. By reading it, the
// test asserts what matters: the job is deferred, to the height the code chose.
const AuditDeferStrideForTest = auditDeferStride

// FrozenPoolAnchorForTest exposes the pool ordinal so a case can assert the convention rather than
// derive it again. A test that computed `h - 1` next to the code would agree with any future change to
// the rule, including a wrong one, testing its own arithmetic instead of the chain's.
func FrozenPoolAnchorForTest(h int64) uint64 { return frozenPoolAnchor(h) }

// AssignedCommitteeForTest exposes `assignedCommittee`, the payment path: settleOptimistic pays
// assignedCommittee(.,1). It checks the prefix property of the AssignedCommittee query: members[0] of
// the query must be the miner that path pays. That is client/chain parity, and a silent divergence
// there would mis-settle every job on the network. Comparing the query to its own ordered
// implementation would be a tautology, so it is compared to the payment path instead.
func (k Keeper) AssignedCommitteeForTest(ctx context.Context, jobId string, size int) (map[string]bool, error) {
	return k.assignedCommittee(ctx, jobId, size)
}

// AssignedCommitteeOrderedForTest exposes the ordered draw, whose element 0 is the primary.
//
// A set cannot answer "who was drawn first", and that is the subject of the assignment ceiling:
// a capped weight does not remove a miner from the committee, it stops it taking slot 0 on
// every job. Asserting on the set would have measured membership while the defect lived in the order.
func (k Keeper) AssignedCommitteeOrderedForTest(ctx context.Context, jobId string, size int) ([]string, error) {
	return k.assignedCommitteeOrdered(ctx, jobId, size)
}

// AnchorCommitteeForTest exposes `anchorCommittee`.
//
// "Seats and date, together" is a property of the primitive: both production sites (the audit draw in
// `audit_sampling.go` and the redo draw in `redo_committee.go`) go through it, and that single point is
// what guarantees no locked juror escapes the counter. Exercising it through the full lottery (VRF
// seed, reveal, eligible pool) would test the draw, not the pairing, and the draw already has its
// determinism test above. As with DrawAuditCommitteeForTest, the property is checked on the function,
// and the wiring of the two callers by inspection.
func (k Keeper) AnchorCommitteeForTest(ctx context.Context, key, members string) error {
	return k.anchorCommittee(ctx, key, members)
}

// AnchorRedoCommitteeForTest exposes `anchorRedoCommittee`, the third draw site.
// The pool freeze needs a test that fails first for each site: three pools, three tests, none inferred
// from the other two.
func (k Keeper) AnchorRedoCommitteeForTest(ctx context.Context, jobId, primaryId, disputerId string) error {
	return k.anchorRedoCommittee(ctx, jobId, primaryId, disputerId)
}

// RedoAnchorKeyForTest exposes the key under which the redo committee is anchored. Without it, a test
// could only read back what `anchorRedoCommittee` wrote by copying the naming convention, a second
// source of truth that drifts the day the convention changes.
func RedoAnchorKeyForTest(jobId string) string { return redoAnchorKey(jobId) }

// CommitRangeForTest exposes `commitRange`.
//
// Bounding an iteration is a property of cost, not of result. The callers keep their
// `strings.HasPrefix` filter as defence in depth, so a broken range would still return the right
// result, by scanning the whole collection, which is the defect the bound claims to have fixed. Only a
// test that counts the keys visited can tell "bounded" from "filtered afterwards", hence exposing the
// primitive itself.
func CommitRangeForTest(prefix string) *collections.Range[string] {
	return commitRange(prefix)
}

// Public README claims.
//
// The public README states, in numbers: "A hard slash requires at least two thirds of the anchored
// seats to return \"invalid\"" and "With no anchored committee, no hard slash fires at all
// (fail-closed)". Both sentences are properties of pure functions: the relative bar lives in
// `auditSlashDecision`, its floor in `effectiveSlashFloor`. Exercising them through behaviour would
// require standing up 15 jurors to read a `>=`, and would say nothing about the `seats == 0` case, the
// case the second claim names and whose denominator is zero. The release gate depends on these
// sentences being true, so they are measured on the functions.
func AuditSlashDecisionForTest(p types.Params, cheated bool, voters, invalidVotes, seats int) bool {
	return auditSlashDecision(p, cheated, voters, invalidVotes, seats)
}

func AuditVindicateDecisionForTest(p types.Params, valid bool, voters, validVotes, seats int) bool {
	return auditVindicateDecision(p, valid, voters, validVotes, seats)
}

func EffectiveSlashFloorForTest(p types.Params) int { return effectiveSlashFloor(p) }

// AuditSlashFloorForTest and AuditRelativeBarForTest expose the absolute floor and the relative bar
// separately. Confusing the two is a defect this module has had to close twice, and a test that only
// saw their composition could not say which of the two stopped moving.
func AuditSlashFloorForTest() int { return auditSlashFloor() }

func AuditRelativeBarForTest(p types.Params, seats int) int { return auditRelativeBar(p, seats) }

// AuditCommitteeDrawSizeForTest is the number of seats actually drawn (15). The public claim speaks of
// "two thirds of the anchored seats": without this value, a test that hard-codes 15 would measure its
// own figure rather than the chain's.
const AuditCommitteeDrawSizeForTest = auditCommitteeDrawSize

// HasOpenObligationForTest exposes the predicate both exit paths consult, `DeleteMiner` and the
// EndBlocker prune. It is exported because a test that fabricates the end state (removing the record
// directly) proves nothing about the exit path: such a test stays green through the very defect it is
// written to catch.
func (k Keeper) HasOpenObligationForTest(ctx context.Context, minerId string) (bool, error) {
	return k.hasOpenObligation(ctx, minerId)
}

// AnchorWorkCommitteeForTest drives the shipped anchoring helper. A test that wrote
// the anchor itself would prove the read path and say nothing about the write path.
func (k Keeper) AnchorWorkCommitteeForTest(ctx context.Context, jobId string) {
	k.anchorWorkCommittee(ctx, jobId)
}

// WorkCommitteeRawForTest exposes the stored anchor, so a test can assert that the write happened at
// the moment it claims, beyond a later read returning something plausible.
func (k Keeper) WorkCommitteeRawForTest(ctx context.Context, jobId string) (string, error) {
	return k.WorkCommittee.Get(ctx, jobId)
}

// UnwindStrandedRetentionForTest exposes the unwind decision itself rather than its two call sites.
// A test that drove only the callers would prove the wiring and leave the decision untested in the
// direction that matters: at `audit_unwind_blocks = 0` the function must refuse before reading
// anything, and "it refused" cannot be told apart from "the caller never asked" unless the decision is
// called directly.
func (k Keeper) UnwindStrandedRetentionForTest(ctx context.Context, h int64, jobId string, p types.Params) bool {
	return k.unwindStrandedRetention(ctx, h, jobId, p)
}

// Epoch 14.

// ReleaseHeldForTest drives finality itself. The cut is paid and the right credited on the branch that
// burns, and that branch has three callers (no audit drawn, vindication, retry sweep); a test driving
// one caller would prove the wiring of that caller and leave the payment untested on the other two.
func (k Keeper) ReleaseHeldForTest(ctx context.Context, job *types.Job) { k.releaseHeld(ctx, job) }

// RefundRetainedToClientForTest drives the refund composition directly, with the miner record the
// caller will persist: the Demand reversal is a mutation of that record, invisible in the store until
// the caller writes it.
func (k Keeper) RefundRetainedToClientForTest(ctx context.Context, job *types.Job, p types.Params, miner *types.Miner) uint64 {
	return k.refundRetainedToClient(ctx, job, p, miner)
}

// MinerPresentAtForTest exposes the presence rule, so its edges (the two-window grace, the newcomer,
// the miner with no date) are asserted on the rule and not through a draw that could hide them.
func (k Keeper) MinerPresentAtForTest(ctx context.Context, minerId string, height uint64, p types.Params) bool {
	return k.minerPresentAt(ctx, minerId, height, p)
}
