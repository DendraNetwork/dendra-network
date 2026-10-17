package keeper

import (
	"context"
	"errors"

	"cosmossdk.io/collections"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ValidatorVrfKey reads back the VRF public key anchored for an operator.
//
// Nothing else could read it. `MsgRegisterValidatorVrfKey` writes this entry and no query exposed it,
// so an operator could only learn whether its anchoring had landed from the receipt of its own
// transaction, which it may not have kept, or from the node's diagnostic log once bonded, which is too
// late. A validator bonded without an anchored key contributes nothing to the decentralized seed while
// every published indicator reports it healthy (synced, never jailed, never absent), and the operator
// had no way to check the one thing that would have shown it.
//
// Absence is an answer, not an error. No anchored key yields `anchored=false` and a successful call;
// gRPC errors are reserved for a real failure to read the store. A caller must be able to tell "there
// is no key" from "I could not find out", and merging the two would let an unknown read as reassurance
// on a criterion that decides whether a validator counts at all.
func (q queryServer) ValidatorVrfKey(ctx context.Context, req *types.QueryValidatorVrfKeyRequest) (*types.QueryValidatorVrfKeyResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	if req.Operator == "" {
		return nil, status.Error(codes.InvalidArgument, "operator address is required")
	}
	// The address is validated rather than used raw: a typo would otherwise come back as a confident
	// "not anchored", which reads like a missing anchoring and sends the operator to re-run a
	// registration that already succeeded under the address they meant.
	if _, err := q.k.addressCodec.StringToBytes(req.Operator); err != nil {
		return nil, status.Error(codes.InvalidArgument, "operator is not a valid address: "+err.Error())
	}

	resp := &types.QueryValidatorVrfKeyResponse{Operator: req.Operator}
	pk, err := q.k.ValidatorVrfPubkey.Get(ctx, req.Operator)
	switch {
	case err == nil:
		resp.PubkeyHex = pk
		resp.Anchored = pk != ""
	case errors.Is(err, collections.ErrNotFound):
		// Nothing anchored: a fact, reported as one.
	default:
		return nil, status.Error(codes.Internal, err.Error())
	}
	return resp, nil
}
