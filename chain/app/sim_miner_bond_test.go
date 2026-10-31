package app

import (
	"encoding/json"
	"testing"

	sdkmath "cosmossdk.io/math"
	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	simtestutil "github.com/cosmos/cosmos-sdk/testutil/sims"
	sdk "github.com/cosmos/cosmos-sdk/types"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	banktypes "github.com/cosmos/cosmos-sdk/x/bank/types"
	stakingtypes "github.com/cosmos/cosmos-sdk/x/staking/types"
	"github.com/stretchr/testify/require"

	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// simAppStateFn is the simulation genesis of `app` in which every simulation account can also post one
// miner bond. The SDK generator funds the accounts in the staking denomination only, while
// `CreateMiner` escrows `min_stake` in udndr, so `SimulateMsgCreateMiner` found no account able to
// post it and returned a no-op on every draw: registration and the bond escrow were never exercised,
// and update and delete only ever saw the genesis miners. One bond per account is enough, since an
// address registers one miner (its id is derived from the address) and the delete refunds the bond.
func simAppStateFn(app *App) simtypes.AppStateFn {
	cdc := app.AppCodec()
	return simtestutil.AppStateFnWithExtendedCb(cdc, app.SimulationManager(), app.DefaultGenesis(),
		func(raw map[string]json.RawMessage) { fundSimMinerBonds(cdc, raw) })
}

// fundSimMinerBonds adds the `min_stake` of the generated x/jobs genesis, in udndr, to every account
// balance of the generated bank genesis, and to the declared supply when the generator declared one.
// The staking pools are left out: staking's InitGenesis requires their balance to equal the tokens
// it tracks, in the staking denomination alone.
func fundSimMinerBonds(cdc codec.JSONCodec, raw map[string]json.RawMessage) {
	var jobsGen jobstypes.GenesisState
	cdc.MustUnmarshalJSON(raw[jobstypes.ModuleName], &jobsGen)
	if jobsGen.Params.MinStake == 0 {
		return
	}
	bond := sdk.NewCoins(sdk.NewCoin("udndr", sdkmath.NewIntFromUint64(jobsGen.Params.MinStake)))
	var bankGen banktypes.GenesisState
	cdc.MustUnmarshalJSON(raw[banktypes.ModuleName], &bankGen)
	pools := map[string]bool{
		authtypes.NewModuleAddress(stakingtypes.NotBondedPoolName).String(): true,
		authtypes.NewModuleAddress(stakingtypes.BondedPoolName).String():    true,
	}
	declared := !bankGen.Supply.Empty()
	for i, b := range bankGen.Balances {
		if pools[b.Address] {
			continue
		}
		bankGen.Balances[i].Coins = b.Coins.Add(bond...)
		if declared {
			bankGen.Supply = bankGen.Supply.Add(bond...)
		}
	}
	raw[banktypes.ModuleName] = cdc.MustMarshalJSON(&bankGen)
}

func TestFundSimMinerBondsGivesEachAccountOneBond(t *testing.T) {
	cdc := codec.NewProtoCodec(codectypes.NewInterfaceRegistry())
	accA := sdk.AccAddress([]byte("sim-bond-account-a__")).String()
	accB := sdk.AccAddress([]byte("sim-bond-account-b__")).String()
	pool := authtypes.NewModuleAddress(stakingtypes.NotBondedPoolName).String()
	stake := sdk.NewCoins(sdk.NewCoin(sdk.DefaultBondDenom, sdkmath.NewInt(500)))
	bank := banktypes.GenesisState{
		Params: banktypes.DefaultParams(),
		Balances: []banktypes.Balance{
			{Address: accA, Coins: stake},
			{Address: accB, Coins: stake},
			{Address: pool, Coins: stake},
		},
		Supply: sdk.NewCoins(sdk.NewCoin(sdk.DefaultBondDenom, sdkmath.NewInt(1500))),
	}
	jobs := jobstypes.GenesisState{Params: jobstypes.DefaultParams()}
	require.NotZero(t, jobs.Params.MinStake, "premise: the shipped min_stake is a real bond")
	raw := map[string]json.RawMessage{
		banktypes.ModuleName: cdc.MustMarshalJSON(&bank),
		jobstypes.ModuleName: cdc.MustMarshalJSON(&jobs),
	}

	fundSimMinerBonds(cdc, raw)

	var got banktypes.GenesisState
	cdc.MustUnmarshalJSON(raw[banktypes.ModuleName], &got)
	bond := sdkmath.NewIntFromUint64(jobs.Params.MinStake)
	require.Equal(t, bond, got.Balances[0].Coins.AmountOf("udndr"))
	require.Equal(t, bond, got.Balances[1].Coins.AmountOf("udndr"))
	require.True(t, got.Balances[2].Coins.AmountOf("udndr").IsZero(), "the staking pool holds the staking denomination only")
	require.Equal(t, bond.MulRaw(2), got.Supply.AmountOf("udndr"), "the declared supply counts the two bonds")
	require.Equal(t, sdkmath.NewInt(1500), got.Supply.AmountOf(sdk.DefaultBondDenom))
}
