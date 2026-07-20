package keeper

import (
	"context"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"dendra/x/emission/types"
)

// PayAvail verse `amt` udndr depuis le compte de module emission vers `recipient`, en débitant
// l'AvailPool d'autant. C'est le robinet du flux DISPONIBILITÉ (jusque-là accumulé chaque époque mais
// JAMAIS versé) : côté x/jobs, à chaque époque de disponibilité, les mineurs PROUVÉS présents (défi
// anti pré-calcul) sont payés EN VRAIS COINS depuis ce pool, pondérés par leur bond. Jumeau de PayWork.
//
// INVARIANT préservé : solde(module) et AvailPool baissent du même montant
// -> `solde(emission) == Reserve + WorkPool + AvailPool` reste vrai.
func (k Keeper) PayAvail(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt == 0 {
		return 0, nil
	}
	pool, err := k.AvailPool.Get(ctx)
	if err != nil {
		pool = 0
	}
	if amt > pool {
		amt = pool // jamais plus que le flux disponibilité accumulé
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
	if err := k.AvailPool.Set(ctx, pool-amt); err != nil {
		return 0, err
	}
	return amt, nil
}

// AvailPoolBalance renvoie le solde courant de l'AvailPool (0 si non initialisé). Permet à x/jobs de
// dimensionner le budget d'une époque de disponibilité (fraction de l'AvailPool) avant de répartir.
func (k Keeper) AvailPoolBalance(ctx context.Context) (uint64, error) {
	v, err := k.AvailPool.Get(ctx)
	if err != nil {
		return 0, nil
	}
	return v, nil
}
