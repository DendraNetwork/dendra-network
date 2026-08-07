package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A job settled by ONE path (marker `+settled` OR `+paid`) must be REFUSED by EVERY other settlement
// path. This closes double payment (settle_pay then payout) and Demand inflation (a non-exclusive
// settle_job).
func TestSettlementAntiReplayUnifiedGO08(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	// (1) job already settled through settle_pay -> state "open+settled". A payout that only looks for
	// "paid" would pay again; it must REFUSE.
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", Fee: 10000, Client: creator, State: "open+settled"}))
	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: creator, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: creator, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // no Demand inflation

	// (2) symmetric case: job "open+paid" (through payout/settle_semantic) -> both settle_pay and settle_job refuse.
	require.NoError(t, gvSetJob(f, f.ctx, "j2", types.Job{JobId: "j2", Fee: 10000, Client: creator, State: "open+paid"}))
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: creator, JobId: "j2", Amount: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: creator, JobId: "j2"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}
