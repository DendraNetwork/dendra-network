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

// RewardTraining — les mineurs sont payés pour l'ENTRAÎNEMENT (compute validé), pas seulement
// l'inférence : c'est le 2e pilier de Dendra. Récompense = compute validé × taux, versée au pool
// `training_paid`. (Démo : taux fixe 10/unité ; gouvernable en prod. La validation par spot-check /
// redondance reste hors scope de ce handler ; ici on pose le versement on-chain.)
func (k msgServer) RewardTraining(ctx context.Context, msg *types.MsgRewardTraining) (*types.MsgRewardTrainingResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// NEW-GO-36 (audit v2) : la récompense d'entraînement est une action PROTOCOLE → réservée à
	// l'AUTORITÉ (gov). Sans ça, n'importe qui gonflait `TrainingPaid` de
	// n'importe quel mineur.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "reward-training reserve a l'autorite (gov)")
	}

	// le mineur doit exister (bond requis pour contribuer à l'entraînement)
	if _, err := k.Miner.Get(ctx, msg.MinerId); err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// NEW-GO-36 : borne `Units` → pas d'overflow uint64 sur `reward = Units * ratePerUnit`.
	const ratePerUnit = 10
	const maxUnits = 1_000_000_000_000_000 // 1e15 → reward ≤ 1e16, très loin de MaxUint64
	if msg.Units > maxUnits {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "units hors borne")
	}
	reward := msg.Units * ratePerUnit

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.TrainingPaid += reward
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgRewardTrainingResponse{}, nil
}
