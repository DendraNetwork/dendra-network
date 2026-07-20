package keeper

import (
	"bytes"
	"context"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"dendra/x/jobs/types"
)

func (k msgServer) UpdateParams(ctx context.Context, req *types.MsgUpdateParams) (*types.MsgUpdateParamsResponse, error) {
	authority, err := k.addressCodec.StringToBytes(req.Authority)
	if err != nil {
		return nil, errorsmod.Wrap(err, "invalid authority address")
	}

	if !bytes.Equal(k.GetAuthority(), authority) {
		expectedAuthorityStr, _ := k.addressCodec.BytesToString(k.GetAuthority())
		return nil, errorsmod.Wrapf(types.ErrInvalidSigner, "invalid authority; expected %s, got %s", expectedAuthorityStr, req.Authority)
	}

	if err := req.Params.Validate(); err != nil {
		return nil, err
	}

	// INVARIANT #8 (anti-bulle, internal audit 2026-06-26) — GARDE RUNTIME : refuser toute MAJ de params qui rendrait
	// le self-dealing Sybil sur Demand +EV (= drain d'émission via le subside WorkPool, plafonné à
	// Demand×WorkGateBps dans msg_server_claim_subsidy.go:39). Le test `params_invariant_test.go` ne fige que le
	// DÉFAUT code ; ceci ferme la voie GOUVERNANCE LIVE (hausse de work_gate_bps OU glissement du split
	// cut->team/treasury). Le subside washé utilise le WorkGateBps de JOBS -> invariant entièrement intra-module.
	if !req.Params.WashSubsidyNegativeEV(req.Params.WorkGateBps) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"invariant #8 viole : ces params rendraient le self-dealing Sybil +EV (drain d'emission) ; baisse work_gate_bps OU augmente cut+burn vs team+treasury")
	}

	if err := k.Params.Set(ctx, req.Params); err != nil {
		return nil, err
	}

	return &types.MsgUpdateParamsResponse{}, nil
}
