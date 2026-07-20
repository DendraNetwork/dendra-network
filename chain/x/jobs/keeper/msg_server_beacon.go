package keeper

import (
	"context"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// H6 -- Le beacon d'un job est gere UNIQUEMENT par le protocole (open-job, a partir d'un alea de
// bloc). On NEUTRALISE donc les messages publics create/update/delete-beacon : sinon n'importe qui
// pourrait fixer/changer le beacon d'un job pour grinder le comite. Le beacon est ainsi immuable
// cote utilisateur (seul `open-job` l'ecrit, via le keeper).
func (k msgServer) CreateBeacon(ctx context.Context, msg *types.MsgCreateBeacon) (*types.MsgCreateBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon gere par le protocole (open-job)")
}

func (k msgServer) UpdateBeacon(ctx context.Context, msg *types.MsgUpdateBeacon) (*types.MsgUpdateBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon immuable (gere par open-job)")
}

func (k msgServer) DeleteBeacon(ctx context.Context, msg *types.MsgDeleteBeacon) (*types.MsgDeleteBeaconResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "beacon immuable (gere par open-job)")
}
