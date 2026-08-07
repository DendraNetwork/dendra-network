package keeper

import (
	"context"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}

	// RESUME vs BOOTSTRAP — the distinction this file has to make.
	//
	// Resetting the Reserve to `GenesisReserveU` and the epoch to 0 on EVERY InitGenesis, without ever
	// looking at what genesis contains, resurrects an already-spent Reserve on any export/import — the
	// ordinary gesture of a chain migration. It also erases the unclaimed pools (subsidies owed to
	// miners vanish while the coins backing them are re-counted as Reserve) and fires an epoch at the
	// first block.
	//
	// `state` is an OPTIONAL message, and that is what makes the distinction possible: proto3 does not
	// separate "absent" from "zero" on a scalar, whereas an exhausted Reserve is legitimately zero.
	// Absent = new chain, bootstrap. Present = resume, restore verbatim, zeros included.
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

	// ADR-023: bootstrap of a NEW chain (Reserve = 3.3 M DNDR, epoch at 0).
	if err := k.Reserve.Set(ctx, GenesisReserveU); err != nil {
		return err
	}

	// THE THREE POCKETS ARE EXPLICITLY SET TO ZERO, AND THAT IS NOT COSMETIC — it is what makes the
	// export/import round-trip an IDENTITY.
	//
	// Without these three lines a new chain has NO key at all for `workpool` / `availpool` /
	// `securitypool`, because nothing writes them before the first epoch. `ExportGenesis` must
	// nonetheless carry them: proto3 does not distinguish "absent" from "zero" on a scalar, so the
	// export writes three zeros and the import SETS them. The re-imported chain then holds three keys
	// the original did not: semantically identical, byte-for-byte different, so a DIFFERENT APP-HASH.
	// And these three pockets HOLD MONEY (`WorkPool`/`AvailPool` are claimable), so papering over the
	// difference with an exception would make that test blind to a REAL divergence of the pools.
	//
	// Two remedies existed; only one is an identity. "Do not write a zero on import" looks cheaper —
	// but a pocket that was GENUINELY EMPTIED is legitimately 0 (`pay_work.go` writes `pool-amt`), so
	// it would disappear on re-import: the same artifact, inverted, and rarer, therefore harder to see.
	// Making PRESENCE uniform from bootstrap removes the case instead of moving it.
	//
	// WHAT IT COSTS, stated: three more keys in the genesis state, so THE GENESIS APP-HASH CHANGES.
	// That is a consensus change; it requires a chain reset, and it ships alongside the reset that is
	// already planned. It is done now because no live chain carries this genesis (`DefaultGenesis()`
	// has no state and `config.yml` only fixes the emission parameters): deferring it would save
	// nothing and would leave the round-trip test blind.
	if err := k.WorkPool.Set(ctx, 0); err != nil {
		return err
	}
	if err := k.AvailPool.Set(ctx, 0); err != nil {
		return err
	}
	if err := k.SecurityPool.Set(ctx, 0); err != nil {
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

	// An absent Item is 0: that is the state of a chain that has not run yet, and carrying it as 0 is
	// exact. Absence is therefore not an error here — it is transported as is.
	get := func(it interface {
		Get(context.Context) (uint64, error)
	}) uint64 {
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
