package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The scaffolded Job CRUD is NEUTRALISED: a job is only ever created through `OpenJob`, with a real
// escrow. CreateJob/UpdateJob/DeleteJob must ALWAYS reject; otherwise a cross-job drain reopens and
// the anti-replay state can be reset.
func TestJobCRUDNeutralizedGO30(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.CreateJob(f.ctx, &types.MsgCreateJob{Creator: creator, JobId: "j0", Fee: 1_000_000, State: "open"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.UpdateJob(f.ctx, &types.MsgUpdateJob{Creator: creator, JobId: "j0", State: "open"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.DeleteJob(f.ctx, &types.MsgDeleteJob{Creator: creator, JobId: "j0"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// the drain through CreateJob is closed: no non-escrowed job could be created.
	has, err := f.keeper.Job.Has(f.ctx, "j0")
	require.NoError(t, err)
	require.False(t, has)
}
