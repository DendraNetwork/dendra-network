package keeper_test

import (
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
)

func TestAuditDeferredAbsentAndRecorded(t *testing.T) {
	f := initFixture(t)
	q := keeper.NewQueryServerImpl(f.keeper)
	_, err := q.AuditDeferred(f.ctx, nil)
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	got, err := q.AuditDeferred(f.ctx, &types.QueryAuditDeferredRequest{})
	require.NoError(t, err)
	require.Empty(t, got.JobIds)
	require.Zero(t, got.AuditsOpened)
	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "job-b"))
	require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "job-a"))
	require.NoError(t, f.keeper.AuditsOpened.Set(f.ctx, 3))
	got, err = q.AuditDeferred(f.ctx, &types.QueryAuditDeferredRequest{})
	require.NoError(t, err)
	require.Equal(t, []string{"job-a", "job-b"}, got.JobIds)
	require.Equal(t, uint64(3), got.AuditsOpened)
}
