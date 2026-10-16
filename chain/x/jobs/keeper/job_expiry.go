package keeper

import (
	"context"
	"strconv"
	"strings"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// jobExpiryBlocks is how long a job may sit unserved before its escrow returns to the client.
//
// It is the compiled default of `job_expiry_blocks` (ADR-048 item 6), selected when the parameter is
// 0 (`effectiveJobExpiry`, policy_params.go).
//
// 100_000 blocks is about 6 days at the interval measured on this chain (about 5.2 s). The direction
// of the error matters more than the number: too short would claw back a job whose miner is merely
// slow, punishing an honest client and an honest miner at once, while too long only delays a refund
// that is certain. So it is generous on purpose.
const jobExpiryBlocks int64 = 100_000

// jobExpiryRetryBlocks is how long before a failed refund is attempted again. A bank refusal must
// not consume the appointment: dropping it would strand the money this file exists to return.
const jobExpiryRetryBlocks int64 = 1_000

// jobExpiryMaxPerBlock is how many escrows may be returned in a single block.
//
// The EndBlocker has no gas, so nothing else bounds this pass. The number is a rate rather than a
// limit: what does not fit is not dropped, it stays in the collection and the next block takes it,
// oldest first, so the cap only delays and never loses an escrow.
//
// The cap is justified by what it costs (a job read plus a bank transfer per entry, in a block
// nothing can interrupt), not by a gas ceiling: this chain has no per-block gas limit
// (`max_gas = -1`) and its validators run with `--minimum-gas-prices 0udndr`, so the only bound on
// a block is `max_bytes`.
const jobExpiryMaxPerBlock = 200

// revealMaxPerBlock is how many committee freezes may run in a single block, with the same reasoning.
// It matters more than it looks: the deferral in `abci.go` pushes a batch by a fixed stride, so every
// batch of the same residue class lands on the same height and merges. Without a cap the merged batch
// would grow without bound, the defect the expiry cap exists to prevent.
const revealMaxPerBlock = 200

// expireUnservedJob returns the client's escrow when nobody ever served the job.
//
// It refuses far more often than it refunds, and every refusal is deliberate:
//   - the job is gone: nothing to refund, the appointment is simply spent;
//   - the state is not "open": something happened to it (+paid, +settled, +disputed, +finalized,
//     +resolved), so settlement owns the money and this pass must not touch it;
//   - a commit exists: the primary did serve, and the job is waiting to settle. The state can still
//     read "open" at that moment, which is why the commit is checked rather than trusting the state
//     alone: refunding here would take the fee away from work that was actually done.
//
// Best-effort like every other bank movement reached from the EndBlocker: a refusal is logged and
// rescheduled, never propagated. Returning an error here would abort the block, every validator would
// replay it and fail identically, and the chain would stop over one refund.
func (k Keeper) expireUnservedJob(ctx context.Context, jobId string, h int64) {
	log := sdk.UnwrapSDKContext(ctx).Logger()

	job, err := k.Job.Get(ctx, jobId)
	if err != nil {
		return // already gone: the appointment is spent, nothing is owed
	}
	if job.State != "open" {
		return // it moved on; settlement owns the money from here
	}
	// Served? A commit is keyed "<jobId>__<minerId>" (commit_range.go), never by the bare jobId, and
	// CreateCommit refuses a JobId that carries no "__". A query on the bare key could never match, and
	// a job that had been served would be refunded to the client, taking the fee
	// away from work that was done. The bounded walk below is the same existence test the settlement
	// paths use, and it stops at the first key.
	// Served by whom? The prefix alone does not say, and any registered miner can write under it.
	// `CreateCommit` only checks that the signer is the operator of the miner it names
	// (msg_server_commit.go, "only the miner's operator may anchor its commit"); it never checks that
	// this miner was assigned to this job. A registered miner outside the committee could anchor
	// `<jobId>__<itsOwnId>` on somebody else's open job, this walk would see a key, `served` would be
	// true, and the expiry would hand the escrow back to nobody: the assigned committee never settles
	// (it never answered), and the client is never refunded. One registration and one gas fee would
	// freeze any open job's fee for good. The membership test below closes that: only a commit from a
	// miner the draw actually assigned counts as service.
	members, mErr := k.assignedCommittee(ctx, jobId, k.workCommitteeSize(ctx))
	if mErr != nil {
		// The committee is not derivable yet (deferred reveal pending) or the pool is unreadable. That
		// is an unknown on the criterion that decides where the money goes, and an unknown is neither
		// "served" nor "unserved": refuse and come back, like the store-failure branch below.
		log.Error("expireUnservedJob: assigned committee not derivable -> refusing to decide, rescheduled",
			"job_id", jobId, "err", mErr.Error())
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: committee underivable and the retry could not be scheduled -> escrow unreachable",
				"job_id", jobId, "err", sErr.Error())
		}
		return
	}
	served := false
	if wErr := k.Commit.Walk(ctx, commitRange(jobId+"__"), func(key string, _ types.Commit) (bool, error) {
		// The key is "<jobId>__<minerId>"; the miner id is what follows the last separator, because a
		// jobId may itself contain one.
		if i := strings.LastIndex(key, "__"); i >= 0 && members[key[i+2:]] {
			served = true
			return true, nil
		}
		return false, nil
	}); wErr != nil {
		// The store is unreadable on a criterion that decides where the money goes. No default is
		// supplied on a security check: an unknown is not "nobody served it". Refuse and retry.
		log.Error("expireUnservedJob: commit lookup failed -> refusing to refund, rescheduled",
			"job_id", jobId, "err", wErr.Error())
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: commit lookup failed and the retry could not be scheduled -> escrow unreachable",
				"job_id", jobId, "err", sErr.Error())
		}
		return
	}
	if served {
		// Served but not yet settled: the fee belongs to that work, not to a refund. It must not be
		// abandoned here either: returning outright drops the job out of the expiry queue for good, so
		// a committee that answered and then never settled would leave the escrow with no appointment
		// left to reclaim it. Come back later instead; if settlement happens meanwhile, the
		// `state != "open"` check above ends the cycle at the next visit.
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: served-but-unsettled and the retry could not be scheduled -> escrow unreachable",
				"job_id", jobId, "err", sErr.Error())
		}
		return
	}
	if job.Fee == 0 {
		return
	}

	refunded := k.refundClient(ctx, &job, job.Fee, job.Fee)
	if refunded == 0 {
		// The bank refused (an unreadable client address, a module short of coins). The money is still
		// owed, so the appointment is renewed instead of being dropped.
		if err := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); err != nil {
			log.Error("expireUnservedJob: refund failed and the retry could not be scheduled -> escrow is now unreachable",
				"job_id", jobId, "fee", job.Fee, "err", err.Error())
			return
		}
		log.Error("expireUnservedJob: refund refused -> rescheduled, escrow still held",
			"job_id", jobId, "fee", job.Fee, "retry_at", h+jobExpiryRetryBlocks)
		return
	}

	job.State = job.State + "+expired+refunded"
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		// The coins are back with the client; only the record failed. Log it loudly rather than let a
		// later pass read a job that still looks refundable.
		log.Error("expireUnservedJob: client refunded but the job could not be persisted",
			"job_id", jobId, "refunded", refunded, "err", err.Error())
		return
	}
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
		"job_expired_refunded",
		sdk.NewAttribute("job_id", jobId),
		sdk.NewAttribute("client", job.Client),
		sdk.NewAttribute("refunded", strconv.FormatUint(refunded, 10)),
	))
	log.Info("expireUnservedJob: never served -> client escrow returned",
		"job_id", jobId, "client", job.Client, "refunded", refunded)
}
