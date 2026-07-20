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

func (q queryServer) ListCommit(ctx context.Context, req *types.QueryAllCommitRequest) (*types.QueryAllCommitResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	commits, pageRes, err := query.CollectionPaginate(
		ctx,
		q.k.Commit,
		req.Pagination,
		func(_ string, value types.Commit) (types.Commit, error) {
			return value, nil
		},
	)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}

	return &types.QueryAllCommitResponse{Commit: commits, Pagination: pageRes}, nil
}

func (q queryServer) GetCommit(ctx context.Context, req *types.QueryGetCommitRequest) (*types.QueryGetCommitResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	val, err := q.k.Commit.Get(ctx, req.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "not found")
		}

		return nil, status.Error(codes.Internal, "internal error")
	}

	return &types.QueryGetCommitResponse{Commit: val}, nil
}
