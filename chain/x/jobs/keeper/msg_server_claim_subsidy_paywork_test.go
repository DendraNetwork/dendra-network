package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ClaimSubsidy REALLY pays the operator from the emission WorkPool, rather than incrementing a
// counter. The WorkPool is debited by exactly the amount paid out.
func TestClaimSubsidyPaysFromWorkPool(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	startPool := uint64(1_000_000)
	f.emission.pool = startPool

	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.NoError(t, err)

	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	paid := m.SubsidyClaimed
	require.Greater(t, paid, uint64(0))               // something was ACTUALLY paid
	require.Equal(t, paid, f.emission.paid[operator]) // paid in real coins to the operator
	require.Equal(t, startPool-paid, f.emission.pool) // WorkPool debited by the same amount
}

// Insufficient pool -> PARTIAL payment; SubsidyClaimed only advances by what was paid, so the rest
// stays claimable.
func TestClaimSubsidyPartialWhenPoolShort(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	f.emission.pool = 50 // far below the cap -> partial payment
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.NoError(t, err)
	require.Equal(t, uint64(50), f.emission.paid[operator]) // pays only what the pool covers
	require.Equal(t, uint64(0), f.emission.pool)            // pool emptied

	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	require.Equal(t, uint64(50), m.SubsidyClaimed) // advances only by the amount paid
}

// Empty pool -> explicit rejection (nothing to pay out).
func TestClaimSubsidyEmptyPoolRejects(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	f.emission.pool = 0
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: operator, MinerId: "m0"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}
