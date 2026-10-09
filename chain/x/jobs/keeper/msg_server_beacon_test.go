package keeper_test

import (
	"strconv"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The BEACON is managed by the PROTOCOL (created by open-job, immutable, anti-grinding H6).
// Manual create/update/delete are therefore REFUSED by design. These cases pin that refusal; the
// scaffold tests expected success and no longer describe the custom handlers.

func TestBeaconMsgServerCreateRejected(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	_, err = srv.CreateBeacon(f.ctx, &types.MsgCreateBeacon{Creator: creator, JobId: strconv.Itoa(0)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
}

func TestBeaconMsgServerUpdateRejected(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	_, err = srv.UpdateBeacon(f.ctx, &types.MsgUpdateBeacon{Creator: creator, JobId: strconv.Itoa(0)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
}

func TestBeaconMsgServerDeleteRejected(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	_, err = srv.DeleteBeacon(f.ctx, &types.MsgDeleteBeacon{Creator: creator, JobId: strconv.Itoa(0)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
}
