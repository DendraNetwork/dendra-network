package keeper

import (
	"context"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"dendra/x/emission/types"
)

// PayWork verse `amt` udndr depuis le compte de module emission vers `recipient`, en débitant le
// WorkPool d'autant. C'est le mécanisme de VERSEMENT du flux TRAVAIL (jusque-là crédité mais retenu) :
// côté x/jobs un mineur réclame sa subvention (gatée par la demande, ADR-017) et est payé EN VRAIS
// COINS depuis ce pool. Renvoie le montant RÉELLEMENT versé (< amt si pool/solde insuffisants).
//
// INVARIANT préservé : solde(module) et WorkPool baissent du même montant
// -> `solde(emission) == Reserve + WorkPool + AvailPool` reste vrai.
func (k Keeper) PayWork(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt == 0 {
		return 0, nil
	}
	pool, err := k.WorkPool.Get(ctx)
	if err != nil {
		pool = 0
	}
	if amt > pool {
		amt = pool // jamais plus que le flux travail accumulé
	}
	if amt == 0 {
		return 0, nil
	}
	// borne défensive par le solde réellement dépensable du compte de module emission
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
	return amt, nil
}
