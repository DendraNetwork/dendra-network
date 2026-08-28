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

// DisputeVerdict (design: docs/DISPUTE-FRAUDPROOF.md) — challenges the verdict of a SETTLED job,
// PERMISSIONLESSLY and against a BOND (anti-grief). This steps outside the honest-majority assumption:
// the existing `report-divergence` is GOVERNANCE-gated, whereas here ANYONE can challenge by posting a
// bond.
//
// Increment 1 is the PRIMITIVE: validation, bond escrow, and state marking (`...+disputed`).
// RE-ADJUDICATION (drawing a FRESH committee through the beacon -> semantic re-settle -> slash of the
// guilty party or refund, or slash of the bond if the dispute fails) is increment 2.
//
// DORMANT by default: `dispute_window == 0` disables disputes, leaving e2e and devnet unchanged.
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
	// Only a SETTLED job can be challenged (a verdict exists), and only ONCE (dispute anti-replay).
	if !jobIsPaid(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "only a settled job can be challenged (paid/settled)")
	}
	if jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already challenged (anti-replay)")
	}
	// BOND (anti-grief): the disputer escrows `dispute_bond` udndr into the "jobs" MODULE account, the
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
	// ADR-034 partition — a human dispute OPENS an audit and is counted ONCE. If the job had already
	// been SELECTED and deferred by the lottery (AuditSelected), the opening was counted there; counting
	// it again would make the partition sum overshoot by one through overlap.
	// FAIL-CLOSED: the store error is raised, never folded into the predicate. Written as
	// `sErr == nil && !sel`, a store failure would silently SKIP the accounting, while
	// audit_sampling.go propagates its own — and two policies for one predicate mean whoever
	// "harmonises" them later picks one at random. Both are aligned on fail-closed: the error aborts
	// the tx and never silently skews the counter.
	sel, sErr := k.AuditSelected.Has(ctx, job.JobId)
	if sErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, sErr.Error())
	}
	if !sel {
		k.bumpAuditsOpened(ctx)
	}
	// ADR-033 — ANCHOR THE RE-ADJUDICATION COMMITTEE HERE, not later. The seed is the one of the
	// opening height: later than the original commits (so the miner cannot grind it) and earlier than
	// the re-commits (so the challenger cannot pick its judges after the fact).
	// Non-blocking: a draw failure leaves the anchor absent, and `AdjudicateDispute` then refuses to
	// move a single coin (fail closed) — the audit timeout decides instead.
	// ORIGIN MARK: once the job is `+disputed`, nothing distinguishes an opening by VRF sampling from
	// one by a human, and deadline-driven resolution must decide differently in the two cases (see
	// `humanDisputeKey`). It is therefore written while the information still exists.
	if err := k.AuditCommittee.Set(ctx, humanDisputeKey(job.JobId), msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if err := k.anchorRedoCommittee(ctx, job.JobId, job.MinerId, msg.Creator); err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("SECURITY: re-adjudication committee NOT anchored -> adjudication is fail-closed on this job (the audit timeout will decide).", "job_id", job.JobId, "err", err.Error())
	}
	// LIVENESS — AUTOMATIC DEADLINE. `runOptimisticAudit` schedules `PendingAuditResolve` for the jobs
	// IT samples; a HUMAN dispute on a never-sampled job schedules none, so without this block its only
	// exit is `AdjudicateDispute`, which requires a DRAWN committee. That pool is N-5 (excluding the
	// primary, the disputer and the three original members): too small, or with those drawn staying
	// silent, and the job would stay `+disputed` FOREVER — contradicting the claim in
	// `audit_sampling.go` that no job remains +disputed indefinitely. The ADR-033 committee requirement
	// makes that case structural, so it is paid for here: every dispute has a deadline, and
	// `resolveDisputedAudit` decides. A duplicate entry is harmless when the job is also sampled,
	// because resolution is idempotent (`jobIsResolved`).
	if p.AuditResolveTimeout > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+int64(p.AuditResolveTimeout), job.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	return &types.MsgDisputeVerdictResponse{}, nil
}

// ResolveDispute — resolves an OPEN dispute and settles the BOND.
// INTERIM: AUTHORITY-gated (gov), like report-divergence. PERMISSIONLESS self-resolution by a FRESH
// committee (re-run through the beacon -> semantic re-settle -> slash of the guilty party + reversal
// of the original slash) is increment 3; it requires off-chain re-execution and recording the original
// verdict. Here the dispute cycle is closed with the bond economics:
//   - upheld=true  (dispute VALID)    : the bond is REFUNDED to the disputer, plus a REWARD from the
//     Treasury (bounded by its balance) — the challenge was justified.
//   - upheld=false (dispute REJECTED) : the bond is SLASHED to the Treasury (anti-grief).
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
		// RESTITUTION first — the challenged verdict was WRONG, so the slashes recorded at settlement are
		// REVERSED. Each slashed miner's stake is re-credited (a plain counter: the coins have been inside
		// the module since the slash); bounded by the Treasury; non-replayable through "resolved".
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
		// Dispute VALID: refund the bond plus a reward (bounded by the REMAINING Treasury). The coins are
		// served from the module, which holds the escrow; the Treasury counter is reduced by the reward.
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
		// Dispute REJECTED: bond -> Treasury (anti-grief; the coins stay inside the module).
		pools.Treasury += bond
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	job.State = job.State + "+resolved"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	k.clearAuditCommittee(ctx, job.JobId) // same gesture as every other resolution: purge the anchored seats
	return &types.MsgResolveDisputeResponse{}, nil
}
