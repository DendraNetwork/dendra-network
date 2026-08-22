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

// ReportDivergence is the S2 verification entry point (ADR-020): a committee or verifier reports that the
// miner's result (miner_commit) diverges from the canonical result (correct_commit). A proven divergence
// slashes the job's miner; the slashed amount funds the treasury, which is what compensates the client.
//
// The "proof" (honest majority, cosine distance, canary) is supplied by the CALLER here. In production
// this message is gated by the verification module (on-chain vote plus semantic oracle).
func (k msgServer) ReportDivergence(ctx context.Context, msg *types.MsgReportDivergence) (*types.MsgReportDivergenceResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// The divergence "proof" is supplied by the CALLER (msg.*Commit), so it is forgeable. With a bond
	// denominated in real coins, leaving this handler permissionless is a griefing vector: a false report
	// slashes an honest miner and destroys its bond. The handler is therefore restricted to the AUTHORITY
	// (governance) until the proof is verified on-chain — the legitimate automatic slash goes through the
	// committee (verify/settle). Lifting the restriction requires an on-chain check of the miner's actual
	// commit against the committee's canonical one.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "report-divergence is restricted to the governance authority until the proof is verified on-chain")
	}

	// No divergence: nothing to do, the miner returned the correct result.
	if msg.MinerCommit == msg.CorrectCommit {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no divergence (commits are identical)")
	}

	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	miner, err := k.Miner.Get(ctx, job.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// Proven divergence: slash at slash_leak_bps; the amount goes to the treasury.
	amt := miner.Stake * params.SlashLeakBps / 10000
	miner.Stake -= amt
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.Treasury += amt
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgReportDivergenceResponse{}, nil
}
