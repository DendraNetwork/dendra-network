package keeper_test

import (
	"github.com/stretchr/testify/require"
	"testing"
)

func TestAvailPoolBalanceMissingZeroAndPositive(t *testing.T) {
	f := initFixture(t)
	got, err := f.keeper.AvailPoolBalance(f.ctx)
	require.NoError(t, err)
	require.Zero(t, got)
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 0))
	got, err = f.keeper.AvailPoolBalance(f.ctx)
	require.NoError(t, err)
	require.Zero(t, got)
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 19))
	got, err = f.keeper.AvailPoolBalance(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(19), got)
}
