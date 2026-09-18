package keeper

import (
	"context"
	"errors"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// FinalizeJob — the on-chain VERDICT, bounded to the ASSIGNED committee. Anyone may relay the message;
// the chain does not trust the caller. It reads the job's anchored commits, keeps ONLY those of the
// miners ASSIGNED by its own rule (assignedCommittee), computes the honest majority
// DETERMINISTICALLY, and slashes the divergent miners (SlashLeakBps -> Treasury). An unassigned miner
// therefore cannot inject a vote to skew the verdict.
func (k msgServer) FinalizeJob(ctx context.Context, msg *types.MsgFinalizeJob) (*types.MsgFinalizeJobResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// DISABLED IN OPTIMISTIC MODE, for the same reason as verify-semantic: k=3 finalization by commit
	// comparison slashes cosine outliers, which has no role in optimistic mode and, being
	// permissionless, allowed honest NON-primary miners to be slashed. Bounded to mode 0.
	if params.VerificationMode != 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"finalize-job is disabled in optimistic mode (verification_mode != 0): verification goes through the VRF audit")
	}
	// The job must EXIST (opened through OpenJob) BEFORE any slash, otherwise an attacker griefs the
	// REAL bond of an honest miner on a PHANTOM job for the price of one transaction. Then anti-replay:
	// an already finalized job cannot be finalized again, so it cannot be double-slashed.
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job does not exist -> finalization and slash refused")
	}
	// UNIFIED ANTI-DOUBLE-SLASH: this also blocks `verify -> finalize` and `settle(k3) -> finalize`,
	// not just `finalize -> finalize`. All three paths slash the same outlier.
	if jobK3Adjudicated(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "the k=3 committee already ruled (settled, verified or finalized): anti-replay")
	}

	// THE DRAW COMES AFTER THE PROOF THAT THE JOB EXISTS. On a phantom job, the reverse order returned
	// the DRAW's error instead of "job does not exist": the message named the wrong cause, and since
	// ADR-037 it masked the expected refusal (`ErrKeyNotFound`) outright. A handler must establish that
	// its subject exists before reasoning about it.
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	commitByMiner := map[string]string{}
	tally := map[string]int{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] { // only ASSIGNED miners vote
				commitByMiner[mid] = c.ResultCommit
				tally[c.ResultCommit]++
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(commitByMiner) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no commit from an assigned miner for this job")
	}

	commits := make([]string, 0, len(tally))
	for c := range tally {
		commits = append(commits, c)
	}
	sort.Strings(commits)
	canonical, best := "", -1
	for _, c := range commits {
		if tally[c] > best {
			best, canonical = tally[c], c
		}
	}
	// `verify_semantic` and `settle_semantic` already require the FULL committee; this path, which
	// SLASHES, used to accept "at least one commit". The consequence: with two commits out of three
	// disagreeing (1-1), the "canonical" answer was decided by the LEXICOGRAPHIC ORDER of the commit
	// strings. A committee member could commit, wait for a single honest peer, call FinalizeJob before
	// the third arrived, and make that peer lose `slash_leak_bps` of its bond on an alphabetical coin
	// toss. A STRICT MAJORITY of the ASSIGNED committee is now required — of the assigned members, not
	// of those present — so a tie slashes nobody and one absentee cannot form a quorum on its own.
	// Liveness is preserved: two agreements out of three suffice.
	if best*2 <= CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no strict majority of the assigned committee -> finalization and slash refused (tie, or committee too incomplete)")
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			continue
		}
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		amt := miner.Stake * params.SlashLeakBps / 10000
		miner.Stake -= amt
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		pools.Treasury += amt
		// Record the slash: it can be restituted if a dispute is later upheld (see verify-semantic).
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: mid, Amount: amt})
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// Mark the job finalized by appending, so an existing "+paid" is not lost. The job necessarily
	// exists at this point (checked above) and the error from Set is CHECKED, which is what makes the
	// anti-replay guard reliable.
	job.State = job.State + "+finalized"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark +finalized: "+err.Error())
	}
	return &types.MsgFinalizeJobResponse{}, nil
}
