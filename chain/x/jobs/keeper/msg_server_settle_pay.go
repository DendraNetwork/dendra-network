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

// SettlePay is the real payment path, bounded to the assigned committee. The client (the signer)
// rewards the miners of the honest majority among the assigned miners, in real "udndr", from its own
// wallet.
//
// Replay protection: the Job is loaded, a job already settled is refused, and the "+settled" flag is
// written at the end with its error checked; without that, a double call pays twice. `msg.Amount`
// stays client-controlled: it is the client's wallet and its own choice of amount, not module escrow.
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
	// Only the job's client may settle it: a permissionless SettlePay could freeze an escrow forever.
	// Without a check on `job.Client`, anyone could settle anyone's job, with `Amount` at zero so the
	// attack costs only gas, and the `+settled` mark written below is terminal: `Payout` and
	// `SettleSemantic` both refuse afterwards through `jobIsPaid`, while
	// `expireUnservedJob` only refunds a job whose state is still exactly "open" (job_expiry.go). The
	// escrow would be locked with no path out, for any job, by any address.
	// The raw settlement that lived next to this handler until consensus epoch 15
	// closed the same attack with the same kind of authority guard.
	if job.Client != msg.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"settle-pay is reserved to the job's own client: settling marks the job terminally, so letting a "+
				"third party do it would strand the escrow with no path left to release or refund it")
	}
	// The amount is bounded from below as well as from above. A settlement of zero pays nobody and
	// still writes the terminal mark; bounding only the ceiling lets a zero through a guard that looks
	// armed.
	if msg.Amount == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"settle-pay with amount 0 pays nobody yet marks the job settled for good: refused")
	}
	if jobIsPaid(job.State) { // unified settlement replay guard (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already settled (replay guard)")
	}
	if jobEscrowReturned(job.State) { // the expiry pass already returned this escrow: nothing left to pay from
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"job escrow already returned to the client (expired): paying now would draw on other clients' escrows")
	}
	// A job that still holds the escrow `OpenJob` took is not this handler's business.
	//
	// Two settlement notions live in this module, and they can collide. `SettleSemantic` is the
	// protocol path: it judges, pays and empties the module escrow. `SettlePay` is the client path: the
	// client rewards the honest majority from its own wallet, which is deliberate and stays. The
	// collision is the marker: `SettlePay` appends "+settled", which is terminal, since
	// `expireUnservedJob` refunds only a state that is exactly "open", and `Payout` and `SettleSemantic`
	// both refuse through `jobIsPaid`. Without the refusal below, the client would pay twice,
	// one payment would reach nobody, and after every disbursement entry point plus the EndBlocker at the
	// booked appointment the escrow would not have moved; the same job settled through `SettleSemantic`
	// empties the module (settlepay_strands_escrow_test.go).
	//
	// Refusing is the option that moves no funds. Releasing the escrow here would make this handler
	// disburse module money on a client's say-so, which it was built not to do.
	// The refusal names the path that works, otherwise it would strand the operator instead of the escrow.
	if job.Fee > 0 {
		return nil, errorsmod.Wrapf(sdkerrors.ErrInvalidRequest,
			"job %s still holds the escrow OpenJob took (fee %d): settle-pay would mark it settled for good "+
				"and no path could ever release that escrow. Use the protocol settlement (settle-semantic, "+
				"or payout) which pays out of the escrow; settle-pay is for a job opened without one.",
			msg.JobId, job.Fee)
	}
	// The governed work committee size, read once so the derivation and the
	// completeness test below compare the same number.
	wcs := k.workCommitteeSize(ctx)
	committee, err := k.assignedCommittee(ctx, msg.JobId, wcs)
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
	// A complete committee is required (see msg_server_payout.go): without it, the first miner to
	// commit takes the whole escrow before its peers have submitted.
	if len(commitByMiner) < wcs {
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
	// The client pays a total of `msg.Amount`, split across the winners (per = Amount / nWinners), not
	// `Amount` to each of them, which would debit Amount x N from the client.
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
			amt += remainder // to the first winner, so the client pays exactly Amount and no dust is lost
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
