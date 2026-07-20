package keeper

import (
	"context"

	"dendra/x/emission/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}

	// REPRISE vs AMORÇAGE — la distinction que ce fichier ne faisait pas.
	//
	// Avant, la Réserve était remise à `GenesisReserveU` et l'époque à 0 À CHAQUE InitGenesis, sans
	// jamais regarder ce que le genesis contenait. Un export/import — le geste normal d'une migration
	// de chaîne — ressuscitait donc une Réserve déjà dépensée, effaçait les pools non réclamés (les
	// subventions dues aux mineurs disparaissaient, pendant que les coins qui les adossaient étaient
	// re-comptés comme Réserve) et faisait tomber une époque au premier bloc.
	//
	// `state` est un message OPTIONNEL, et c'est ce qui rend la distinction possible : proto3 ne
	// sépare pas « absent » de « zéro » sur un scalaire, alors qu'une Réserve épuisée vaut légitimement
	// zéro. Absent = chaîne neuve, on amorce. Présent = reprise, on restitue tel quel, zéros compris.
	if s := genState.State; s != nil {
		if err := k.Reserve.Set(ctx, s.Reserve); err != nil {
			return err
		}
		if err := k.WorkPool.Set(ctx, s.WorkPool); err != nil {
			return err
		}
		if err := k.AvailPool.Set(ctx, s.AvailPool); err != nil {
			return err
		}
		if err := k.SecurityPool.Set(ctx, s.SecurityPool); err != nil {
			return err
		}
		if err := k.LastEpoch.Set(ctx, s.LastEpoch); err != nil {
			return err
		}
		return k.LastSupply.Set(ctx, s.LastSupply)
	}

	// TK-02 / ADR-023 : amorçage d'une chaîne NEUVE (Réserve = 3,3 M DNDR, époque à 0).
	if err := k.Reserve.Set(ctx, GenesisReserveU); err != nil {
		return err
	}
	if err := k.LastEpoch.Set(ctx, 0); err != nil {
		return err
	}
	return k.LastSupply.Set(ctx, 0)
}

// ExportGenesis returns the module's exported genesis.
func (k Keeper) ExportGenesis(ctx context.Context) (*types.GenesisState, error) {
	var err error

	genesis := types.DefaultGenesis()
	genesis.Params, err = k.Params.Get(ctx)
	if err != nil {
		return nil, err
	}

	// Un Item absent vaut 0 : c'est l'état d'une chaîne qui n'a pas encore tourné, et le transporter
	// à 0 est exact. On n'échoue donc pas sur l'absence — on la transporte telle quelle.
	get := func(it interface{ Get(context.Context) (uint64, error) }) uint64 {
		v, e := it.Get(ctx)
		if e != nil {
			return 0
		}
		return v
	}
	genesis.State = &types.EmissionState{
		Reserve:      get(k.Reserve),
		WorkPool:     get(k.WorkPool),
		AvailPool:    get(k.AvailPool),
		SecurityPool: get(k.SecurityPool),
		LastEpoch:    get(k.LastEpoch),
		LastSupply:   get(k.LastSupply),
	}

	return genesis, nil
}
