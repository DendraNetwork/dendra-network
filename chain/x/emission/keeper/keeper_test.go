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

	"dendra/x/emission/keeper"
	module "dendra/x/emission/module"
	"dendra/x/emission/types"
)

// mockEmBank — bank EN MÉMOIRE pour tester `RunEpoch` (EM-02). Sans elle, le fixture passait `bank=nil`
// et AUCUN test n'appelait `RunEpoch` → l'invariant `solde == Reserve + WorkPool + AvailPool` et le
// mouvement de coins n'étaient couverts par RIEN. `supply` pilote la demande (= baisse de supply) ;
// `moved` enregistre ce qui sort vers fee_collector ; `fail` simule un compte de module non financé.
type mockEmBank struct {
	supply    uint64
	moved     map[string]sdk.Coins
	fail      bool
	spendable uint64               // solde dépensable simulé du compte de module emission
	paid      map[string]sdk.Coins // versements SendCoinsFromModuleToAccount (par destinataire)
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
		return fmt.Errorf("compte de module emission non finance")
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

	bank := &mockEmBank{}
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
