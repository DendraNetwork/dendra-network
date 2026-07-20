package keeper_test

import (
	"context"
	"strconv"
	"testing"

	"github.com/cosmos/cosmos-sdk/types/query"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

func createNMiner(keeper keeper.Keeper, ctx context.Context, n int) []types.Miner {
	items := make([]types.Miner, n)
	for i := range items {
		items[i].MinerId = strconv.Itoa(i)
		items[i].Operator = strconv.Itoa(i)
		items[i].Region = strconv.Itoa(i)
		items[i].Stake = uint64(i)
		_ = keeper.Miner.Set(ctx, items[i].MinerId, items[i])
	}
	return items
}

func TestMinerQuerySingle(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNMiner(f.keeper, f.ctx, 2)
	tests := []struct {
		desc     string
		request  *types.QueryGetMinerRequest
		response *types.QueryGetMinerResponse
		err      error
	}{
		{
			desc: "First",
			request: &types.QueryGetMinerRequest{
				MinerId: msgs[0].MinerId,
			},
			response: &types.QueryGetMinerResponse{Miner: msgs[0]},
		},
		{
			desc: "Second",
			request: &types.QueryGetMinerRequest{
				MinerId: msgs[1].MinerId,
			},
			response: &types.QueryGetMinerResponse{Miner: msgs[1]},
		},
		{
			desc: "KeyNotFound",
			request: &types.QueryGetMinerRequest{
				MinerId: strconv.Itoa(100000),
			},
			err: status.Error(codes.NotFound, "not found"),
		},
		{
			desc: "InvalidRequest",
			err:  status.Error(codes.InvalidArgument, "invalid request"),
		},
	}
	for _, tc := range tests {
		t.Run(tc.desc, func(t *testing.T) {
			response, err := qs.GetMiner(f.ctx, tc.request)
			if tc.err != nil {
				require.ErrorIs(t, err, tc.err)
			} else {
				require.NoError(t, err)
				require.EqualExportedValues(t, tc.response, response)
			}
		})
	}
}

func TestMinerQueryPaginated(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNMiner(f.keeper, f.ctx, 5)

	request := func(next []byte, offset, limit uint64, total bool) *types.QueryAllMinerRequest {
		return &types.QueryAllMinerRequest{
			Pagination: &query.PageRequest{
				Key:        next,
				Offset:     offset,
				Limit:      limit,
				CountTotal: total,
			},
		}
	}
	t.Run("ByOffset", func(t *testing.T) {
		step := 2
		for i := 0; i < len(msgs); i += step {
			resp, err := qs.ListMiner(f.ctx, request(nil, uint64(i), uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Miner), step)
			require.Subset(t, msgs, resp.Miner)
		}
	})
	t.Run("ByKey", func(t *testing.T) {
		step := 2
		var next []byte
		for i := 0; i < len(msgs); i += step {
			resp, err := qs.ListMiner(f.ctx, request(next, 0, uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Miner), step)
			require.Subset(t, msgs, resp.Miner)
			next = resp.Pagination.NextKey
		}
	})
	t.Run("Total", func(t *testing.T) {
		resp, err := qs.ListMiner(f.ctx, request(nil, 0, 0, true))
		require.NoError(t, err)
		require.Equal(t, len(msgs), int(resp.Pagination.Total))
		require.EqualExportedValues(t, msgs, resp.Miner)
	})
	t.Run("InvalidRequest", func(t *testing.T) {
		_, err := qs.ListMiner(f.ctx, nil)
		require.ErrorIs(t, err, status.Error(codes.InvalidArgument, "invalid request"))
	})
}
