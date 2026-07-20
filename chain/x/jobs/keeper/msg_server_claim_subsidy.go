package keeper

import (
	"context"
	"errors"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

func (k msgServer) ClaimSubsidy(ctx context.Context, msg *types.MsgClaimSubsidy) (*types.MsgClaimSubsidyResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}

	miner, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// NEW-GO-37 (audit v2) : seul l'OPÉRATEUR du mineur peut réclamer SA subvention. Sinon un tiers
	// épuise le quota (SubsidyClaimed → cap) d'un concurrent qui ne touche rien = grief.
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "seul l'operateur du mineur peut reclamer sa subvention")
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// subvention GATEE par la demande non-recuperable (ADR-017): plafond = work_gate * demande
	subsidyCap := miner.Demand * params.WorkGateBps / 10000
	if subsidyCap <= miner.SubsidyClaimed {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "rien a reclamer (plafond atteint ou demande nulle)")
	}
	pay := subsidyCap - miner.SubsidyClaimed

	// Phase 1a : VERSER en VRAIS coins depuis le WorkPool de l'emission (au lieu d'un simple compteur).
	// PayWork renvoie le montant REELLEMENT verse (< pay si le pool est insuffisant) -> on ne credite
	// SubsidyClaimed / MinerPaid que de ce qui a ete paye (le reste reste reclamable a la prochaine epoque).
	opBz, err := k.addressCodec.StringToBytes(miner.Operator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "adresse operateur invalide")
	}
	paid, err := k.emissionKeeper.PayWork(ctx, sdk.AccAddress(opBz), pay)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if paid == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pool de travail (emission) vide -- reessayer apres la prochaine epoque")
	}
	miner.SubsidyClaimed += paid
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
	pools.MinerPaid += paid
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgClaimSubsidyResponse{}, nil
}
