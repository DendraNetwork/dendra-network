package keeper_test

import (
	"github.com/stretchr/testify/require"
	"testing"
)

func TestSeedPruningRemovesOnlySelectedHeight(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.DeleteBlockHash(f.ctx, 0))
	require.NoError(t, f.keeper.DeleteDecentralizedSeed(f.ctx, 0))
	require.NoError(t, f.keeper.DeleteDecentralizedSeedContributors(f.ctx, 0))
	for _, h := range []int64{0, 1} {
		require.NoError(t, f.keeper.SetBlockHash(f.ctx, h, []byte{byte(h + 1)}))
		require.NoError(t, f.keeper.SetDecentralizedSeed(f.ctx, h, []byte{byte(h + 2)}))
		require.NoError(t, f.keeper.SetDecentralizedSeedContributors(f.ctx, h, uint64(h+1)))
	}
	require.NoError(t, f.keeper.DeleteBlockHash(f.ctx, 0))
	require.NoError(t, f.keeper.DeleteDecentralizedSeed(f.ctx, 0))
	require.NoError(t, f.keeper.DeleteDecentralizedSeedContributors(f.ctx, 0))
	_, ok := f.keeper.GetBlockHash(f.ctx, 0)
	require.False(t, ok)
	_, ok = f.keeper.GetDecentralizedSeed(f.ctx, 0)
	require.False(t, ok)
	_, ok = f.keeper.GetDecentralizedSeedContributors(f.ctx, 0)
	require.False(t, ok)
	v, ok := f.keeper.GetBlockHash(f.ctx, 1)
	require.True(t, ok)
	require.Equal(t, []byte{2}, v)
	v, ok = f.keeper.GetDecentralizedSeed(f.ctx, 1)
	require.True(t, ok)
	require.Equal(t, []byte{3}, v)
	n, ok := f.keeper.GetDecentralizedSeedContributors(f.ctx, 1)
	require.True(t, ok)
	require.Equal(t, uint64(2), n)
}
