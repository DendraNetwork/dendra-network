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

func createNJob(keeper keeper.Keeper, ctx context.Context, n int) []types.Job {
	items := make([]types.Job, n)
	for i := range items {
		items[i].JobId = strconv.Itoa(i)
		items[i].Client = strconv.Itoa(i)
		items[i].MinerId = strconv.Itoa(i)
		items[i].Fee = uint64(i)
		items[i].State = strconv.Itoa(i)
		_ = keeper.Job.Set(ctx, items[i].JobId, items[i])
	}
	return items
}

func TestJobQuerySingle(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNJob(f.keeper, f.ctx, 2)
	tests := []struct {
		desc     string
		request  *types.QueryGetJobRequest
		response *types.QueryGetJobResponse
		err      error
	}{
		{
			desc: "First",
			request: &types.QueryGetJobRequest{
				JobId: msgs[0].JobId,
			},
			response: &types.QueryGetJobResponse{Job: msgs[0]},
		},
		{
			desc: "Second",
			request: &types.QueryGetJobRequest{
				JobId: msgs[1].JobId,
			},
			response: &types.QueryGetJobResponse{Job: msgs[1]},
		},
		{
			desc: "KeyNotFound",
			request: &types.QueryGetJobRequest{
				JobId: strconv.Itoa(100000),
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
			response, err := qs.GetJob(f.ctx, tc.request)
			if tc.err != nil {
				require.ErrorIs(t, err, tc.err)
			} else {
				require.NoError(t, err)
				require.EqualExportedValues(t, tc.response, response)
			}
		})
	}
}

func TestJobQueryPaginated(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNJob(f.keeper, f.ctx, 5)

	request := func(next []byte, offset, limit uint64, total bool) *types.QueryAllJobRequest {
		return &types.QueryAllJobRequest{
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
			resp, err := qs.ListJob(f.ctx, request(nil, uint64(i), uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Job), step)
			require.Subset(t, msgs, resp.Job)
		}
	})
	t.Run("ByKey", func(t *testing.T) {
		step := 2
		var next []byte
		for i := 0; i < len(msgs); i += step {
			resp, err := qs.ListJob(f.ctx, request(next, 0, uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Job), step)
			require.Subset(t, msgs, resp.Job)
			next = resp.Pagination.NextKey
		}
	})
	t.Run("Total", func(t *testing.T) {
		resp, err := qs.ListJob(f.ctx, request(nil, 0, 0, true))
		require.NoError(t, err)
		require.Equal(t, len(msgs), int(resp.Pagination.Total))
		require.EqualExportedValues(t, msgs, resp.Job)
	})
	t.Run("InvalidRequest", func(t *testing.T) {
		_, err := qs.ListJob(f.ctx, nil)
		require.ErrorIs(t, err, status.Error(codes.InvalidArgument, "invalid request"))
	})
}
