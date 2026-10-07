package keeper_test

import (
	"context"
	"fmt"
	"testing"

	"cosmossdk.io/core/address"
	"cosmossdk.io/math"
	storetypes "cosmossdk.io/store/types"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/testutil"
	sdk "github.com/cosmos/cosmos-sdk/types"
	moduletestutil "github.com/cosmos/cosmos-sdk/types/module/testutil"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	module "github.com/DendraNetwork/dendra-network/chain/x/emission/module"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// mockEmBank — an IN-MEMORY bank used to test `RunEpoch`. `supply` drives demand (a drop in supply);
// `moved` records what leaves for the fee_collector; `fail` simulates a bank refusal.
//
// ITS DEFAULT SPENDABLE BALANCE BACKS THE GENESIS RESERVE, and that is not a convenience: a chain
// whose Reserve counter is not covered by its module account is exactly what `InitGenesis` refuses,
// so a fixture leaving the module at zero would model a chain that cannot exist. Every case that
// cares about a specific balance sets `spendable` itself (the RunEpoch and pay_work cases do); the
// default is the funded chain they all start from.
type mockEmBank struct {
	supply    uint64
	moved     map[string]sdk.Coins
	fail      bool
	spendable uint64               // simulated spendable balance of the emission module account
	paid      map[string]sdk.Coins // SendCoinsFromModuleToAccount payouts, per recipient
}

func (m *mockEmBank) SpendableCoins(context.Context, sdk.AccAddress) sdk.Coins {
	if m.spendable == 0 {
		return nil
	}
	return sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(m.spendable)))
}

func (m *mockEmBank) SendCoinsFromModuleToAccount(_ context.Context, _ string, to sdk.AccAddress, amt sdk.Coins) error {
	if m.paid == nil {
		m.paid = map[string]sdk.Coins{}
	}
	m.paid[to.String()] = m.paid[to.String()].Add(amt...)
	return nil
}

func (m *mockEmBank) GetSupply(_ context.Context, denom string) sdk.Coin {
	return sdk.NewCoin(denom, math.NewIntFromUint64(m.supply))
}

func (m *mockEmBank) SendCoinsFromModuleToModule(_ context.Context, _, to string, amt sdk.Coins) error {
	if m.fail {
		return fmt.Errorf("emission module account is not funded")
	}
	if m.moved == nil {
		m.moved = map[string]sdk.Coins{}
	}
	m.moved[to] = m.moved[to].Add(amt...)
	return nil
}

type fixture struct {
	ctx          context.Context
	keeper       keeper.Keeper
	addressCodec address.Codec
	bank         *mockEmBank
}

func initFixture(t *testing.T) *fixture {
	t.Helper()

	encCfg := moduletestutil.MakeTestEncodingConfig(module.AppModule{})
	addressCodec := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	storeKey := storetypes.NewKVStoreKey(types.StoreKey)

	storeService := runtime.NewKVStoreService(storeKey)
	ctx := testutil.DefaultContextWithDB(t, storeKey, storetypes.NewTransientStoreKey("transient_test")).Ctx

	authority := authtypes.NewModuleAddress(types.GovModuleName)

	bank := &mockEmBank{spendable: keeper.GenesisReserveU}
	k := keeper.NewKeeper(
		storeService,
		encCfg.Codec,
		addressCodec,
		authority,
		bank,
	)

	// Initialize params
	if err := k.Params.Set(ctx, types.DefaultParams()); err != nil {
		t.Fatalf("failed to set params: %v", err)
	}

	return &fixture{
		ctx:          ctx,
		keeper:       k,
		addressCodec: addressCodec,
		bank:         bank,
	}
}
