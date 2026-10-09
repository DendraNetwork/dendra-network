package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// FinalizeJob must REFUSE a non-existent job BEFORE any slash: otherwise an attacker destroys the REAL
// bond of an honest miner on a PHANTOM job for the price of one transaction.
func TestFinalizeJobRequiresExistingJobGO33(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: creator, JobId: "ghost"})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
}
