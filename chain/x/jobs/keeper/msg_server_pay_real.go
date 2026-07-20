package keeper

import (
	"context"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// PayReal — paiement en VRAIS tokens via le module bank : le signataire envoie `amount` de "udndr"
// au destinataire. Démontre que le module jobs orchestre un mouvement de coins RÉELS (en prod, l'escrow
// à l'open-job et les paiements au settle passeront par ce mécanisme, via un compte de module).
func (k msgServer) PayReal(ctx context.Context, msg *types.MsgPayReal) (*types.MsgPayRealResponse, error) {
	fromBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	toBz, err := k.addressCodec.StringToBytes(msg.Recipient)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid recipient address")
	}

	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Amount))) // GO-11 : denom unique udndr (etait "token")
	if err := k.bankKeeper.SendCoins(ctx, sdk.AccAddress(fromBz), sdk.AccAddress(toBz), coins); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
	}

	return &types.MsgPayRealResponse{}, nil
}
