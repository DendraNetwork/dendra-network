package keeper_test

import (
	"strconv"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// Le BEACON est géré par le PROTOCOLE (créé par open-job, immuable, anti-grinding H6).
// Create/Update/Delete manuels sont donc REFUSÉS par conception. Ces tests verrouillent ce refus
// (les anciens tests scaffold attendaient un succès -> obsolètes vs handlers customisés).

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
