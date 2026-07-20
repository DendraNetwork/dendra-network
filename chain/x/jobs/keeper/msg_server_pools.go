package keeper

import (
	"context"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// NEW-GO-34 (audit v2) — CRUD scaffold NEUTRALISÉ.
// Le singleton `Pools` (comptabilité Treasury/Team/Validators/MinerPaid) est géré par le PROTOCOLE :
// créé paresseusement au règlement (`settle_semantic`/`finalize_job` font `Pools.Get` avec repli
// `types.Pools{}` si absent), à terme posé au genesis. Le CRUD scaffold public laissait le PREMIER
// appelant créer/posséder/falsifier la comptabilité (compteurs arbitraires) et la DoS (`DeletePools`
// → `SettleJob`/`ClaimSubsidy` cassent). On le rejette (modèle `msg_server_beacon.go`).
func (k msgServer) CreatePools(ctx context.Context, msg *types.MsgCreatePools) (*types.MsgCreatePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "pools gere par le protocole (reglement / genesis)")
}

func (k msgServer) UpdatePools(ctx context.Context, msg *types.MsgUpdatePools) (*types.MsgUpdatePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "pools non modifiable hors protocole")
}

func (k msgServer) DeletePools(ctx context.Context, msg *types.MsgDeletePools) (*types.MsgDeletePoolsResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "suppression de pools interdite (DoS reglement)")
}
