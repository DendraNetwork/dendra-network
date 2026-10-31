package keeper

import (
	"context"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// PayWork transfers `amt` udndr from the emission module account to `recipient` and debits the
// WorkPool by the same amount. It is the payout of the work stream, which is credited and withheld
// until claimed: on the x/jobs side a miner claims its subsidy (gated by demand) and is paid
// in real coins from this pool. It returns the amount actually paid (less than `amt` when the pool or
// the module balance is short).
//
// Invariant: the module balance and the WorkPool decrease by the same amount, so
// `balance(emission) == Reserve + WorkPool + AvailPool` still holds.
func (k Keeper) PayWork(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt == 0 {
		return 0, nil
	}
	pool, err := k.WorkPool.Get(ctx)
	if err != nil {
		pool = 0
	}
	if amt > pool {
		amt = pool // never more than the accumulated work stream
	}
	if amt == 0 {
		return 0, nil
	}
	// Defensive bound: the actually spendable balance of the emission module account.
	modAddr := authtypes.NewModuleAddress(types.ModuleName)
	spendable := k.bankKeeper.SpendableCoins(ctx, modAddr).AmountOf("udndr")
	if amtInt := math.NewIntFromUint64(amt); spendable.LT(amtInt) {
		amt = spendable.Uint64()
	}
	if amt == 0 {
		return 0, nil
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(amt)))
	if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, recipient, coins); err != nil {
		return 0, err
	}
	if err := k.WorkPool.Set(ctx, pool-amt); err != nil {
		return 0, err
	}
	// The cumulative payout, for the Pools query: written after the pool, so the
	// two move together and the total never counts a payment the pool did not release.
	if err := addItem(ctx, k.WorkPaidTotal, amt); err != nil {
		return 0, err
	}
	return amt, nil
}
