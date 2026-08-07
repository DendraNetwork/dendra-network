package keeper_test

import (
	"strconv"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// CreateCommit binds a commit to its signer: it requires a "<jobId>__<minerId>" key AND that the
// signer be the miner's OPERATOR, and it REFUSES to overwrite an existing commit.
func TestCommitMsgServerCreate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	idM0 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idM0, Operator: creator, Stake: 1000})
	require.NoError(t, err)

	for i := 0; i < 5; i++ {
		expected := &types.MsgCreateCommit{Creator: creator, JobId: strconv.Itoa(i) + "__" + idM0}
		_, err := srv.CreateCommit(f.ctx, expected)
		require.NoError(t, err)
		rst, err := f.keeper.Commit.Get(f.ctx, expected.JobId)
		require.NoError(t, err)
		require.Equal(t, expected.Creator, rst.Creator)
	}
}

// An anchored commit is IMMUTABLE, which is what makes the signature binding worth anything.
// UpdateCommit and DeleteCommit must ALWAYS reject, EVEN for the legitimate creator: otherwise it
// watches the other miners' public commits, then rewrites its own to escape a slash, or deletes it
// to escape the tally.
func TestCommitImmutableGO32(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	idM0 := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: idM0, Operator: creator, Stake: 1000})
	require.NoError(t, err)
	key := "0__" + idM0
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: creator, JobId: key, ResultCommit: "VRAI"})
	require.NoError(t, err)

	_, err = srv.UpdateCommit(f.ctx, &types.MsgUpdateCommit{Creator: creator, JobId: key, ResultCommit: "TRICHE"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.DeleteCommit(f.ctx, &types.MsgDeleteCommit{Creator: creator, JobId: key})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// The original commit is INTACT after both attempts: immutability is effective, not just declared.
	rst, err := f.keeper.Commit.Get(f.ctx, key)
	require.NoError(t, err)
	require.Equal(t, "VRAI", rst.ResultCommit)
}
