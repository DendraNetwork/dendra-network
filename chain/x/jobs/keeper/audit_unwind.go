package keeper

import (
	"context"
	"strconv"

	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// unwindStrandedRetention undoes a retention no audit can ever conclude, instead
// of deferring it again.
//
// The deferral sites (a jury under the floor, a committee below quorum, and since epoch 14 a missing
// decentralised seed) reschedule forever, on purpose: releasing to the miner after N
// attempts would make evasion free, and clawing back would punish an honest primary for a network
// failure. The price is that held escrow can grow without bound on a thin network. This function pays
// that price down without reopening either hole: the client is refunded, the miner is neither paid
// nor slashed, and its stake and the dispute bond stay intact. Silence yields zero instead of a free
// pass.
//
// Dormant by default, and dormant means byte-identical: at `audit_unwind_blocks = 0` the first branch
// returns before reading anything else, so every deferral behaves as it did before this file existed.
// The tests drive that property, since "dormant by default" is otherwise only an intention.
//
// Fail closed on the date, in every way it can fail: `HeldSince` unreadable, absent while a debt
// exists, or later than the current height all return false, and the caller defers as before. An
// unknown age is never a licence to unwind: the deferral sites exist so that nobody loses to a network
// failure, and undoing a debt whose age cannot be established would be exactly that.
//
// The order of the checks is the contract. Every refusal happens before any mutation, and once
// `refundRetainedToClient` has moved money the function commits: it returns true even if a later
// persistence fails, and logs it loudly. Returning false after the refund would let the caller defer
// a job that has already been settled, refunded and rescheduled, the one outcome worse than either
// branch alone.
//
// It returns true when the retention was unwound and the caller must not defer.
func (k Keeper) unwindStrandedRetention(ctx context.Context, h int64, jobId string, p types.Params) bool {
	// (0) Dormant: read nothing, touch nothing, decide nothing.
	if p.AuditUnwindBlocks == 0 {
		return false
	}
	// (1) Is there a debt at all? No retention means there is nothing to unwind; the deferral is then
	// about the audit itself and must proceed untouched. A read error is treated as "no" for the same
	// reason as the date below: not knowing is not a licence.
	held, errHeld := k.HeldFee.Get(ctx, jobId)
	if errHeld != nil || held == 0 {
		return false
	}
	// (2) The age. `HeldSince` is the twin key of `HeldFee`, and a debt without its date is the case
	// this must refuse, because the age is the only thing that distinguishes "stranded" from "still
	// being verified".
	since, errSince := k.HeldSince.Get(ctx, jobId)
	if errSince != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("retention held with no readable date -> deferred as before, never unwound. An age that cannot be established is not an age.",
			"job_id", jobId, "held", held)
		return false
	}
	if h < 0 {
		return false // a negative height is not a height; refuse rather than convert it
	}
	now := uint64(h)
	if now < since {
		// A date in the future. Subtracting would wrap around on uint64 and yield an enormous age, so the
		// unwind would fire on the youngest debts.
		sdk.UnwrapSDKContext(ctx).Logger().Error("held_since is ahead of the current height -> deferred. The subtraction would wrap and read as a very old debt.",
			"job_id", jobId, "held_since", since, "height", h)
		return false
	}
	if now-since < p.AuditUnwindBlocks {
		return false // too young: the audit still has time to conclude, which is the normal case
	}
	// (3) The job. Without it there is nothing to mark and no client to name; refuse before mutating.
	job, errJob := k.Job.Get(ctx, jobId)
	if errJob != nil {
		return false
	}
	// The primary may be absent (evicted, or carried in by an import). `refundRetainedToClient`
	// tolerates a nil miner (only the Demand reversal is skipped), and the client is refunded either
	// way: the retention belongs to the client, whatever happened to the miner.
	var miner *types.Miner
	if m, errMiner := k.Miner.Get(ctx, job.MinerId); errMiner == nil {
		miner = &m
	}

	// Past this line the function commits.
	refunded := k.refundRetainedToClient(ctx, &job, p, miner)
	if miner != nil {
		if err := k.Miner.Set(ctx, miner.MinerId, *miner); err != nil {
			// Best-effort, and logged. A reversed Demand that does not persist leaves a stale figure, a
			// reporting defect; returning false here would leave a settled job scheduled for another
			// audit, a correctness defect. The smaller one is taken and named.
			sdk.UnwrapSDKContext(ctx).Logger().Error("miner not persisted after unwind (Demand reversal lost) -> the unwind stands, the figure is stale.",
				"job_id", jobId, "miner_id", job.MinerId, "err", err.Error())
		}
	}
	// The dispute bond is returned, by the module's own rule. `resolveDisputedAudit` states it in full:
	// return when the disputer is right or when no instance remains, confiscate when it is wrong, and
	// keep it in escrow while an instance is still possible. An unwind is the case where no instance
	// remains, since no committee will ever rule on this job. Keeping it in escrow here, on a job that
	// is now resolved, would strand the bond forever on a state nothing revisits; confiscating it would
	// sanction a disputer no committee ever found wrong. The unwind's "bond intact" and that rule agree,
	// and this is the branch where they meet.
	if job.DisputeBond > 0 {
		if err := k.refundDisputeBond(ctx, &job); err != nil {
			sdk.UnwrapSDKContext(ctx).Logger().Error("dispute bond not returned on unwind -> it stays escrowed on a resolved job. The unwind stands; this bond needs a manual look.",
				"job_id", jobId, "bond", job.DisputeBond, "err", err.Error())
		}
	}
	// The jurors are released: there is nothing left to vote on, and holding their seats would keep
	// them impossible to evict for an audit that will never conclude.
	k.clearAuditCommittee(ctx, jobId)
	// `resolved` carries the anti-replay, as everywhere else in this module; `unwound` names why, so a
	// reader never mistakes it for a verdict. No `clawed`, no `vindicated`: no committee ruled.
	job.State = job.State + "+resolved+unwound"
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("job state not persisted after unwind -> the refund stands and the job may be re-examined. Money is not double-spent: HeldFee is already consumed, so a second pass finds no debt.",
			"job_id", jobId, "err", err.Error())
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	sdkCtx.Logger().Info("retention unwound — the client is refunded, the miner is neither paid nor slashed, its stake and bond are intact. No sanction: an audit that cannot conclude is not a verdict.",
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
