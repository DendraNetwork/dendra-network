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

func (q queryServer) ListJob(ctx context.Context, req *types.QueryAllJobRequest) (*types.QueryAllJobResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	jobs, pageRes, err := query.CollectionPaginate(
		ctx,
		q.k.Job,
		req.Pagination,
		func(_ string, value types.Job) (types.Job, error) {
			return value, nil
		},
	)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}

	return &types.QueryAllJobResponse{Job: jobs, Pagination: pageRes}, nil
}

func (q queryServer) GetJob(ctx context.Context, req *types.QueryGetJobRequest) (*types.QueryGetJobResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	val, err := q.k.Job.Get(ctx, req.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "not found")
		}

		return nil, status.Error(codes.Internal, "internal error")
	}

	return &types.QueryGetJobResponse{Job: val}, nil
}
