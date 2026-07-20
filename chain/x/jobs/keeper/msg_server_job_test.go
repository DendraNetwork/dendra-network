package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// NEW-GO-30/31 (audit v2) — le CRUD scaffold Job est NEUTRALISÉ : un job ne se crée que via `OpenJob`
// (escrow réel). CreateJob/UpdateJob/DeleteJob doivent TOUJOURS rejeter ; sinon drain inter-jobs
// (GO-02 ré-ouvert) + reset de l'anti-rejeu.
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

	// le drain GO-02 via CreateJob est fermé : aucun job non-escrowé n'a pu être créé.
	has, err := f.keeper.Job.Has(f.ctx, "j0")
	require.NoError(t, err)
	require.False(t, has)
}
