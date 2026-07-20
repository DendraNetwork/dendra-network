package keeper

import (
	"context"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// NEW-GO-30 / NEW-GO-31 (audit v2) — CRUD scaffold NEUTRALISÉ.
// Un job ne peut être créé/modifié/supprimé que par le PROTOCOLE : `OpenJob` est le SEUL chemin de
// création (il SÉQUESTRE la fee = escrow réel) et le règlement gère l'état. Les CRUD scaffold ignite
// écrivaient le MÊME état `Job{Fee,State}` SANS escrow et SANS garde → `CreateJob` rouvrait le drain
// inter-jobs (GO-02 ré-ouvert), `UpdateJob` resettait l'anti-rejeu, `DeleteJob` piégeait l'escrow.
// On les rejette tous (modèle `msg_server_beacon.go`).
func (k msgServer) CreateJob(ctx context.Context, msg *types.MsgCreateJob) (*types.MsgCreateJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "job cree uniquement via open-job (escrow reel)")
}

func (k msgServer) UpdateJob(ctx context.Context, msg *types.MsgUpdateJob) (*types.MsgUpdateJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "job immuable cote utilisateur (etat gere par le reglement)")
}

func (k msgServer) DeleteJob(ctx context.Context, msg *types.MsgDeleteJob) (*types.MsgDeleteJobResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "suppression de job interdite (escrow / anti-rejeu)")
}
