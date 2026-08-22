package keeper_test

import (
	"testing"

	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
)

// PayWork pays the WORK flow (WorkPool) in real coins, bounded by the pool AND by the module's
// balance, while preserving the invariant that the pool drops by exactly the amount paid.
func TestPayWorkFromPool(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 1000))
	f.bank.spendable = 5000 // the module account has ample cover

	recip := authtypes.NewModuleAddress("recip-test")

	// (a) 1500 is requested but the pool only holds 1000 -> 1000 paid (capped by the pool)
	paid, err := f.keeper.PayWork(f.ctx, recip, 1500)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), paid)

	wp, err := f.keeper.WorkPool.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), wp) // WorkPool debited by the same amount

	// the coins were ACTUALLY sent to the recipient
	require.Equal(t, int64(1000), f.bank.paid[recip.String()].AmountOf("udndr").Int64())

	// (b) empty pool -> nothing left to pay, and no error
	paid2, err := f.keeper.PayWork(f.ctx, recip, 100)
	require.NoError(t, err)
	require.Equal(t, uint64(0), paid2)
}

// The payout is capped by the module's spendable balance (a defensive bound), even when the WorkPool
// counter is higher than what the account actually holds.
func TestPayWorkBoundedBySpendable(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 1000))
	f.bank.spendable = 300 // the module only holds 300 spendable

	recip := authtypes.NewModuleAddress("recip-test")
	paid, err := f.keeper.PayWork(f.ctx, recip, 1000)
	require.NoError(t, err)
	require.Equal(t, uint64(300), paid) // bounded by the real balance

	wp, err := f.keeper.WorkPool.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(700), wp) // the pool is debited only by what was actually paid
}
