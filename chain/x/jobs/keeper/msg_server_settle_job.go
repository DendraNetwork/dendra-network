package keeper

import (
	"bytes"
	"context"
	"errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// SettleJob IS A v1 VESTIGE, AND IT IS DANGEROUS WITHOUT A GATE.
//
// This handler settles a job — marks `+settled`, credits the pools, increments demand — on the SOLE
// basis of the format of the signer address: no commit read, no committee, no verification. Optimistic
// mode settles through `SettleSemantic`/`settleOptimistic`, redundant mode through `SettleSemantic`
// too; the production stack NEVER calls `settle-job` (only legacy demo scripts do). Left
// permissionless, anyone can call `SettleJob` on any unsettled job, set `+settled`, and ALL the real
// settlement paths then refuse with "job already settled": the escrow stays locked in the module
// FOREVER, the miner is never paid, and the pool counters are credited as if it had been (accounting
// divergence). Cost = the gas, replayable on every job as soon as it opens. This is the purest form of
// "capital moved on no authenticated basis".
//
// Closed by reserving it to the AUTHORITY, like `SlashMiner`/`ResolveDispute`: a third party can no
// longer freeze an escrow. CANDIDATE FOR FULL REMOVAL at the next proto regeneration — a raw settlement
// RPC has no reason to exist next to the verified paths; keeping it gated is the prudent measure until
// the proto removal is done and tested.
func (k msgServer) SettleJob(ctx context.Context, msg *types.MsgSettleJob) (*types.MsgSettleJobResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}

	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if jobIsPaid(job.State) { // UNIFIED replay guard (paid||settled) -> no re-settlement, no Demand inflation
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job already settled")
	}
	// AUTHORITY GATE — placed AFTER the replay guard on purpose: already-settled paths answer "job
	// already settled" (the unified replay test requires it), and only an UNSETTLED job reaches here.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "settle-job (v1 vestige) is reserved to the authority; use settle-semantic for a verified settlement")
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	cut := job.Fee * params.ProtocolFeeBps / 10000
	minerGross := job.Fee - cut
	minerLocked := minerGross * params.MinerVestBps / 10000
	minerLiquid := minerGross - minerLocked
	validators := cut * params.ValidatorRewardBps / 10000
	team := cut * params.TeamFeeBps / 10000
	treasury := cut - validators - team

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.MinerPaid += minerLiquid
	pools.MinerLocked += minerLocked
	pools.Validators += validators
	pools.Team += team
	pools.Treasury += treasury
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// NON-RECOVERABLE demand (ADR-017): treasury + team, EXCEPT self-dealing (client == operator)
	if miner, mErr := k.Miner.Get(ctx, job.MinerId); mErr == nil {
		if job.Client != miner.Operator {
			miner.Demand += treasury + team
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
		}
	}

	job.State = job.State + "+settled" // APPEND (do NOT overwrite existing markers: +verified/+finalized)
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgSettleJobResponse{}, nil
}
