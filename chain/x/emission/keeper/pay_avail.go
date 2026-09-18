package keeper

import (
	"context"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// PayAvail sends `amt` udndr from the emission module account to `recipient`, debiting the AvailPool
// by the same amount. It is the tap of the AVAILABILITY flow, which is otherwise accrued every epoch
// and never paid out: in x/jobs, at each availability epoch, miners PROVEN present (through the
// anti-precomputation challenge) are paid in REAL COINS from this pool, weighted by their bond. Twin
// of PayWork.
//
// INVARIANT preserved: the module balance and the AvailPool decrease by the same amount, so
// `balance(emission) == Reserve + WorkPool + AvailPool` remains true.
func (k Keeper) PayAvail(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt == 0 {
		return 0, nil
	}
	pool, err := k.AvailPool.Get(ctx)
	if err != nil {
		pool = 0
	}
	if amt > pool {
		amt = pool // never more than the accrued availability flow
	}
	if amt == 0 {
		return 0, nil
	}
	// defensive bound: the actually spendable balance of the emission module account
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
	if err := k.AvailPool.Set(ctx, pool-amt); err != nil {
		return 0, err
	}
	return amt, nil
}

// AvailPoolBalance returns the current AvailPool balance (0 when uninitialized). It lets x/jobs size
// the budget of an availability epoch — a fraction of the AvailPool — before distributing it.
func (k Keeper) AvailPoolBalance(ctx context.Context) (uint64, error) {
	v, err := k.AvailPool.Get(ctx)
	if err != nil {
		return 0, nil
	}
	return v, nil
}
