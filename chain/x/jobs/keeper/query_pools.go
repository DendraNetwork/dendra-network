package keeper

import (
	"context"
	"errors"

	"cosmossdk.io/collections"

	"dendra/x/jobs/types"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (q queryServer) GetPools(ctx context.Context, req *types.QueryGetPoolsRequest) (*types.QueryGetPoolsResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	val, err := q.k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "not found")
		}

		return nil, status.Error(codes.Internal, "internal error")
	}

	return &types.QueryGetPoolsResponse{Pools: val}, nil
}
