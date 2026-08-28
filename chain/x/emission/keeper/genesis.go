package keeper

import (
	"context"
	"fmt"
	"os"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	if err := k.Params.Set(ctx, genState.Params); err != nil {
		return err
	}
	// THE COUNTER IS NOT THE MONEY, AND THIS IS THE ONLY MOMENT THAT CAN STILL SAY SO.
	//
	// `Reserve` is a COUNTER. The coins it counts live in the module account, and nothing in this file
	// puts them there: a genesis is free to announce a Reserve of 3.3 M DNDR while crediting an
	// ordinary account instead. One wrong address in a genesis builder is enough, and every balance
	// query afterwards looks right, because both numbers exist.
	//
	// The chain then starts, syncs and stays healthy for as long as the first epoch is away. At that
	// height `RunEpoch` moves the security tranche out of an empty account, the transfer fails, and the
	// epoch DEFERS -- then defers at every epoch after it. The reward budget is unreachable for good:
	// `WorkPool` and `AvailPool` stay at zero, every `MsgClaimSubsidy` answers "work pool is empty",
	// and no validator is ever paid. Nothing reports it before that block, because the deferral is
	// deliberately quiet: a transient bank failure must not halt the chain.
	//
	// So the confrontation belongs here, at the one moment where refusing costs a configuration file
	// instead of a chain reset. `bank` initialises before this module (app_config.go), so the balance
	// is already readable, and `x/mint` is de-registered: there is no later source for these coins, so
	// a Reserve the module cannot cover is a misconfiguration and never a schedule.
	//
	// It compares the COUNTER to the BALANCE, not one declaration to another. `config.yml` and the
	// genesis builder both declare the module account funded; a gate that read the intent would have
	// passed while the deployed artifact diverged from it.
	// REFUSING TO CREATE IS PROTECTIVE; REFUSING TO JOIN IS SELF-HARM -- MEASURED, NOT SUPPOSED.
	// This guard shipped on 2026-08-15 and made the LIVE network unjoinable the same day. The genesis
	// served at api.dendranetwork.com (sha256 9dc9e320..., the digest network-info.txt announces) puts
	// the 3.3e12 on an ORDINARY account, so a node built from the PUBLISHED source panics at InitChain
	// with EXIT=2 -- while the running chain keeps going, because it never replays InitGenesis. The
	// chain exists whether or not a joining node refuses it; there, the refusal repairs nothing and
	// keeps miners away from a network that needs five of them. Creating and joining are the SAME code
	// path over the SAME file, and no field distinguishes them, so the OPERATOR says which one it is --
	// once, explicitly, and never by default.
	//
	// THIS CANNOT FORK ANYTHING, which is the only reason it may be a switch at all: the guard READS.
	// It writes no state and changes no value, so a node that skips it computes exactly the state a
	// node that never had it computes. The variable decides whether THIS node starts, never what the
	// chain agrees on. An environment variable rather than a flag because InitGenesis sees no command
	// line.
	if err := k.reserveIsBacked(ctx, genState); err != nil {
		if os.Getenv(allowUnbackedReserveEnv) != "1" {
			return err
		}
		// Said ONCE, at ERROR level, because nothing downstream repeats it: the deferral events only
		// begin at the first epoch, which is days away at epoch_blocks=86400.
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"EMISSION RESERVE UNBACKED -- GENESIS LOADED ANYWAY because "+allowUnbackedReserveEnv+"=1. This chain "+
				"CANNOT pay rewards: every epoch defers with security_transfer_failed until the module account is "+
				"funded or the network restarts from a corrected genesis. Joining is legitimate; launching is not.",
			"detail", err.Error())
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

// reserveIsBacked refuses a genesis whose Reserve counter exceeds the coins the module account holds.
//
// WHICH RESERVE IS CHECKED. On a RESUME it is the value the export carried; on a BOOTSTRAP it is
// `GenesisReserveU`, which the branch above is about to write. Both are claims about money that must
// already be there, so both are checked.
//
// WHY `SpendableCoins` AND NOT A RAW BALANCE. The pools pay out of spendable coins (`pay_work.go`,
// `pay_avail.go`) and `RunEpoch` moves spendable coins, so spendable is the quantity that decides
// whether the module can honour its counter. A vested or locked balance would satisfy a naive balance
// check and still defer every epoch -- the exact failure this refusal exists to make impossible.
//
// A ZERO RESERVE NEEDS NO BACKING: an exhausted Reserve is a legitimate end state, and an export
// carrying it must re-import.
//
// IT ONLY EVER RETURNS AN ERROR -- it writes nothing. That is what lets the caller downgrade it to a
// logged warning for an operator joining an already-unbacked network without any risk of divergence.

// allowUnbackedReserveEnv -- the ONE way past the refusal, set to "1" by an operator who knows the
// network they are joining is already unbacked. Absent, or any other value, and the refusal stands.
// Compose forwards ONLY what it names, so this has to be listed in deploy/testnet-node/docker-compose.yml
// to be reachable inside the container at all.
const allowUnbackedReserveEnv = "DENDRA_ALLOW_UNBACKED_RESERVE"

// THE COUNTER TO BACK IS THE WHOLE IDENTITY, NOT ONE THIRD OF IT.
//
// `RunEpoch` states the invariant this module lives by: `balance(emission module) == Reserve +
// WorkPool + AvailPool`, because SECURITY is the only flow that leaves the module -- work and
// availability STAY there, as counters awaiting their own payout path (`pay_work.go`,
// `pay_avail.go`, both of which read `SpendableCoins` of that same address before sending).
// Checking `Reserve` alone therefore accepts a genesis that declares pools nothing backs: the chain
// boots, looks healthy, and the shortfall only surfaces at the first work or availability payment --
// which is exactly the "counter is not the money" failure this guard exists to refuse, one level
// down. `SecurityPool` is deliberately NOT summed: those coins are sent out to `fee_collector`, so
// requiring the module to still hold them would refuse a correct genesis.
//
// The sum is computed in a width that cannot wrap: three uint64 counters can overflow uint64, and an
// overflow here would UNDERSTATE what must be backed -- the reassuring direction, on a criterion that
// decides whether the chain may start at all.
func (k Keeper) reserveIsBacked(ctx context.Context, genState types.GenesisState) error {
	resv, work, avail := GenesisReserveU, uint64(0), uint64(0)
	if s := genState.State; s != nil {
		resv, work, avail = s.Reserve, s.WorkPool, s.AvailPool
	}
	declaredBig := math.NewIntFromUint64(resv).
		Add(math.NewIntFromUint64(work)).
		Add(math.NewIntFromUint64(avail))
	if declaredBig.IsZero() {
		return nil
	}
	declared := declaredBig.Uint64()
	modAddr := authtypes.NewModuleAddress(types.ModuleName) // same derivation RunEpoch uses
	held := k.bankKeeper.SpendableCoins(ctx, modAddr).AmountOf("udndr")
	if held.LT(declaredBig) {
		return fmt.Errorf(
			"emission: the counters this module must hold (Reserve %d + WorkPool %d + AvailPool %d = %d udndr) are "+
				"NOT backed by the module account %s, which holds %s udndr. The counter is not the money: these coins "+
				"must be credited to the MODULE address in genesis, not to an ordinary account. Only SecurityPool "+
				"leaves the module, so it is excluded from this sum. Started unchecked, the chain looks healthy until "+
				"the first epoch and then defers every one of them (security_transfer_failed) -- reward budget "+
				"unreachable, work and availability payments short of coins, and nothing saying so before that "+
				"height. If you are JOINING a network already in this state rather than launching one, set "+
				allowUnbackedReserveEnv+"=1 and this becomes a logged warning instead of a refusal",
			resv, work, avail, declared, modAddr.String(), held.String())
	}
	return nil
}
