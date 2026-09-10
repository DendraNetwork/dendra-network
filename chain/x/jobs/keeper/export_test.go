package keeper

import (
	"context"

	"cosmossdk.io/collections"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// export_test.go — test hooks, compiled ONLY by `go test` and never into the binary.
//
// The tests live in the external `keeper_test` package, so they cannot see unexported methods. This
// file exposes strictly what they need without widening the module's public surface in production:
// the standard Go pattern for testing an internal primitive.

// DrawAuditCommitteeForTest exposes `drawAuditCommittee` (ADR-032).
// It is tested directly because the DETERMINISM of the draw is a consensus property: two validators
// that drew different lists would anchor different values and fork the chain. That cannot be checked
// "through behaviour"; it is checked on the function itself.
func (k Keeper) DrawAuditCommitteeForTest(ctx context.Context, seed, jobId, primaryId, disputerId string) ([]string, error) {
	return k.drawAuditCommittee(ctx, seed, jobId, primaryId, disputerId)
}

// AuditDeferStrideForTest exposes the audit deferral stride.
//
// A test that hard-coded the value ("the job must be at h+1") would assert the SETTING rather than the
// property, and would break as soon as the stride is adjusted for cost reasons. By reading it, the
// test asserts what matters: the job is DEFERRED, to the height the code chose.
const AuditDeferStrideForTest = auditDeferStride

// AssignedCommitteeForTest exposes `assignedCommittee`, the PAYMENT path: settleOptimistic pays
// assignedCommittee(.,1). It is used to check the PREFIX property of the AssignedCommittee query:
// members[0] of the query MUST be the miner that path pays. That is client/chain parity itself, and a
// silent divergence there mis-settles every job on the network. Comparing the query to its own ordered
// implementation would be a tautology; it is compared to the payment path instead.
// FrozenPoolAnchorForTest exposes the pool ordinal so a case can assert the CONVENTION rather than
// re-derive it. A test that recomputed `h - 1` beside the code would agree with any future change to
// the rule, including a wrong one — it would test its own arithmetic, not the chain's.
func FrozenPoolAnchorForTest(h int64) uint64 { return frozenPoolAnchor(h) }

func (k Keeper) AssignedCommitteeForTest(ctx context.Context, jobId string, size int) (map[string]bool, error) {
	return k.assignedCommittee(ctx, jobId, size)
}

// AssignedCommitteeOrderedForTest exposes the ORDERED draw, whose element 0 is the primary.
//
// A set cannot answer "who was drawn FIRST", and that is the whole subject of the assignment ceiling
// (ADR-042): a capped weight does not remove a miner from the committee, it stops it taking slot 0 on
// every job. Asserting on the set would have measured membership while the defect lived in the order.
func (k Keeper) AssignedCommitteeOrderedForTest(ctx context.Context, jobId string, size int) ([]string, error) {
	return k.assignedCommitteeOrdered(ctx, jobId, size)
}

// AnchorCommitteeForTest exposes `anchorCommittee`.
//
// "Seats and date, together" is a property of the PRIMITIVE: both production sites (the audit draw in
// `audit_sampling.go` and the redo draw in `redo_committee.go`) go through it, and that single
// chokepoint is what guarantees no locked juror escapes the counter. Exercising it through the full
// lottery (VRF seed, reveal, eligible pool) would test the DRAW, not the pairing — and the draw
// already has its determinism test above. Same pattern as DrawAuditCommitteeForTest: the property is
// checked on the function, and the wiring of the two callers is checked by inspection.
func (k Keeper) AnchorCommitteeForTest(ctx context.Context, key, members string) error {
	return k.anchorCommittee(ctx, key, members)
}

// AnchorRedoCommitteeForTest exposes `anchorRedoCommittee` (ADR-033), the THIRD draw site.
// ADR-037 requires a failing-first test PER SITE: three pools, three tests, none inferred from the
// other two.
func (k Keeper) AnchorRedoCommitteeForTest(ctx context.Context, jobId, primaryId, disputerId string) error {
	return k.anchorRedoCommittee(ctx, jobId, primaryId, disputerId)
}

// RedoAnchorKeyForTest exposes the key under which the redo committee is anchored. Without it, a test
// can only re-read what `anchorRedoCommittee` wrote by recopying the naming convention — a SECOND
// source of truth that drifts the day the convention changes.
func RedoAnchorKeyForTest(jobId string) string { return redoAnchorKey(jobId) }

// CommitRangeForTest exposes `commitRange`.
//
// Bounding an iteration is a property of COST, not of result. The callers keep their
// `strings.HasPrefix` filter as defence in depth, so a broken range would still return the RIGHT
// result — by scanning the whole collection, which is exactly the defect the bound claims to have
// fixed. Only a test that counts the keys VISITED can tell "bounded" apart from "filtered afterwards",
// hence exposing the primitive itself.
func CommitRangeForTest(prefix string) *collections.Range[string] {
	return commitRange(prefix)
}

// ─── PUBLIC README CLAIMS ────────────────────────────────────────────────────────────────────────
//
// The public README states, in numbers: "A hard slash requires at least two thirds of the anchored
// seats to return \"invalid\"" and "With no anchored committee, no hard slash fires at all
// (fail-closed)". Both sentences are properties of PURE FUNCTIONS: the relative bar lives in
// `auditSlashDecision`, its floor in `effectiveSlashFloor`. Exercising them "through behaviour" would
// require standing up 15 jurors to read a `>=`, and would say nothing about the `seats == 0` case —
// precisely the case the second claim names, and the one whose denominator is zero. The release gate
// depends on these sentences being true, so they are measured on the functions.
func AuditSlashDecisionForTest(p types.Params, cheated bool, voters, invalidVotes, seats int) bool {
	return auditSlashDecision(p, cheated, voters, invalidVotes, seats)
}

func AuditVindicateDecisionForTest(p types.Params, valid bool, voters, validVotes, seats int) bool {
	return auditVindicateDecision(p, valid, voters, validVotes, seats)
}

func EffectiveSlashFloorForTest(p types.Params) int { return effectiveSlashFloor(p) }

// AuditSlashFloorForTest and AuditRelativeBarForTest expose the ABSOLUTE FLOOR and the RELATIVE BAR
// separately. Conflating the two is the defect this module has had to close twice; a test that only
// saw their composition could not say which of the two stopped moving.
func AuditSlashFloorForTest() int { return auditSlashFloor() }

func AuditRelativeBarForTest(p types.Params, seats int) int { return auditRelativeBar(p, seats) }

// AuditCommitteeDrawSizeForTest — the number of seats actually drawn (15). The public claim speaks of
// "two thirds of the anchored seats": without this value, a test that hard-codes 15 would measure its
// own figure rather than the chain's.
const AuditCommitteeDrawSizeForTest = auditCommitteeDrawSize

// HasOpenObligationForTest exposes the predicate BOTH exit paths consult -- `DeleteMiner` and the
// EndBlocker prune. It is exported because a bench that fabricates the end state (removing the record
// directly) proves nothing about the door: the test that used to cover eviction did exactly that and
// stayed green through a defect it was written to catch.
func (k Keeper) HasOpenObligationForTest(ctx context.Context, minerId string) (bool, error) {
	return k.hasOpenObligation(ctx, minerId)
}

// AnchorWorkCommitteeForTest drives the SHIPPED anchoring helper (ADR-045, entry 12). A bench that
// wrote the anchor itself would prove the READ path and say nothing about the WRITE path -- the
// difference this repository has paid for more than once.
func (k Keeper) AnchorWorkCommitteeForTest(ctx context.Context, jobId string) {
	k.anchorWorkCommittee(ctx, jobId)
}

// WorkCommitteeRawForTest exposes the stored anchor, so a bench can assert that the WRITE happened at
// the moment it claims -- not merely that a later read returned something plausible.
func (k Keeper) WorkCommitteeRawForTest(ctx context.Context, jobId string) (string, error) {
	return k.WorkCommittee.Get(ctx, jobId)
}

// UnwindStrandedRetentionForTest exposes the ADR-044 decision itself rather than its two call sites.
// A bench that drove only the callers would prove the WIRING and leave the decision untested in the
// one direction that matters: at `audit_unwind_blocks = 0` the function must refuse before reading
// anything, and "it refused" is indistinguishable from "the caller never asked" unless the decision is
// called directly.
func (k Keeper) UnwindStrandedRetentionForTest(ctx context.Context, h int64, jobId string, p types.Params) bool {
	return k.unwindStrandedRetention(ctx, h, jobId, p)
}
