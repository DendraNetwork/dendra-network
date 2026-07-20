package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// NEW-GO-33 (audit v2) — FinalizeJob doit REFUSER un job inexistant AVANT tout slash : sinon un
// attaquant détruit le bond RÉEL (GO-13) d'un mineur honnête sur un job FANTÔME au prix d'une tx.
func TestFinalizeJobRequiresExistingJobGO33(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: creator, JobId: "ghost"})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
}
