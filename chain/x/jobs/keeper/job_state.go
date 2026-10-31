package keeper

import "strings"

// jobIsPaid is the settlement anti-replay predicate, shared by every settlement path (the job state is
// a string machine, pending a proto enum).
//
// Without a common predicate, a job settled through one path is not seen as settled by another:
// `settle_pay` writes `+settled` while `payout`/`settle_semantic` only tested for `+paid`, which allowed
// double payment; and `settle_job` tested `== "settled"` (strict), so an `open+paid` job slipped through
// and inflated `Demand`. One rule instead: a job counts as already settled as soon as it carries `paid`
// or `settled`, whichever path wrote it.
//
// `verified` / `finalized` are verdict markers (verification, finalisation), not payment, and are left
// out of this predicate on purpose: a verified or finalised job may still be paid once.
func jobIsPaid(state string) bool {
	return strings.Contains(state, "paid") || strings.Contains(state, "settled")
}

// jobEscrowReturned reports whether the expiry pass has already sent the client's escrow back
// (`abci.go` -> `expireUnservedJob`, marker "+expired+refunded").
//
// It is deliberately separate from `jobIsPaid`. Widening `jobIsPaid` would have been a one-line fix, and
// a wrong one: two of its six callers need the opposite reading. `msg_server_dispute.go` refuses to
// dispute a job that is not paid, and `audit_sampling.go` only samples jobs that are paid; an expired
// job has no primary and no commit, so it must enter neither. The same word would have meant "already
// disbursed" at four call sites and "settled, therefore actionable" at two.
//
// The refund empties the module of this job's escrow but leaves `job.Fee` intact and the state
// readable as "open+expired+refunded", which `jobIsPaid` does not match (it looks for "paid" or
// "settled"). Any disbursement path reached afterwards would compute an amount from `job.Fee` and send
// it from the module account, which no longer holds this job's coins, so the payment would come out
// of the other clients' escrows. Payout is permissionless, so anyone could drain the module once a
// single job had expired.
func jobEscrowReturned(state string) bool {
	return strings.Contains(state, "refunded")
}

// jobIsDisputed reports whether the job verdict has already been challenged. Dispute anti-replay:
// one dispute per job. The marker is appended to the state (`...+disputed`), like `paid`/`settled`.
func jobIsDisputed(state string) bool {
	return strings.Contains(state, "disputed")
}

// jobIsResolved reports whether the job dispute has already been adjudicated (resolution anti-replay).
func jobIsResolved(state string) bool {
	return strings.Contains(state, "resolved")
}

// jobIsClawed (anti-evasion) reports whether the optimistic payment was clawed back because the
// primary stayed silent: it never anchored its reveal `<jobId>__reveal__<primId>` before the deadline.
// A paid primary that does not reveal is the suspect, since it has already cashed in, so silence is
// treated as cheating rather than innocence. The marker is informational and sits beside "+resolved";
// "resolved" still carries the anti-replay.
func jobIsClawed(state string) bool {
	return strings.Contains(state, "clawed")
}

// jobIsOptimistic reports whether the job was settled in optimistic k=1 mode (settleOptimistic
// writes the "optimistic" marker). Such a job has a paid primary and no initial slash, so the audit
// judges it by comparison against a fresh committee (hard slash on divergence), unlike the redundant k=3
// path, which is already adjudicated at settlement time.
func jobIsOptimistic(state string) bool {
	return strings.Contains(state, "optimistic")
}

// jobK3Adjudicated reports whether this job's k=3 committee has already been judged, through any of the
// three redundant paths. All three slash the same cosine outliers:
//   - SettleSemantic (k=3)  -> writes `+paid`/`+settled`  (anti-replay already via jobIsPaid)
//   - VerifySemantic        -> writes `+verified`
//   - FinalizeJob           -> writes `+finalized`
//
// Without a common predicate each path only recognises its own marker: `settle -> verify` then
// `verify -> finalize` slash the same outlier again (80 % of the remainder on every pass, up to about
// 96 %), and these entry points are permissionless. One rule instead: as soon as any of these markers
// is present the committee has been judged and no other path may slash again.
// (Payment is still carried by jobIsPaid: a verified but unpaid job may still be paid once.)
func jobK3Adjudicated(state string) bool {
	return jobIsPaid(state) || strings.Contains(state, "verified") || strings.Contains(state, "finalized")
}
