package keeper

import (
	"bytes"
	"context"
	"errors"
	"strings"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"dendra/x/modelregistry/types"
)

// RegisterModel — enregistre/active un modèle autorisé (gouvernance). L'ancre de confiance est
// `weights_sha256` : à terme, la chaîne ne paiera que de l'inférence dont le modèle servi
// correspond à un Model enregistré + actif.
func (k msgServer) RegisterModel(ctx context.Context, req *types.MsgRegisterModel) (*types.MsgRegisterModelResponse, error) {
	authority, err := k.addressCodec.StringToBytes(req.Authority)
	if err != nil {
		return nil, errorsmod.Wrap(err, "invalid authority address")
	}
	if !bytes.Equal(k.GetAuthority(), authority) {
		expectedAuthorityStr, _ := k.addressCodec.BytesToString(k.GetAuthority())
		return nil, errorsmod.Wrapf(types.ErrInvalidSigner, "invalid authority; expected %s, got %s", expectedAuthorityStr, req.Authority)
	}

	m := req.Model
	if strings.TrimSpace(m.Id) == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "model id requis")
	}
	if strings.TrimSpace(m.WeightsSha256) == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "weights_sha256 requis (attestation des poids)")
	}
	// INT-8 : ancrage Hugging Face optionnel. Un repo SANS révision = source MUTABLE qui annule le pin
	// -> si hf_repo est fourni, hf_revision (commit SHA / tag immuable) est OBLIGATOIRE.
	if strings.TrimSpace(m.HfRepo) != "" && strings.TrimSpace(m.HfRevision) == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"hf_revision requis quand hf_repo est fourni (ancrage immuable : commit SHA, pas une branche)")
	}
	m.Active = true // enregistrer = activer
	if err := k.Models.Set(ctx, m.Id, m); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgRegisterModelResponse{}, nil
}

// DeregisterModel — désactive un modèle (gouvernance). On conserve l'entrée (Active=false) pour l'audit.
func (k msgServer) DeregisterModel(ctx context.Context, req *types.MsgDeregisterModel) (*types.MsgDeregisterModelResponse, error) {
	authority, err := k.addressCodec.StringToBytes(req.Authority)
	if err != nil {
		return nil, errorsmod.Wrap(err, "invalid authority address")
	}
	if !bytes.Equal(k.GetAuthority(), authority) {
		expectedAuthorityStr, _ := k.addressCodec.BytesToString(k.GetAuthority())
		return nil, errorsmod.Wrapf(types.ErrInvalidSigner, "invalid authority; expected %s, got %s", expectedAuthorityStr, req.Authority)
	}

	m, err := k.Models.Get(ctx, req.Id)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "modele inconnu")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	m.Active = false
	if err := k.Models.Set(ctx, req.Id, m); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgDeregisterModelResponse{}, nil
}
