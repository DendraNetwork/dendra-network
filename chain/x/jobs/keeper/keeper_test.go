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

	"dendra/x/jobs/keeper"
	module "dendra/x/jobs/module"
	"dendra/x/jobs/types"
)

// mockBankKeeper — bank EN MÉMOIRE pour les tests (GO-13 : escrow/slash/refund RÉELS testables).
// Une adresse sans solde explicite est traitée comme "riche" (les tests existants n'ont pas à
// financer leurs créateurs) ; setBalance borne un compte pour exercer le rejet "fonds insuffisants".
type mockBankKeeper struct {
	bal    map[string]sdk.Coins // solde par adresse (bech32)
	mod    map[string]sdk.Coins // solde par compte de module (nom)
	burned sdk.Coins            // total brûlé (déflation v5) — pour les assertions de test
}

func newMockBank() *mockBankKeeper {
	return &mockBankKeeper{bal: map[string]sdk.Coins{}, mod: map[string]sdk.Coins{}}
}

func richCoins() sdk.Coins { return sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000))) }

func (m *mockBankKeeper) balOf(a sdk.AccAddress) sdk.Coins {
	if c, ok := m.bal[a.String()]; ok {
		return c
	}
	return richCoins()
}

func (m *mockBankKeeper) setBalance(a sdk.AccAddress, c sdk.Coins) { m.bal[a.String()] = c }

func (m *mockBankKeeper) SpendableCoins(_ context.Context, a sdk.AccAddress) sdk.Coins {
	return m.balOf(a)
}

func (m *mockBankKeeper) SendCoins(_ context.Context, from, to sdk.AccAddress, amt sdk.Coins) error {
	fb := m.balOf(from)
	if !fb.IsAllGTE(amt) {
		return fmt.Errorf("solde insuffisant: %s < %s", fb, amt)
	}
	m.bal[from.String()] = fb.Sub(amt...)
	m.bal[to.String()] = m.balOf(to).Add(amt...)
	return nil
}

func (m *mockBankKeeper) SendCoinsFromAccountToModule(_ context.Context, from sdk.AccAddress, moduleName string, amt sdk.Coins) error {
	fb := m.balOf(from)
	if !fb.IsAllGTE(amt) {
		return fmt.Errorf("solde insuffisant: %s < %s", fb, amt)
	}
	m.bal[from.String()] = fb.Sub(amt...)
	m.mod[moduleName] = m.mod[moduleName].Add(amt...)
	return nil
}

func (m *mockBankKeeper) SendCoinsFromModuleToAccount(_ context.Context, moduleName string, to sdk.AccAddress, amt sdk.Coins) error {
	mb := m.mod[moduleName]
	if !mb.IsAllGTE(amt) {
		return fmt.Errorf("module %s insuffisant: %s < %s", moduleName, mb, amt)
	}
	m.mod[moduleName] = mb.Sub(amt...)
	m.bal[to.String()] = m.balOf(to).Add(amt...)
	return nil
}

func (m *mockBankKeeper) BurnCoins(_ context.Context, moduleName string, amt sdk.Coins) error {
	mb := m.mod[moduleName]
	if !mb.IsAllGTE(amt) {
		return fmt.Errorf("burn module %s insuffisant: %s < %s", moduleName, mb, amt)
	}
	m.mod[moduleName] = mb.Sub(amt...)
	m.burned = m.burned.Add(amt...)
	return nil
}

// mockEmissionKeeper — emission EN MEMOIRE : PayWork verse depuis un WorkPool simule (Phase 1a).
type mockEmissionKeeper struct {
	pool      uint64            // WorkPool simule
	avail     uint64            // AvailPool simule (Phase 1b)
	paid      map[string]uint64 // total verse par destinataire (bech32)
	availPaid map[string]uint64 // total verse en disponibilite (Phase 1b)
}

func (m *mockEmissionKeeper) PayWork(_ context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt > m.pool {
		amt = m.pool
	}
	if amt == 0 {
		return 0, nil
	}
	m.pool -= amt
	if m.paid == nil {
		m.paid = map[string]uint64{}
	}
	m.paid[recipient.String()] += amt
	return amt, nil
}

func (m *mockEmissionKeeper) PayAvail(_ context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error) {
	if amt > m.avail {
		amt = m.avail
	}
	if amt == 0 {
		return 0, nil
	}
	m.avail -= amt
	if m.availPaid == nil {
		m.availPaid = map[string]uint64{}
	}
	m.availPaid[recipient.String()] += amt
	return amt, nil
}

func (m *mockEmissionKeeper) AvailPoolBalance(_ context.Context) (uint64, error) { return m.avail, nil }

// mockModelRegistry — registre EN MEMOIRE : IsActive renvoie l'etat simule (incrément C).
type mockModelRegistry struct {
	active  map[string]bool
	weights map[string]string
}

func (m *mockModelRegistry) IsActive(_ context.Context, id string) bool {
	return m.active[id]
}

// ExpectedWeights — mock NEW-MR-03 : renvoie le weights ancré simulé + actif.
func (m *mockModelRegistry) ExpectedWeights(_ context.Context, id string) (string, bool) {
	if !m.active[id] {
		return "", false
	}
	return m.weights[id], true
}

type fixture struct {
	ctx          context.Context
	keeper       keeper.Keeper
	addressCodec address.Codec
	bank         *mockBankKeeper
	emission     *mockEmissionKeeper
	modelReg     *mockModelRegistry
	authority    string // adresse gov (bech32) — autorité des handlers gatés (slash manuel, params)
}

func initFixture(t *testing.T) *fixture {
	t.Helper()

	encCfg := moduletestutil.MakeTestEncodingConfig(module.AppModule{})
	addressCodec := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	storeKey := storetypes.NewKVStoreKey(types.StoreKey)

	storeService := runtime.NewKVStoreService(storeKey)
	ctx := testutil.DefaultContextWithDB(t, storeKey, storetypes.NewTransientStoreKey("transient_test")).Ctx

	authority := authtypes.NewModuleAddress(types.GovModuleName)
	authorityStr, err := addressCodec.BytesToString(authority)
	if err != nil {
		t.Fatalf("authority addr: %v", err)
	}

	bank := newMockBank()
	emission := &mockEmissionKeeper{pool: 1_000_000_000_000, avail: 1_000_000_000_000} // pools larges -> tests existants passent
	modelReg := &mockModelRegistry{active: map[string]bool{}, weights: map[string]string{}}
	k := keeper.NewKeeper(
		storeService,
		encCfg.Codec,
		addressCodec,
		authority,
		bank,
		emission,
		modelReg,
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
		emission:     emission,
		modelReg:     modelReg,
		authority:    authorityStr,
	}
}
