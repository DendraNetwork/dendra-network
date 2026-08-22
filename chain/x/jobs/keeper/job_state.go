package keeper

import "strings"

// jobIsPaid is the settlement anti-replay predicate, SHARED by every settlement path (the job state is
// a string machine, pending a proto enum).
//
// Without a common predicate, a job settled through one path is not seen as settled by another:
// `settle_pay` writes `+settled` while `payout`/`settle_semantic` only tested for `+paid`, which allows
// DOUBLE PAYMENT; and `settle_job` tested `== "settled"` (strict), so an `open+paid` job slipped
// through and inflated `Demand`. One rule instead: a job counts as already settled as soon as it
// carries `paid` OR `settled`, whichever path wrote it.
//
// `verified` / `finalized` are VERDICT markers (verification, finalisation), not payment, and are
// deliberately OUTSIDE this predicate: a verified or finalised job may still be paid once.
func jobIsPaid(state string) bool {
	return strings.Contains(state, "paid") || strings.Contains(state, "settled")
}

// jobEscrowReturned reports whether the client's escrow has ALREADY been sent back by the expiry pass
// (`abci.go` -> `expireUnservedJob`, marker "+expired+refunded").
//
// IT IS DELIBERATELY SEPARATE FROM `jobIsPaid`, AND THAT SEPARATION IS THE WHOLE POINT. Widening
// `jobIsPaid` would have been the one-line fix, and it would have been wrong: two of its six callers
// require the OPPOSITE reading. `msg_server_dispute.go` refuses to dispute a job that is NOT paid, and
// `audit_sampling.go` only samples jobs that ARE paid — an expired job has no primary and no commit,
// so it must enter neither. The same word would have meant "already disbursed" at four call sites and
// "settled, therefore actionable" at two.
//
// WHAT IT GUARDS. The refund empties the module of THIS job's escrow but leaves `job.Fee` intact and
// the state readable as "open+expired+refunded", which `jobIsPaid` does not match (it looks for "paid"
// or "settled"). Any disbursement path reached afterwards would recompute an amount from `job.Fee` and
// send it from the module account — which no longer holds this job's coins. The payment would
// therefore be drawn from the escrows of the OTHER clients. Payout is permissionless, so this is not a
// theoretical ordering: it is a drain reachable by anyone once a single job has expired.
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

// jobIsClawed (ADR-028, anti-evasion) reports whether the optimistic payment was CLAWED BACK because the
// primary stayed SILENT — it never anchored its reveal `<jobId>__reveal__<primId>` before the deadline.
// A paid primary that does not reveal is the suspect: it has already cashed in, so silence is treated as
// cheating, not as innocence. The marker is informational and sits BESIDE "+resolved"; anti-replay stays
// carried by "resolved".
func jobIsClawed(state string) bool {
	return strings.Contains(state, "clawed")
}

// jobIsOptimistic (ADR-025) reports whether the job was settled in OPTIMISTIC k=1 mode (the "optimistic"
// marker is written by settleOptimistic). Such a job has a PAID primary and no initial slash, so the audit
// judges it by comparison against a fresh committee (hard slash on divergence) — unlike the redundant k=3
// path, which is already adjudicated at settlement time.
func jobIsOptimistic(state string) bool {
	return strings.Contains(state, "optimistic")
}

// jobK3Adjudicated reports whether this job's k=3 committee has ALREADY been judged, through ANY of the
// three redundant paths. All three slash the SAME cosine outliers:
//   - SettleSemantic (k=3)  -> writes `+paid`/`+settled`  (anti-replay already via jobIsPaid)
//   - VerifySemantic        -> writes `+verified`
//   - FinalizeJob           -> writes `+finalized`
//
// Without a COMMON predicate each path only recognises its own marker: `settle -> verify` then
// `verify -> finalize` re-slash the same outlier (80 % of the REMAINDER on every pass, up to ~96 %), and
// these entry points are permissionless. One rule instead: as soon as any of these markers is present the
// committee has been judged and no other path may slash again.
// (Payment stays carried by jobIsPaid: a verified but unpaid job may still be paid ONCE.)
func jobK3Adjudicated(state string) bool {
	return jobIsPaid(state) || strings.Contains(state, "verified") || strings.Contains(state, "finalized")
}
