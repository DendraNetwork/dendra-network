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

func createNCommit(keeper keeper.Keeper, ctx context.Context, n int) []types.Commit {
	items := make([]types.Commit, n)
	for i := range items {
		items[i].JobId = strconv.Itoa(i)
		items[i].PromptCommit = strconv.Itoa(i)
		items[i].ResultCommit = strconv.Itoa(i)
		items[i].Kind = strconv.Itoa(i)
		_ = keeper.Commit.Set(ctx, items[i].JobId, items[i])
	}
	return items
}

func TestCommitQuerySingle(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNCommit(f.keeper, f.ctx, 2)
	tests := []struct {
		desc     string
		request  *types.QueryGetCommitRequest
		response *types.QueryGetCommitResponse
		err      error
	}{
		{
			desc: "First",
			request: &types.QueryGetCommitRequest{
				JobId: msgs[0].JobId,
			},
			response: &types.QueryGetCommitResponse{Commit: msgs[0]},
		},
		{
			desc: "Second",
			request: &types.QueryGetCommitRequest{
				JobId: msgs[1].JobId,
			},
			response: &types.QueryGetCommitResponse{Commit: msgs[1]},
		},
		{
			desc: "KeyNotFound",
			request: &types.QueryGetCommitRequest{
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
			response, err := qs.GetCommit(f.ctx, tc.request)
			if tc.err != nil {
				require.ErrorIs(t, err, tc.err)
			} else {
				require.NoError(t, err)
				require.EqualExportedValues(t, tc.response, response)
			}
		})
	}
}

func TestCommitQueryPaginated(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	msgs := createNCommit(f.keeper, f.ctx, 5)

	request := func(next []byte, offset, limit uint64, total bool) *types.QueryAllCommitRequest {
		return &types.QueryAllCommitRequest{
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
			resp, err := qs.ListCommit(f.ctx, request(nil, uint64(i), uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Commit), step)
			require.Subset(t, msgs, resp.Commit)
		}
	})
	t.Run("ByKey", func(t *testing.T) {
		step := 2
		var next []byte
		for i := 0; i < len(msgs); i += step {
			resp, err := qs.ListCommit(f.ctx, request(next, 0, uint64(step), false))
			require.NoError(t, err)
			require.LessOrEqual(t, len(resp.Commit), step)
			require.Subset(t, msgs, resp.Commit)
			next = resp.Pagination.NextKey
		}
	})
	t.Run("Total", func(t *testing.T) {
		resp, err := qs.ListCommit(f.ctx, request(nil, 0, 0, true))
		require.NoError(t, err)
		require.Equal(t, len(msgs), int(resp.Pagination.Total))
		require.EqualExportedValues(t, msgs, resp.Commit)
	})
	t.Run("InvalidRequest", func(t *testing.T) {
		_, err := qs.ListCommit(f.ctx, nil)
		require.ErrorIs(t, err, status.Error(codes.InvalidArgument, "invalid request"))
	})
}
