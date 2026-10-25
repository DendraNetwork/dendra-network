package keeper

import (
	"bytes"
	"context"

	"cosmossdk.io/collections"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// DisputeVerdict challenges the verdict of a settled job, without permission and against a bond
// (anti-grief). This steps outside the honest-majority assumption: the existing `report-divergence`
// is governance-gated, whereas here anyone can challenge by posting a bond.
//
// Increment 1 is the primitive: validation, bond escrow, and state marking (`...+disputed`).
// Re-adjudication (drawing a fresh committee through the beacon, semantic re-settlement, then a slash
// of the guilty party or a refund, or a slash of the bond if the dispute fails) is increment 2.
//
// Dormant by default: `dispute_window == 0` disables disputes, leaving e2e and devnet unchanged.
func (k msgServer) DisputeVerdict(ctx context.Context, msg *types.MsgDisputeVerdict) (*types.MsgDisputeVerdictResponse, error) {
	p, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if p.DisputeWindow == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "disputes are disabled (dispute_window=0)")
	}
	disputerBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown job")
	}
	// Only a settled job can be challenged (a verdict exists), and only once (dispute anti-replay).
	if !jobIsPaid(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "only a settled job can be challenged (paid/settled)")
	}
	if jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already challenged (anti-replay)")
	}
	// A closed job cannot be challenged, because the bond would have no way out. An unwound job is
	// stored `+resolved+unwound` without `+disputed` (the jury-floor and missing-seed deferrals unwind
	// before any dispute exists), so the check above let a dispute through. Every resolution path then
	// refuses a resolved job (AdjudicateDispute, resolveDisputedAudit, ResolveDispute), and the escrowed
	// bond stayed in the module for good.
	if jobIsResolved(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already resolved: no instance remains to hear a dispute, and its bond could never be returned")
	}
	// Bond (anti-grief): the disputer escrows `dispute_bond` udndr into the "jobs" module account, the
	// same mechanism as the fee escrow in OpenJob. Losing the dispute means losing the bond.
	if p.DisputeBond > 0 {
		bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(p.DisputeBond)))
		if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(disputerBz), types.ModuleName, bond); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}
	job.Disputer = msg.Creator
	job.DisputeBond = p.DisputeBond
	job.DisputeHeight = sdk.UnwrapSDKContext(ctx).BlockHeight()
	job.State = job.State + "+disputed"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// A human dispute opens an audit and is counted once. If the job had already
	// been selected and deferred by the lottery (AuditSelected), the opening was counted there, and
	// counting it again would make the partition sum overshoot by one.
	// Fail closed: the store error is raised, never folded into the predicate. Written as
	// `sErr == nil && !sel`, a store failure would silently skip the accounting, while
	// audit_sampling.go propagates its own error; with two policies for one predicate, whoever
	// harmonises them later picks one at random. Both use fail-closed: the error aborts the tx and
	// never silently skews the counter.
	sel, sErr := k.AuditSelected.Has(ctx, job.JobId)
	if sErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, sErr.Error())
	}
	if !sel {
		k.bumpAuditsOpened(ctx)
	}
	// Anchor the re-adjudication committee here. The seed is the one of the opening height:
	// later than the original commits (so the miner cannot grind it) and earlier than the re-commits
	// (so the challenger cannot pick its judges after the fact).
	// Non-blocking: a draw failure leaves the anchor absent, and `AdjudicateDispute` then refuses to
	// move a single coin (fail closed), and the audit timeout decides instead.
	// Origin mark: once the job is `+disputed`, nothing distinguishes an opening by VRF sampling from
	// one by a human, and deadline-driven resolution must decide differently in the two cases (see
	// `humanDisputeKey`). It is written while the information still exists.
	if err := k.AuditCommittee.Set(ctx, humanDisputeKey(job.JobId), msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if err := k.anchorRedoCommittee(ctx, job.JobId, job.MinerId, msg.Creator); err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("SECURITY: re-adjudication committee not anchored -> adjudication is fail-closed on this job (the audit timeout will decide).", "job_id", job.JobId, "err", err.Error())
	}
	// Liveness, automatic deadline. `runOptimisticAudit` schedules `PendingAuditResolve` for the jobs
	// it samples; a human dispute on a never-sampled job schedules none, so without this block its only
	// exit is `AdjudicateDispute`, which requires a drawn committee. That pool is N-5 (excluding the
	// primary, the disputer and the three original members): if it is too small, or those drawn stay
	// silent, the job would stay `+disputed` forever, contradicting the guarantee in `audit_sampling.go`
	// that no job stays +disputed indefinitely. The anchored re-adjudication committee requirement makes that case
	// structural, so it is handled here: every dispute has a deadline, and `resolveDisputedAudit`
	// decides. A duplicate entry is harmless when the job is also sampled, because resolution is
	// idempotent (`jobIsResolved`).
	if p.AuditResolveTimeout > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+int64(p.AuditResolveTimeout), job.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	return &types.MsgDisputeVerdictResponse{}, nil
}

// ResolveDispute resolves an open dispute and settles the bond.
// Interim: authority-gated (gov), like report-divergence. Permissionless self-resolution by a fresh
// committee (re-run through the beacon, semantic re-settlement, slash of the guilty party and reversal
// of the original slash) is increment 3; it requires off-chain re-execution and recording the original
// verdict. Here the dispute cycle is closed with the bond economics:
//   - upheld=true (dispute valid): the bond is refunded to the disputer, plus a reward from the
//     Treasury (bounded by its balance), since the challenge was justified.
//   - upheld=false (dispute rejected): the bond is slashed to the Treasury (anti-grief).
//
// Anti-replay: a single resolution per dispute (the "resolved" marker).
func (k msgServer) ResolveDispute(ctx context.Context, msg *types.MsgResolveDispute) (*types.MsgResolveDisputeResponse, error) {
	authBz, err := k.addressCodec.StringToBytes(msg.Authority)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid authority address")
	}
	if !bytes.Equal(k.GetAuthority(), authBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "resolve-dispute is reserved to the authority (interim; fresh-committee self-resolution is increment 3)")
	}
	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown job")
	}
	if !jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no open dispute for this job")
	}
	if jobIsResolved(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "dispute already resolved (anti-replay)")
	}
	pools, perr := k.Pools.Get(ctx)
	if perr != nil {
		pools = types.Pools{}
	}
	bond := job.DisputeBond
	if msg.Upheld {
		// Restitution first: the challenged verdict was wrong, so the slashes recorded at settlement are
		// reversed. Each slashed miner's stake is credited back (a plain counter: the coins have been
		// inside the module since the slash); bounded by the Treasury; non-replayable through "resolved".
		for _, rec := range job.SlashRecords {
			amt := rec.Amount
			if amt > pools.Treasury {
				amt = pools.Treasury
			}
			if amt == 0 {
				continue
			}
			m, mErr := k.Miner.Get(ctx, rec.MinerId)
			if mErr != nil {
				continue
			}
			m.Stake += amt
			if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
			pools.Treasury -= amt
		}
		// Dispute valid: refund the bond plus a reward (bounded by the remaining Treasury). The module,
		// which holds the escrow, pays the coins; the Treasury counter is reduced by the reward.
		reward := bond
		if reward > pools.Treasury {
			reward = pools.Treasury
		}
		if payout := bond + reward; payout > 0 {
			disputerBz, derr := k.addressCodec.StringToBytes(job.Disputer)
			if derr != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "disputer address")
			}
			coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(payout)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(disputerBz), coins); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
			}
		}
		pools.Treasury -= reward
	} else {
		// Dispute rejected: bond to the Treasury (anti-grief; the coins stay inside the module).
		pools.Treasury += bond
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// The retained money of an optimistic job is settled here too, as on every other
	// resolution. Marking the job `+resolved` without touching the retention would leave HeldFee and
	// HeldBurn frozen on a job no path revisits, and since epoch 14 the three shares of the cut and the
	// pending subsidy right as well. The rule is the same as in AdjudicateDispute: upheld
	// means the optimistic payment was wrong, so the client is refunded from the retention; rejected
	// means it stands, so the retention is released and the cut paid. No-op when nothing is retained.
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		if msg.Upheld {
			p, pErr := k.Params.Get(ctx)
			if pErr != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, pErr.Error())
			}
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				k.refundRetainedToClient(ctx, &job, p, &m)
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
			} else {
				k.refundRetainedToClient(ctx, &job, p, nil)
			}
		} else {
			k.releaseHeld(ctx, &job)
		}
	}
	job.State = job.State + "+resolved"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	k.clearAuditCommittee(ctx, job.JobId) // same step as every other resolution: purge the anchored seats
	return &types.MsgResolveDisputeResponse{}, nil
}
