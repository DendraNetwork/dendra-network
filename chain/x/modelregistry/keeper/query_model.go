package keeper

import (
	"context"
	"errors"

	"cosmossdk.io/collections"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

// Model returns a registered model by its id.
func (q queryServer) Model(ctx context.Context, req *types.QueryModelRequest) (*types.QueryModelResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	m, err := q.k.Models.Get(ctx, req.Id)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "unknown model")
		}
		return nil, status.Error(codes.Internal, "internal error")
	}
	return &types.QueryModelResponse{Model: m}, nil
}

// Models lists every registered model.
func (q queryServer) Models(ctx context.Context, req *types.QueryModelsRequest) (*types.QueryModelsResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	var models []types.Model
	if err := q.k.Models.Walk(ctx, nil, func(_ string, m types.Model) (bool, error) {
		models = append(models, m)
		return false, nil
	}); err != nil {
		return nil, status.Error(codes.Internal, "internal error")
	}
	return &types.QueryModelsResponse{Models: models}, nil
}
