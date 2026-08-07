package keeper

import (
	"context"
	"sort"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// SettlePay is the real payment path, bounded to the ASSIGNED COMMITTEE. The CLIENT (the signer)
// rewards the miners of the honest MAJORITY AMONG THE ASSIGNED MINERS, in real "udndr", FROM ITS OWN
// wallet.
//
// Replay protection: the Job is loaded, a job already settled is refused, and the "+settled" flag is
// written at the end with its error checked. Without that, a double call pays twice. `msg.Amount`
// stays client-controlled: it is the CLIENT's wallet and its own choice of amount, not module escrow.
func (k msgServer) SettlePay(ctx context.Context, msg *types.MsgSettlePay) (*types.MsgSettlePayResponse, error) {
	fromBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// Replay protection on the job state.
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not open")
	}
	if jobIsPaid(job.State) { // UNIFIED settlement replay guard (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already settled (replay guard)")
	}
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
			if committee[mid] {
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
	// A COMPLETE committee is required (see msg_server_payout.go): without it, the first miner to
	// commit takes the whole escrow before its peers have submitted.
	if len(commitByMiner) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "incomplete committee -> settlement refused")
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

	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	// The client pays a TOTAL of `msg.Amount`, SPLIT across the winners (per = Amount / nWinners), and
	// not `Amount` to EACH of them, which would debit Amount x N from the client.
	winners := make([]string, 0, len(mids))
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			winners = append(winners, mid)
		}
	}
	if len(winners) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no majority -> nothing to pay")
	}
	per := msg.Amount / uint64(len(winners))
	remainder := msg.Amount - per*uint64(len(winners)) // integer-division remainder (<= N-1 udndr)
	for i, mid := range winners {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
		if aErr != nil {
			continue
		}
		amt := per
		if i == 0 {
			amt += remainder // to the first winner -> the client pays EXACTLY Amount, no dust lost
		}
		coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(amt)))
		if err := k.bankKeeper.SendCoins(ctx, sdk.AccAddress(fromBz), sdk.AccAddress(toBz), coins); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}
	// Mark as settled, with the write error checked.
	job.State = job.State + "+settled"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark +settled: "+err.Error())
	}
	return &types.MsgSettlePayResponse{}, nil
}
