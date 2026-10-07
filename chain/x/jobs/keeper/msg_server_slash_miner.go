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

func (k msgServer) SlashMiner(ctx context.Context, msg *types.MsgSlashMiner) (*types.MsgSlashMinerResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// A MANUAL slash is reserved to the module AUTHORITY (governance). The bond is escrowed in REAL
	// coins, so an ungated slash would let ANYONE DESTROY an honest miner's bond (grief). The
	// legitimate, automatic slash goes through the committee path (verify/settle/finalize) and is
	// determined by the ON-CHAIN commits, not by the caller.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "slash-miner is reserved to the authority (gov)")
	}

	miner, err := k.Miner.Get(ctx, msg.MinerId)
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

	// Slash slash_leak_bps (80 %) of the stake; the amount feeds the Treasury, which funds compensation.
	// Because the bond is ESCROWED in real coins at create-miner, reducing `Stake` removes a REAL
	// amount from what is refundable (delete-miner only returns the remainder); the slashed coins stay
	// in the module account. The slash therefore has a bankable effect, not merely a counter change.
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

	return &types.MsgSlashMinerResponse{}, nil
}
