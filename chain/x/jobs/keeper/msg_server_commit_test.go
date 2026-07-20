package keeper_test

import (
	"strconv"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// CreateCommit (H1, liaison de signature) exige une clé "<jobId>__<minerId>" ET que le signataire soit
// l'OPÉRATEUR du mineur ; il REFUSE l'écrasement. (Setup : un mineur dont l'opérateur = creator.)
func TestCommitMsgServerCreate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "m0", Operator: creator, Stake: 1000})
	require.NoError(t, err)

	for i := 0; i < 5; i++ {
		expected := &types.MsgCreateCommit{Creator: creator, JobId: strconv.Itoa(i) + "__m0"}
		_, err := srv.CreateCommit(f.ctx, expected)
		require.NoError(t, err)
		rst, err := f.keeper.Commit.Get(f.ctx, expected.JobId)
		require.NoError(t, err)
		require.Equal(t, expected.Creator, rst.Creator)
	}
}

// NEW-GO-32 (audit v2) — un commit ancré est IMMUABLE (fondement de H1). UpdateCommit/DeleteCommit
// doivent TOUJOURS rejeter, MÊME par le créateur légitime : sinon il observe les commits publics des
// autres puis réécrit le sien (évasion du slash) ou le supprime (évasion du tally).
func TestCommitImmutableGO32(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "m0", Operator: creator, Stake: 1000})
	require.NoError(t, err)
	key := "0__m0"
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: creator, JobId: key, ResultCommit: "VRAI"})
	require.NoError(t, err)

	_, err = srv.UpdateCommit(f.ctx, &types.MsgUpdateCommit{Creator: creator, JobId: key, ResultCommit: "TRICHE"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.DeleteCommit(f.ctx, &types.MsgDeleteCommit{Creator: creator, JobId: key})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// le commit d'origine est INTACT après les deux tentatives (immuabilité effective).
	rst, err := f.keeper.Commit.Get(f.ctx, key)
	require.NoError(t, err)
	require.Equal(t, "VRAI", rst.ResultCommit)
}
