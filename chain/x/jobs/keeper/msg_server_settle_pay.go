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
	// ⛔ SETTLE-PAY WAS PERMISSIONLESS, AND THAT FREEZES AN ESCROW FOR EVER.
	// This handler never read `job.Client`. Anyone could settle anyone's job -- with `Amount` at ZERO,
	// so the whole attack costs gas -- and the `+settled` mark written below is terminal: `Payout`,
	// `SettleSemantic` and `SettleJob` all refuse afterwards through `jobIsPaid`, while
	// `expireUnservedJob` only ever refunds a job whose state is still exactly "open"
	// (job_expiry.go). Escrow locked, no path out, for any job, by any address.
	// The attack is described at the top of msg_server_settle_job.go, where an authority guard closes
	// it -- and the guard was never carried across to this sibling. A rule learned in one file and not
	// propagated protects only that file.
	if job.Client != msg.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"settle-pay is reserved to the job's own client: settling marks the job terminally, so letting a "+
				"third party do it would strand the escrow with no path left to release or refund it")
	}
	// AND THE AMOUNT IS BOUNDED FROM BELOW, not only from above. A settlement of zero is not a
	// settlement: it pays nobody and still writes the terminal mark. Bounding only the ceiling is how a
	// zero slips through a guard that looks armed.
	if msg.Amount == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"settle-pay with amount 0 pays nobody yet marks the job settled for good: refused")
	}
	if jobIsPaid(job.State) { // UNIFIED settlement replay guard (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already settled (replay guard)")
	}
	if jobEscrowReturned(job.State) { // the expiry pass already returned this escrow: nothing left to pay FROM
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"job escrow already returned to the client (expired): paying now would draw on other clients' escrows")
	}
	// ⛔ A JOB THAT STILL HOLDS THE ESCROW `OpenJob` TOOK IS NOT THIS HANDLER'S BUSINESS (ADR-045, 9).
	//
	// Two settlement notions live in this module and they were treading on each other. `SettleSemantic`
	// is the PROTOCOL path: it judges, pays and EMPTIES the module escrow. `SettlePay` is the CLIENT
	// path: the client rewards the honest majority from its OWN wallet, which is deliberate and stays.
	// The collision is the marker: `SettlePay` appends "+settled", which is terminal —
	// `expireUnservedJob` refunds only a state that is exactly "open", and `Payout` / `SettleSemantic` /
	// `SettleJob` all refuse through `jobIsPaid`. MEASURED (settlepay_strands_escrow_test.go): the client
	// pays TWICE, one payment reaches nobody, and after every disbursement entry point plus the
	// EndBlocker at the booked appointment the escrow has not moved. Control: the same job settled
	// through `SettleSemantic` empties the module.
	//
	// Refusing is the option that MOVES NO FUNDS. Releasing the escrow here would make this handler
	// disburse module money on a client's say-so, which is exactly what it was built not to do.
	// ⚠️ The refusal NAMES the path that works, or it would strand the operator instead of the escrow.
	if job.Fee > 0 {
		return nil, errorsmod.Wrapf(sdkerrors.ErrInvalidRequest,
			"job %s still holds the escrow OpenJob took (fee %d): settle-pay would mark it settled for good "+
				"and no path could ever release that escrow. Use the protocol settlement (settle-semantic, "+
				"or payout) which pays FROM the escrow; settle-pay is for a job opened without one.",
			msg.JobId, job.Fee)
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
