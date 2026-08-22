package keeper

import (
	"context"
	"strconv"
	"strings"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// jobExpiryBlocks — how long a job may sit UNSERVED before its escrow returns to the client.
//
// A COMPILED CONSTANT, AND THAT IS A DEBT WORTH NAMING. Every other deadline on this chain is either
// governed (`committee_reveal_delay`, `juror_freshness_blocks`) or compiled with a governed override.
// This one has no parameter yet, because adding one is a change to the params message and therefore to
// the wire format: it belongs to the same pass as any other params addition, not to the fix that stops
// the coins from being stranded. Until then the value is read here and nowhere else.
//
// 100_000 blocks is ~6 days at the interval measured on this chain (~5.2 s). The number matters less
// than the direction of the error: too SHORT would claw back a job whose miner is merely slow, which
// punishes an honest client and an honest miner at once; too LONG only delays a refund that is
// certain. So it is deliberately generous.
const jobExpiryBlocks int64 = 100_000

// jobExpiryRetryBlocks — how long before a FAILED refund is attempted again. A bank refusal must not
// consume the appointment: dropping it here would strand exactly the money this file exists to return.
const jobExpiryRetryBlocks int64 = 1_000

// jobExpiryMaxPerBlock — how many escrows may be returned in a SINGLE block.
//
// The EndBlocker has no gas, so nothing else bounds this pass. The number is a rate, not a limit:
// what does not fit is not dropped, it stays in the collection and the next block takes it, oldest
// first, so the cap only delays -- it never loses an escrow.
//
// AND THE OBVIOUS JUSTIFICATION WOULD HAVE BEEN FALSE. This comment first read "a block holds far
// fewer than 200 OpenJob at the current gas limit". This chain has NO per-block gas limit
// (`max_gas = -1`) and its validators run with `--minimum-gas-prices 0udndr`: the only bound on a
// block is `max_bytes`. Citing a ceiling the chain does not have is exactly the recopied-number
// mistake this repository keeps paying for -- so the cap is justified by what it COSTS instead: a
// job read plus a bank transfer per entry, in a block nothing can interrupt.
const jobExpiryMaxPerBlock = 200

// revealMaxPerBlock — how many committee freezes may run in a SINGLE block, same reasoning as above.
// It matters more here than it looks: the deferral in `abci.go` pushes a batch by a FIXED stride, so
// every batch of the same residue class lands on the same height and MERGES. Without a cap the merged
// batch grows without bound, which is the defect the expiry cap exists to prevent, reintroduced by
// the deferral's own fix.
const revealMaxPerBlock = 200

// expireUnservedJob — returns the client's escrow when nobody ever served the job.
//
// IT REFUSES FAR MORE OFTEN THAN IT REFUNDS, and every refusal is deliberate:
//   - the job is gone            -> nothing to refund, the appointment is simply spent;
//   - the state is not "open"    -> anything at all happened to it (+paid, +settled, +disputed,
//     +finalized, +resolved), so settlement owns the money and this pass must not touch it;
//   - a commit exists            -> the primary DID serve, and the job is merely waiting to settle.
//     The state can still read "open" at that moment, which is exactly why the commit is checked
//     rather than trusted to the state alone: refunding here would take the fee away from work that
//     was actually done.
//
// Best-effort like every other bank movement reached from the EndBlocker: a refusal is logged and
// RESCHEDULED, never propagated. Returning an error here would abort the block, every validator would
// replay it and fail identically, and the chain would stop -- over one refund.
func (k Keeper) expireUnservedJob(ctx context.Context, jobId string, h int64) {
	log := sdk.UnwrapSDKContext(ctx).Logger()

	job, err := k.Job.Get(ctx, jobId)
	if err != nil {
		return // already gone: the appointment is spent, nothing is owed
	}
	if job.State != "open" {
		return // it moved on; settlement owns the money from here
	}
	// SERVED? A commit is keyed "<jobId>__<minerId>" (commit_range.go), NEVER by the bare jobId --
	// CreateCommit REFUSES a JobId that carries no "__". This guard used to query the bare key, so it
	// could never match: it was DEAD, and a job that HAD been served was refunded to the client,
	// taking the fee away from work that was actually done. That is the exact outcome the comment
	// above says it exists to prevent. The bounded walk below is the same existence test the
	// settlement paths use, and it stops at the first key.
	// SERVED BY WHOM? THE PREFIX ALONE DOES NOT SAY, AND ANY REGISTERED MINER CAN WRITE UNDER IT.
	// `CreateCommit` only checks that the signer is the operator OF THE MINER IT NAMES
	// (msg_server_commit.go, "only the miner's operator may anchor its commit") -- it never checks that
	// this miner was ASSIGNED to this job. So a miner that is registered but not on the committee could
	// anchor `<jobId>__<itsOwnId>` on somebody else's open job, this walk would see a key, `served`
	// would be true, and the expiry would hand the escrow back to nobody: the assigned committee never
	// settles (it never answered), and the client is never refunded. One registration and one gas fee
	// freeze any open job's fee for good. The membership test below closes that: only a commit from a
	// miner the draw actually assigned counts as service.
	members, mErr := k.assignedCommittee(ctx, jobId, CommitteeSize)
	if mErr != nil {
		// The committee is not derivable yet (deferred reveal pending) or the pool is unreadable. That
		// is an UNKNOWN on the criterion that decides where the money goes, and an unknown is neither
		// "served" nor "unserved": refuse and come back, exactly like the store-failure branch below.
		log.Error("expireUnservedJob: assigned committee not derivable -> refusing to decide, rescheduled",
			"job_id", jobId, "err", mErr.Error())
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: committee underivable AND the retry could not be scheduled -> escrow unreachable",
				"job_id", jobId, "err", sErr.Error())
		}
		return
	}
	served := false
	if wErr := k.Commit.Walk(ctx, commitRange(jobId+"__"), func(key string, _ types.Commit) (bool, error) {
		// The key is "<jobId>__<minerId>"; the miner id is what follows the LAST separator, because a
		// jobId may itself contain one.
		if i := strings.LastIndex(key, "__"); i >= 0 && members[key[i+2:]] {
			served = true
			return true, nil
		}
		return false, nil
	}); wErr != nil {
		// The store is unreadable on a criterion that decides WHERE THE MONEY GOES. No default is
		// supplied on a security criterion: an unknown is not a "nobody served it". Refuse and retry.
		log.Error("expireUnservedJob: commit lookup failed -> refusing to refund, rescheduled",
			"job_id", jobId, "err", wErr.Error())
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: commit lookup failed AND the retry could not be scheduled -> escrow unreachable",
				"job_id", jobId, "err", sErr.Error())
		}
		return
	}
	if served {
		// SERVED but not yet settled -- the fee belongs to that work, not to a refund. But it must NOT
		// be abandoned here: returning outright drops the job out of the expiry queue for good, so a
		// committee that answered and then never settled would leave the escrow with no appointment
		// left to reclaim it. Come back later instead; if settlement happens meanwhile, the `state !=
		// "open"` test above ends the cycle at the next visit.
		if sErr := k.PendingJobExpiry.Set(ctx, collections.Join(h+jobExpiryRetryBlocks, jobId)); sErr != nil {
			log.Error("expireUnservedJob: served-but-unsettled AND the retry could not be scheduled -> escrow unreachable",
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
			log.Error("expireUnservedJob: refund failed AND the retry could not be scheduled -> escrow is now unreachable",
				"job_id", jobId, "fee", job.Fee, "err", err.Error())
			return
		}
		log.Error("expireUnservedJob: refund refused -> rescheduled, escrow still held",
			"job_id", jobId, "fee", job.Fee, "retry_at", h+jobExpiryRetryBlocks)
		return
	}

	job.State = job.State + "+expired+refunded"
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		// The coins ARE back with the client; only the record failed. Say so loudly rather than let a
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
