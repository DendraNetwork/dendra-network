package keeper

import (
	"bytes"
	"context"
	"errors"
	"strings"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

// RegisterModel registers and activates an authorised model (governance). The trust anchor is
// `weights_sha256`: in the end state, the chain only pays for inference whose served model matches a
// registered and active Model.
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
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "model id is required")
	}
	if strings.TrimSpace(m.WeightsSha256) == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "weights_sha256 is required (weights attestation)")
	}
	// Optional Hugging Face anchoring. A repo WITHOUT a revision is a MUTABLE source that defeats the
	// pin, so whenever hf_repo is supplied, hf_revision (an immutable commit SHA or tag) is MANDATORY.
	if strings.TrimSpace(m.HfRepo) != "" && strings.TrimSpace(m.HfRevision) == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"hf_revision is required when hf_repo is supplied (immutable anchor: a commit SHA, not a branch)")
	}
	m.Active = true // registering means activating
	if err := k.Models.Set(ctx, m.Id, m); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgRegisterModelResponse{}, nil
}

// DeregisterModel deactivates a model (governance). The entry is kept (Active=false) for auditability.
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
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown model")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	m.Active = false
	if err := k.Models.Set(ctx, req.Id, m); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgDeregisterModelResponse{}, nil
}
