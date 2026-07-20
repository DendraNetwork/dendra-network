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

func (q queryServer) ListBeacon(ctx context.Context, req *types.QueryAllBeaconRequest) (*types.QueryAllBeaconResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	beacons, pageRes, err := query.CollectionPaginate(
		ctx,
		q.k.Beacon,
		req.Pagination,
		func(_ string, value types.Beacon) (types.Beacon, error) {
			return value, nil
		},
	)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}

	return &types.QueryAllBeaconResponse{Beacon: beacons, Pagination: pageRes}, nil
}

func (q queryServer) GetBeacon(ctx context.Context, req *types.QueryGetBeaconRequest) (*types.QueryGetBeaconResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}

	val, err := q.k.Beacon.Get(ctx, req.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, status.Error(codes.NotFound, "not found")
		}

		return nil, status.Error(codes.Internal, "internal error")
	}

	return &types.QueryGetBeaconResponse{Beacon: val}, nil
}
