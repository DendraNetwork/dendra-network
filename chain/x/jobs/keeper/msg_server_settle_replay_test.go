package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// TestSettlementAntiReplayUnifiedGO08 — GO-08 / NEW-GO-35 : un job réglé par UN chemin (marqueur
// `+settled` OU `+paid`) doit être REFUSÉ par TOUS les autres chemins de règlement → ferme le
// double-paiement (settle_pay puis payout) ET l'inflation de Demand (settle_job non exclusif).
func TestSettlementAntiReplayUnifiedGO08(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	// (1) job déjà réglé via settle_pay → state "open+settled" : AVANT, payout ne voyait que "paid"
	// et repayait. Désormais il REFUSE.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", Fee: 10000, Client: creator, State: "open+settled"}))
	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: creator, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: creator, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // pas d'inflation de Demand

	// (2) symétrique : job "open+paid" (via payout/settle_semantic) → settle_pay ET settle_job refusent.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j2", types.Job{JobId: "j2", Fee: 10000, Client: creator, State: "open+paid"}))
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: creator, JobId: "j2", Amount: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: creator, JobId: "j2"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}
