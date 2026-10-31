package keeper_test

import (
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
)

func TestModelQueryRejectsNilAndUnknownAndReturnsKnown(t *testing.T) {
	f := initFixture(t)
	q := keeper.NewQueryServerImpl(f.keeper)
	_, err := q.Model(f.ctx, nil)
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	_, err = q.Model(f.ctx, &types.QueryModelRequest{})
	require.Equal(t, codes.NotFound, status.Code(err))
	model := types.Model{Id: "m", Active: true}
	require.NoError(t, f.keeper.Models.Set(f.ctx, model.Id, model))
	got, err := q.Model(f.ctx, &types.QueryModelRequest{Id: model.Id})
	require.NoError(t, err)
	require.Equal(t, model, got.Model)
}

func TestModelsQueryRejectsNilAndListsOnlyStoredModels(t *testing.T) {
	f := initFixture(t)
	q := keeper.NewQueryServerImpl(f.keeper)
	_, err := q.Models(f.ctx, nil)
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	got, err := q.Models(f.ctx, &types.QueryModelsRequest{})
	require.NoError(t, err)
	require.Empty(t, got.Models)
	model := types.Model{Id: "m", Active: false}
	require.NoError(t, f.keeper.Models.Set(f.ctx, model.Id, model))
	got, err = q.Models(f.ctx, &types.QueryModelsRequest{})
	require.NoError(t, err)
	require.Equal(t, []types.Model{model}, got.Models)
}
