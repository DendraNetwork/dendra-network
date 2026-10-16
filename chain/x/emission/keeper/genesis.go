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
	// The counter is not the money, and genesis is the last moment that can still say so.
	//
	// `Reserve` is a counter. The coins it counts live in the module account, and nothing in this file
	// puts them there: a genesis can announce a Reserve of 3.3 M DNDR while crediting an ordinary account
	// instead. One wrong address in a genesis builder is enough, and every balance query afterwards looks
	// right, because both numbers exist.
	//
	// The chain then starts and stays healthy until the first epoch. At that height `RunEpoch` moves the
	// security tranche out of an empty account, the transfer fails, and the epoch is deferred, then
	// deferred again at every epoch after it. The reward budget becomes unreachable: `WorkPool` and
	// `AvailPool` stay at zero, every `MsgClaimSubsidy` answers "work pool is empty", and no validator is
	// paid. Nothing reports it before that block, because the deferral is quiet on purpose: a transient
	// bank failure must not halt the chain.
	//
	// So the check belongs here, where refusing costs a configuration file instead of a chain reset.
	// `bank` initialises before this module (app_config.go), so the balance is already readable, and
	// `x/mint` is not registered: there is no later source for these coins, so a Reserve the module
	// cannot cover is a misconfiguration.
	//
	// It compares the counter to the balance. `config.yml` and the genesis builder both declare the
	// module account funded; a gate that read the declared intent would pass while the deployed file
	// diverged from it.
	//
	// Refusing to create a chain protects it; refusing to join an existing one only hurts. A network
	// whose genesis already puts the Reserve on an ordinary account keeps running, since it never
	// replays InitGenesis, while a node built from the published source would panic at InitChain.
	// Creating and joining run the same code over the same file and no field tells them apart, so the
	// operator says which one it is, explicitly and never by default.
	//
	// This cannot fork anything, which is the only reason it may be a switch: the guard only reads. It
	// writes no state and changes no value, so a node that skips it computes the same state as a node
	// that never had it. The variable decides whether this node starts, never what the chain agrees on.
	// It is an environment variable because InitGenesis sees no command line.
	if err := k.reserveIsBacked(ctx, genState); err != nil {
		if os.Getenv(allowUnbackedReserveEnv) != "1" {
			return err
		}
		// Logged once, at error level, because nothing downstream repeats it: the deferral events only
		// begin at the first epoch, which is days away at epoch_blocks=86400.
		sdk.UnwrapSDKContext(ctx).Logger().Error(
			"emission reserve unbacked -- genesis loaded anyway because "+allowUnbackedReserveEnv+"=1. This chain "+
				"cannot pay rewards: every epoch defers with security_transfer_failed until the module account is "+
				"funded or the network restarts from a corrected genesis. Joining is legitimate; launching is not.",
			"detail", err.Error())
	}

	// Resume or bootstrap: the distinction this file has to make.
	//
	// Resetting the Reserve to `GenesisReserveU` and the epoch to 0 on every InitGenesis, without
	// looking at what genesis contains, would resurrect an already-spent Reserve on any export/import,
	// the usual step of a chain migration. It would also erase the unclaimed pools (subsidies owed to
	// miners would vanish while the coins backing them were counted again as Reserve) and fire an
	// epoch at the first block.
	//
	// `state` is an optional message, which is what makes the distinction possible: proto3 does not
	// separate "absent" from "zero" on a scalar, and an exhausted Reserve is legitimately zero. Absent
	// means a new chain, so bootstrap; present means resume, so restore as is, zeros included.
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

	// ADR-023: bootstrap of a new chain (Reserve = 3.3 M DNDR, epoch at 0).
	if err := k.Reserve.Set(ctx, GenesisReserveU); err != nil {
		return err
	}

	// The three pockets are set to zero explicitly, which makes the export/import round trip an
	// identity.
	//
	// Without these lines a new chain has no key at all for `workpool` / `availpool` / `securitypool`,
	// because nothing writes them before the first epoch. `ExportGenesis` must still carry them: proto3
	// does not distinguish "absent" from "zero" on a scalar, so the export writes three zeros and the
	// import sets them. The re-imported chain would then hold three keys the original did not:
	// semantically identical, different bytes, so a different app hash. These pockets hold money
	// (`WorkPool`/`AvailPool` are claimable), so hiding the difference behind an exception would blind
	// the round-trip test to a real divergence of the pools.
	//
	// Not writing a zero on import looks cheaper, but a pocket that was really emptied is legitimately 0
	// (`pay_work.go` writes `pool-amt`), so it would disappear on re-import: the same artifact, inverted
	// and rarer, so harder to see. Making presence uniform from bootstrap removes the case instead.
	//
	// The cost: three more keys in the genesis state, so the genesis app hash changes. That is a
	// consensus change that requires a chain reset, and it ships with the reset already planned. No live
	// chain carries this genesis (`DefaultGenesis()` has no state and `config.yml` only sets the emission
	// parameters), so waiting would save nothing and leave the round-trip test blind.
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
	// exact, so absence is not an error here.
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
// Which Reserve is checked: on a resume, the value the export carried; on a bootstrap,
// `GenesisReserveU`, which the branch above is about to write. Both are claims about money that must
// already be there.
//
// It uses `SpendableCoins` rather than a raw balance because the pools pay out of spendable coins
// (`pay_work.go`, `pay_avail.go`) and `RunEpoch` moves spendable coins, so spendable is the quantity
// that decides whether the module can honour its counter. A vested or locked balance would satisfy a
// naive balance check and still defer every epoch.
//
// A zero Reserve needs no backing: an exhausted Reserve is a legitimate end state, and an export
// carrying it must re-import.
//
// It only returns an error and writes nothing, which lets the caller downgrade it to a logged warning
// for an operator joining an already-unbacked network, with no risk of divergence.

// allowUnbackedReserveEnv is the only way past the refusal, set to "1" by an operator who knows the
// network they are joining is already unbacked. When it is absent or holds any other value, the
// refusal stands. Compose forwards only the variables it names, so this one has to be listed in
// deploy/testnet-node/docker-compose.yml to reach the container at all.
const allowUnbackedReserveEnv = "DENDRA_ALLOW_UNBACKED_RESERVE"

// The counter to back is the whole identity, not a third of it.
//
// `RunEpoch` states the invariant this module lives by: `balance(emission module) == Reserve +
// WorkPool + AvailPool`, because security is the only flow that leaves the module; work and
// availability stay there as counters awaiting their own payout path (`pay_work.go`, `pay_avail.go`,
// both of which read `SpendableCoins` of that address before sending). Checking `Reserve` alone
// would accept a genesis that declares pools nothing backs: the chain boots, looks healthy, and the
// shortfall surfaces at the first work or availability payment. `SecurityPool` is not summed on
// purpose: those coins are sent out to `fee_collector`, so requiring the module to still hold them
// would refuse a correct genesis.
//
// The sum is computed in a width that cannot wrap: three uint64 counters can overflow uint64, and an
// overflow here would understate what must be backed, the reassuring direction, on the check that
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
				"not backed by the module account %s, which holds %s udndr. The counter is not the money: these coins "+
				"must be credited to the module address in genesis, not to an ordinary account. Only SecurityPool "+
				"leaves the module, so it is excluded from this sum. Started unchecked, the chain looks healthy until "+
				"the first epoch and then defers every one of them (security_transfer_failed) -- reward budget "+
				"unreachable, work and availability payments short of coins, and nothing saying so before that "+
				"height. If you are joining a network already in this state rather than launching one, set "+
				allowUnbackedReserveEnv+"=1 and this becomes a logged warning instead of a refusal",
			resv, work, avail, declared, modAddr.String(), held.String())
	}
	return nil
}
