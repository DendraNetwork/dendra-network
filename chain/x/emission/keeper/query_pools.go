package keeper

import (
	"context"
	"errors"

	"cosmossdk.io/collections"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// Pools returns the six counters of the engine: what is left to release, what
// each claimable pool still holds, and what has left the module per flow. The module account balance
// is one opaque sum; this split lets a reader check `balance(emission) == reserve + work_pool +
// avail_pool` (the identity RunEpoch states) and tell a work payout from a security transfer.
//
// An absent counter is 0, which is its measured value: nothing has been written to it yet (a fresh
// chain before its first epoch, or an upgraded chain before its first payout). Any other read error
// is an internal error, never a zero.
func (q queryServer) Pools(ctx context.Context, req *types.QueryPoolsRequest) (*types.QueryPoolsResponse, error) {
	if req == nil {
		return nil, status.Error(codes.InvalidArgument, "invalid request")
	}
	read := func(item collections.Item[uint64]) (uint64, error) {
		v, err := item.Get(ctx)
		if errors.Is(err, collections.ErrNotFound) {
			return 0, nil
		}
		return v, err
	}
	resp := &types.QueryPoolsResponse{}
	for _, c := range []struct {
		item collections.Item[uint64]
		dst  *uint64
	}{
		{q.k.Reserve, &resp.Reserve},
		{q.k.WorkPool, &resp.WorkPool},
		{q.k.AvailPool, &resp.AvailPool},
		{q.k.SecurityDistributedTotal, &resp.SecurityDistributedTotal},
		{q.k.WorkPaidTotal, &resp.WorkPaidTotal},
		{q.k.AvailPaidTotal, &resp.AvailPaidTotal},
	} {
		v, err := read(c.item)
		if err != nil {
			return nil, status.Error(codes.Internal, "internal error")
		}
		*c.dst = v
	}
	return resp, nil
}
