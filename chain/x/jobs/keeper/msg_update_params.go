package keeper

import (
	"bytes"
	"context"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
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

	// INVARIANT #8 (anti-bubble) — RUNTIME GUARD: refuse any params update that would make Sybil
	// self-dealing on Demand +EV, i.e. an emission drain through the WorkPool subsidy, capped at
	// Demand x WorkGateBps in msg_server_claim_subsidy.go. `params_invariant_test.go` only freezes the
	// compiled DEFAULT; this closes the LIVE GOVERNANCE path (raising work_gate_bps, or shifting the
	// split from cut towards team/treasury). The washed subsidy uses the jobs module's own WorkGateBps,
	// so the invariant is entirely intra-module.
	if !req.Params.WashSubsidyNegativeEV(req.Params.WorkGateBps) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"invariant #8 violated: these params would make Sybil self-dealing +EV (emission drain); lower work_gate_bps OR raise cut+burn against team+treasury")
	}

	if err := k.Params.Set(ctx, req.Params); err != nil {
		return nil, err
	}

	return &types.MsgUpdateParamsResponse{}, nil
}
