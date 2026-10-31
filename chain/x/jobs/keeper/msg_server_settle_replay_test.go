package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A job settled by one path (marker `+settled` or `+paid`) must be refused by every other settlement
// path. This closes double payment (settle_pay then payout). The raw settlement that once sat beside
// them (SettleJob) was removed at epoch 15, so the pair below is the whole set.
func TestSettlementAntiReplayUnifiedGO08(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	// (1) Job already settled through settle_pay, state "open+settled". A payout that only looks for "paid"
	// would pay again; it must refuse.
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", Fee: 10000, Client: creator, State: "open+settled"}))
	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: creator, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (2) Symmetric case: job "open+paid" (through payout/settle_semantic), so settle_pay refuses.
	require.NoError(t, gvSetJob(f, f.ctx, "j2", types.Job{JobId: "j2", Fee: 10000, Client: creator, State: "open+paid"}))
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: creator, JobId: "j2", Amount: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}
