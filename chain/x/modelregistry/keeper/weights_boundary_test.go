package keeper_test

import (
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestExpectedWeightsRequiresAnActiveModel(t *testing.T) {
	f := initFixture(t)
	got, ok := f.keeper.ExpectedWeights(f.ctx, "")
	require.False(t, ok)
	require.Empty(t, got)
	require.NoError(t, f.keeper.Models.Set(f.ctx, "off", types.Model{Id: "off", WeightsSha256: "anchor", Active: false}))
	got, ok = f.keeper.ExpectedWeights(f.ctx, "off")
	require.False(t, ok)
	require.Empty(t, got)
	require.NoError(t, f.keeper.Models.Set(f.ctx, "on", types.Model{Id: "on", WeightsSha256: "anchor", Active: true}))
	got, ok = f.keeper.ExpectedWeights(f.ctx, "on")
	require.True(t, ok)
	require.Equal(t, "anchor", got)
	require.NoError(t, f.keeper.Models.Set(f.ctx, "empty", types.Model{Id: "empty", Active: true}))
	got, ok = f.keeper.ExpectedWeights(f.ctx, "empty")
	require.True(t, ok)
	require.Empty(t, got)
}

func TestActiveWithoutAnchorFiltersAndSorts(t *testing.T) {
	f := initFixture(t)
	ids, err := f.keeper.ActiveWithoutAnchor(f.ctx)
	require.NoError(t, err)
	require.Empty(t, ids)
	for _, m := range []types.Model{
		{Id: "z", Active: true}, {Id: "a", Active: true, WeightsSha256: "  "},
		{Id: "off", Active: false}, {Id: "anchored", Active: true, WeightsSha256: "hash"},
	} {
		require.NoError(t, f.keeper.Models.Set(f.ctx, m.Id, m))
	}
	ids, err = f.keeper.ActiveWithoutAnchor(f.ctx)
	require.NoError(t, err)
	require.Equal(t, []string{"a", "z"}, ids)
}
