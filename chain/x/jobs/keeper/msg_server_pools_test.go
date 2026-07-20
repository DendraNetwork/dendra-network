package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// NEW-GO-34 (audit v2) — le CRUD scaffold Pools est NEUTRALISÉ : la comptabilité `Pools` est gérée par
// le règlement (création paresseuse) / le genesis. Create/Update/DeletePools doivent TOUJOURS rejeter ;
// sinon le premier appelant possède/falsifie la compta et peut DoS le règlement.
func TestPoolsCRUDNeutralizedGO34(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	_, err = srv.CreatePools(f.ctx, &types.MsgCreatePools{Creator: creator, Treasury: 10_000_000})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.UpdatePools(f.ctx, &types.MsgUpdatePools{Creator: creator, Treasury: 10_000_000})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	_, err = srv.DeletePools(f.ctx, &types.MsgDeletePools{Creator: creator})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// aucun singleton Pools n'a pu être posé par un appelant arbitraire.
	has, err := f.keeper.Pools.Has(f.ctx)
	require.NoError(t, err)
	require.False(t, has)
}
