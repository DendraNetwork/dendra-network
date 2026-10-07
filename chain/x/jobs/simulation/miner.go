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

		// ADR-039 — the identifier is DERIVED from the signer, so the simulation cannot draw one at
		// random any more. It used to send `strconv.Itoa(r.Int())`, which the identity guard now
		// refuses on every single draw: the operation would report a failure on every step and the
		// simulation would stop exercising registration at all, silently.
		//
		// Deriving here also makes the "already exists" branch below mean what it says. With a random
		// id that branch was effectively dead (two random ints colliding), whereas one address has
		// exactly one identifier — so re-drawing the same account is now the COMMON case, and skipping
		// it is the correct no-op rather than a rare curiosity.
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

		// THE SCAFFOLD SENT `Stake: 0` — a state the chain CANNOT produce, which is why the four
		// simulation tests were skipped. `CreateMiner` requires TWO things the generated message
		// ignored: a stake >= `min_stake` (ADR-018, minimum bond) and, since GO-13, that the signer
		// actually HOLDS that bond — it is escrowed as real coins to the module account. A declared
		// stake with no deposit is fictitious skin in the game; the simulation message still followed
		// the old model.
		//
		// The FIXTURE is what gets fixed: it reads the parameter instead of guessing it, and it backs
		// out cleanly when the drawn account lacks the funds. A simulation operation that cannot succeed
		// is not a strict test, it is an aborted simulation — and that is exactly what made
		// `TestAppImportExport` unreachable.
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
			CoinsSpentInMsg: bond, // the bond LEAVES the account: declare it, or fees are computed on a balance that will no longer exist
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
