package simulation

import (
	"math/rand"

	"cosmossdk.io/math"

	"github.com/cosmos/cosmos-sdk/baseapp"
	"github.com/cosmos/cosmos-sdk/client"
	sdk "github.com/cosmos/cosmos-sdk/types"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	"github.com/cosmos/cosmos-sdk/x/simulation"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

func SimulateMsgCreateMiner(
	ak types.AuthKeeper,
	bk types.BankKeeper,
	k keeper.Keeper,
	txGen client.TxConfig,
) simtypes.Operation {
	return func(r *rand.Rand, app *baseapp.BaseApp, ctx sdk.Context, accs []simtypes.Account, chainID string,
	) (simtypes.OperationMsg, []simtypes.FutureOperation, error) {
		simAccount, _ := simtypes.RandomAcc(r, accs)

		// The identifier is derived from the signer, so the simulation cannot draw one
		// at random. A random `strconv.Itoa(r.Int())` is refused by the identity guard on every
		// draw: the operation would fail at every step and the simulation would silently stop
		// exercising registration.
		//
		// Deriving here also makes the "already exists" branch below meaningful. With a random id that
		// branch was effectively dead (two random ints colliding), whereas one address has exactly one
		// identifier, so drawing the same account again is now the common case, and skipping it is the
		// correct no-op.
		minerID, err := types.DeriveMinerID(simAccount.Address.Bytes())
		if err != nil {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(&types.MsgCreateMiner{}), "cannot derive miner id"), nil, err
		}
		msg := &types.MsgCreateMiner{
			Creator: simAccount.Address.String(),
			MinerId: minerID,
		}

		found, err := k.Miner.Has(ctx, msg.MinerId)
		if err == nil && found {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(msg), "Miner already exist"), nil, nil
		}

		// The scaffold sent `Stake: 0`, a state the chain cannot produce, which is why the four
		// simulation tests were skipped. `CreateMiner` requires two things the generated message
		// ignored: a stake >= `min_stake` (minimum bond) and, since GO-13, that the signer
		// actually holds that bond, which is escrowed as real coins to the module account. A declared
		// stake with no deposit is fictitious skin in the game.
		//
		// The fixture reads the parameter instead of guessing it, and it backs out cleanly when the
		// drawn account lacks the funds. A simulation operation that cannot succeed aborts the
		// simulation, which is what made `TestAppImportExport` unreachable.
		params, perr := k.Params.Get(ctx)
		if perr != nil {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(msg), "params unreadable"), nil, nil
		}
		msg.Stake = params.MinStake
		bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Stake)))
		if !bk.SpendableCoins(ctx, simAccount.Address).IsAllGTE(bond) {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(msg), "bond not fundable by this account"), nil, nil
		}

		txCtx := simulation.OperationInput{
			R:               r,
			App:             app,
			TxGen:           txGen,
			Cdc:             nil,
			Msg:             msg,
			Context:         ctx,
			SimAccount:      simAccount,
			ModuleName:      types.ModuleName,
			CoinsSpentInMsg: bond, // the bond leaves the account: declare it, or fees are computed on a balance that will no longer exist
			AccountKeeper:   ak,
			Bankkeeper:      bk,
		}
		return simulation.GenAndDeliverTxWithRandFees(txCtx)
	}
}

func SimulateMsgUpdateMiner(
	ak types.AuthKeeper,
	bk types.BankKeeper,
	k keeper.Keeper,
	txGen client.TxConfig,
) simtypes.Operation {
	return func(r *rand.Rand, app *baseapp.BaseApp, ctx sdk.Context, accs []simtypes.Account, chainID string,
	) (simtypes.OperationMsg, []simtypes.FutureOperation, error) {
		var (
			simAccount = simtypes.Account{}
			miner      = types.Miner{}
			msg        = &types.MsgUpdateMiner{}
			found      = false
		)

		var allMiner []types.Miner
		err := k.Miner.Walk(ctx, nil, func(key string, value types.Miner) (stop bool, err error) {
			allMiner = append(allMiner, value)
			return false, nil
		})
		if err != nil {
			panic(err)
		}

		for _, obj := range allMiner {
			acc, err := ak.AddressCodec().StringToBytes(obj.Creator)
			if err != nil {
				return simtypes.OperationMsg{}, nil, err
			}

			simAccount, found = simtypes.FindAccount(accs, sdk.AccAddress(acc))
			if found {
				miner = obj
				break
			}
		}
		if !found {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(msg), "miner creator not found"), nil, nil
		}
		msg.Creator = simAccount.Address.String()
		msg.MinerId = miner.MinerId

		txCtx := simulation.OperationInput{
			R:               r,
			App:             app,
			TxGen:           txGen,
			Cdc:             nil,
			Msg:             msg,
			Context:         ctx,
			SimAccount:      simAccount,
			ModuleName:      types.ModuleName,
			CoinsSpentInMsg: sdk.NewCoins(),
			AccountKeeper:   ak,
			Bankkeeper:      bk,
		}
		return simulation.GenAndDeliverTxWithRandFees(txCtx)
	}
}

// SimulateMsgDeleteMiner sends a MsgDeleteMiner from the creator of a registered miner. The bond
// comes back to the signer, so no coin is spent by the message.
func SimulateMsgDeleteMiner(
	ak types.AuthKeeper,
	bk types.BankKeeper,
	k keeper.Keeper,
	txGen client.TxConfig,
) simtypes.Operation {
	return func(r *rand.Rand, app *baseapp.BaseApp, ctx sdk.Context, accs []simtypes.Account, chainID string,
	) (simtypes.OperationMsg, []simtypes.FutureOperation, error) {
		var (
			simAccount = simtypes.Account{}
			miner      = types.Miner{}
			msg        = &types.MsgDeleteMiner{}
			found      = false
		)

		var allMiner []types.Miner
		err := k.Miner.Walk(ctx, nil, func(key string, value types.Miner) (stop bool, err error) {
			allMiner = append(allMiner, value)
			return false, nil
		})
		if err != nil {
			panic(err)
		}

		for _, obj := range allMiner {
			acc, err := ak.AddressCodec().StringToBytes(obj.Creator)
			if err != nil {
				return simtypes.OperationMsg{}, nil, err
			}

			simAccount, found = simtypes.FindAccount(accs, sdk.AccAddress(acc))
			if found {
				miner = obj
				break
			}
		}
		if !found {
			return simtypes.NoOpMsg(types.ModuleName, sdk.MsgTypeURL(msg), "miner creator not found"), nil, nil
		}
		msg.Creator = simAccount.Address.String()
		msg.MinerId = miner.MinerId

		txCtx := simulation.OperationInput{
			R:               r,
			App:             app,
			TxGen:           txGen,
			Cdc:             nil,
			Msg:             msg,
			Context:         ctx,
			SimAccount:      simAccount,
			ModuleName:      types.ModuleName,
			CoinsSpentInMsg: sdk.NewCoins(),
			AccountKeeper:   ak,
			Bankkeeper:      bk,
		}
		return simulation.GenAndDeliverTxWithRandFees(txCtx)
	}
}
