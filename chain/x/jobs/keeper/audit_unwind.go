package keeper

import (
	"context"
	"strconv"

	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// unwindStrandedRetention — ADR-044, gesture 2. A RETENTION NO AUDIT CAN EVER CONCLUDE IS UNDONE,
// NOT DEFERRED AGAIN.
//
// WHAT IT CLOSES. Both deferral sites reschedule for ever, and that is deliberate: releasing to the
// miner after N attempts would make evasion free, and clawing back would punish an honest primary for
// a network failure. The price written into those two sites is that held escrow can grow without
// bound on a thin network. This function pays that price down WITHOUT reopening either hole — the
// CLIENT is refunded, the miner is neither paid nor slashed, its stake and the dispute bond stay
// intact. Silence yields ZERO instead of yielding a free pass.
//
// ⛔ DORMANT BY DEFAULT, AND DORMANT MEANS BYTE-IDENTICAL. At `audit_unwind_blocks = 0` the first
// branch returns before reading anything else, so every deferral behaves exactly as it did before this
// file existed. That is the property the bench drives, because "dormant by default" is otherwise an
// intention rather than a guarantee.
//
// ⛔ FAIL-CLOSED ON THE DATE, IN EVERY DIRECTION IT CAN FAIL. `HeldSince` unreadable, absent while a
// debt exists, or later than the current height: all three return false and the caller defers as
// today. An unknown age is never a licence to unwind. The whole point of the two deferral sites is
// that nobody loses to a network failure — and undoing a debt whose age we cannot establish would be
// exactly that, dressed up as a policy.
//
// ⭐ THE ORDER OF THE CHECKS IS THE CONTRACT, not a style. Every refusal happens BEFORE any mutation,
// and once `refundRetainedToClient` has moved money the function COMMITS: it returns true even if a
// later persistence fails, and says so loudly. Returning false after the refund would let the caller
// defer a job that has already been settled — refunded AND rescheduled, the one outcome worse than
// either branch alone.
//
// Returns true when the retention was unwound and the caller must NOT defer.
func (k Keeper) unwindStrandedRetention(ctx context.Context, h int64, jobId string, p types.Params) bool {
	// (0) DORMANT. Read nothing, touch nothing, decide nothing.
	if p.AuditUnwindBlocks == 0 {
		return false
	}
	// (1) IS THERE A DEBT AT ALL? No retention means there is nothing to unwind; the deferral is then
	// about the audit itself and must proceed untouched. A read error is treated as "no" for the same
	// reason as the date below: not knowing is not a licence.
	held, errHeld := k.HeldFee.Get(ctx, jobId)
	if errHeld != nil || held == 0 {
		return false
	}
	// (2) THE AGE. `HeldSince` is the twin key of `HeldFee`; a debt without its date is precisely the
	// case this must refuse, because the age is the ONLY thing that distinguishes "stranded" from
	// "still being verified".
	since, errSince := k.HeldSince.Get(ctx, jobId)
	if errSince != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("ADR-044: retention held with NO readable date -> deferred as before, never unwound. An age that cannot be established is not an age.",
			"job_id", jobId, "held", held)
		return false
	}
	if h < 0 {
		return false // a negative height is not a height; refuse rather than convert it.
	}
	now := uint64(h)
	if now < since {
		// A date in the future. Subtracting would wrap around on uint64 and yield an ENORMOUS age —
		// the zero-value trap in its arithmetic form: the unwind would fire on the youngest debts.
		sdk.UnwrapSDKContext(ctx).Logger().Error("ADR-044: held_since is AHEAD of the current height -> deferred. The subtraction would wrap and read as a very old debt.",
			"job_id", jobId, "held_since", since, "height", h)
		return false
	}
	if now-since < p.AuditUnwindBlocks {
		return false // too young: the audit still has time to conclude, which is the normal case.
	}
	// (3) THE JOB. Without it there is nothing to mark and no client to name; refuse before mutating.
	job, errJob := k.Job.Get(ctx, jobId)
	if errJob != nil {
		return false
	}
	// The primary MAY be absent — evicted, or carried in by an import. `refundRetainedToClient`
	// tolerates a nil miner (only the Demand reversal is skipped), and the client is refunded either
	// way: the retention belongs to the client, not to the miner's continued existence.
	var miner *types.Miner
	if m, errMiner := k.Miner.Get(ctx, job.MinerId); errMiner == nil {
		miner = &m
	}

	// ── PAST THIS LINE THE FUNCTION COMMITS ──────────────────────────────────────────────────────
	refunded := k.refundRetainedToClient(ctx, &job, p, miner)
	if miner != nil {
		if err := k.Miner.Set(ctx, miner.MinerId, *miner); err != nil {
			// Best-effort, and SAID. The reversed Demand not persisting leaves a stale figure, which
			// is a reporting defect; returning false here would leave a settled job scheduled for
			// another audit, which is a correctness defect. We take the smaller one and name it.
			sdk.UnwrapSDKContext(ctx).Logger().Error("ADR-044: miner not persisted after unwind (Demand reversal lost) -> the unwind STANDS, the figure is stale.",
				"job_id", jobId, "miner_id", job.MinerId, "err", err.Error())
		}
	}
	// ⭐ THE DISPUTE BOND IS RETURNED, AND THE MODULE'S OWN RULE IS WHAT SAYS SO. `resolveDisputedAudit`
	// states it in full: "RETURN when the disputer is right or when no instance remains; CONFISCATE
	// when it is wrong; ESCROW while an instance is still possible." An unwind is precisely the case
	// where NO INSTANCE REMAINS — no committee will ever rule on this job. Escrowing it here, on a job
	// that is now resolved, would strand the bond for ever on a state nothing revisits; confiscating it
	// would sanction a disputer no committee ever found wrong. ADR-044's "bond intact" and that rule
	// agree, and this is the branch where they meet.
	if job.DisputeBond > 0 {
		if err := k.refundDisputeBond(ctx, &job); err != nil {
			sdk.UnwrapSDKContext(ctx).Logger().Error("ADR-044: dispute bond NOT returned on unwind -> it stays escrowed on a resolved job. The unwind STANDS; this bond needs a manual look.",
				"job_id", jobId, "bond", job.DisputeBond, "err", err.Error())
		}
	}
	// The jurors are released: there is nothing left to vote on, and holding their seats would keep
	// them un-evictable for an audit that will never conclude.
	k.clearAuditCommittee(ctx, jobId)
	// `resolved` carries the anti-replay, as everywhere else in this module; `unwound` names WHY, so a
	// reader never confuses it with a verdict. No `clawed`, no `vindicated`: no committee ruled.
	job.State = job.State + "+resolved+unwound"
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("ADR-044: job state not persisted after unwind -> the refund STANDS and the job may be re-examined. Money is not double-spent: HeldFee is already consumed, so a second pass finds no debt.",
			"job_id", jobId, "err", err.Error())
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	sdkCtx.Logger().Info("ADR-044: retention UNWOUND — the client is refunded, the miner is neither paid nor slashed, its stake and bond are intact. No sanction: an audit that cannot conclude is not a verdict.",
		"job_id", jobId, "held_since", since, "height", h, "age", now-since,
		"audit_unwind_blocks", p.AuditUnwindBlocks, "refunded", refunded)
	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"audit_retention_unwound",
		sdk.NewAttribute("job_id", jobId),
		sdk.NewAttribute("reason", "no audit could conclude within audit_unwind_blocks -> the trade is undone, not judged"),
		sdk.NewAttribute("age_blocks", strconv.FormatUint(now-since, 10)),
		sdk.NewAttribute("refunded", strconv.FormatUint(refunded, 10)),
	))
	return true
}
