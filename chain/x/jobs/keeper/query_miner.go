package keeper

import (
	"context"
	"errors"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	"github.com/cosmos/cosmos-sdk/types/query"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (q queryServer) ListMiner(ctx context.Context, req *types.QueryAllMinerRequest) (*types.QueryAllMinerResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	miners, pageRes, err := query.CollectionPaginate(
		ctx,
		q.k.Miner,
		req.Pagination,
		func(_ string, value types.Miner) (types.Miner, error) {
			return value, nil
		},
	)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}

	return &types.QueryAllMinerResponse{Miner: miners, Pagination: pageRes}, nil
}

func (q queryServer) GetMiner(ctx context.Context, req *types.QueryGetMinerRequest) (*types.QueryGetMinerResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	val, err := q.k.Miner.Get(ctx, req.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "not found")
		}

		return nil, status.Error(codes.Internal, "internal error")
	}

	return &types.QueryGetMinerResponse{Miner: val}, nil
}
