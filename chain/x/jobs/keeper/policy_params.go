package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-048 item 6 — FOUR POLICY CONSTANTS BECOME BOUNDED PARAMETERS.
//
// Each was a Go constant with a comment admitting the debt ("to be made governable through params
// later", committee.go; "stays a Go constant until the next regeneration window", audit_committee.go).
// The regeneration happened at epoch 14, so the debt is paid here, on the module's standard dormant
// pattern: 0 selects the compiled value, which is why the constants stay — they ARE the defaults, and a
// genesis that omits the fields behaves byte for byte as before. `Params.Validate` bounds a posted value.
//
// ⚠️ ONE CONSTANT KEEPS ITS OLD NAME FOR A DIFFERENT JOB, AND THAT IS WHERE A MISREAD WOULD COST. Two
// sites used `CommitteeSize` not as the size of the work committee but as an absolute FLOOR on a redo
// jury (msg_server_adjudicate.go, redo_committee.go). Governing the work committee down to 1 — legitimate
// in optimistic mode, where only the primary is paid — must not lower a jury floor as a side effect, so
// those two sites read `redoQuorumFloor` instead, frozen at the value they always had.

// redoQuorumFloor — the absolute floor of a RE-ADJUDICATION jury. It was spelled `CommitteeSize` and
// equal to it; it is not the size of anything that is governed.
const redoQuorumFloor = 3

func effectiveJobExpiry(p types.Params) int64 {
	if p.JobExpiryBlocks > 0 {
		return int64(p.JobExpiryBlocks)
	}
	return jobExpiryBlocks
}

func effectiveWorkCommitteeSize(p types.Params) int {
	if p.WorkCommitteeSize > 0 {
		return int(p.WorkCommitteeSize)
	}
	return CommitteeSize
}

func effectiveStakeCapMultiple(p types.Params) uint64 {
	if p.AssignmentStakeCapMultiple > 0 {
		return p.AssignmentStakeCapMultiple
	}
	return assignmentStakeCapMultiple
}

func effectiveAuditDrawSize(p types.Params) int {
	if p.AuditCommitteeDrawSize > 0 {
		return int(p.AuditCommitteeDrawSize)
	}
	return auditCommitteeDrawSize
}

// workCommitteeSize — the size of the WORK committee for a caller that does not already hold the params.
// Unreadable params select the compiled value, the same answer `eligibleAsJuror` gives: it is the
// behaviour of a chain that never governed the field, never a refusal and never a guess.
func (k Keeper) workCommitteeSize(ctx context.Context) int {
	p, err := k.Params.Get(ctx)
	if err != nil {
		return CommitteeSize
	}
	return effectiveWorkCommitteeSize(p)
}
