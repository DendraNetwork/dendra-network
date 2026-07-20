package keeper

import (
	"bytes"
	"context"
	"errors"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

func (k msgServer) SlashMiner(ctx context.Context, msg *types.MsgSlashMiner) (*types.MsgSlashMinerResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// GO-13 (durcissement) : SLASH MANUEL réservé à l'AUTORITÉ (gov). Le bond étant désormais séquestré
	// en VRAIS coins, un slash non gaté laisserait N'IMPORTE QUI DÉTRUIRE le bond d'un mineur honnête
	// (grief). Le slash LÉGITIME automatique passe par le comité (verify/settle/finalize), déterminé par
	// les commits ON-CHAIN — pas par l'appelant.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "slash-miner réservé à l'autorité (gov)")
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

	// slash de slash_leak_bps (80%) du stake ; le montant alimente la tresorerie (indemnisation).
	// GO-13 : le bond étant désormais SÉQUESTRÉ en vrais coins au create-miner, réduire `Stake` retire
	// un montant RÉEL du remboursable (delete-miner ne rendra que le restant) ; les coins slashés restent
	// au compte de module (trésorerie). Le slash a donc un effet bankable effectif, pas un simple compteur.
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
