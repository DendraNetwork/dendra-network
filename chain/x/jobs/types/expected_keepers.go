package types

import (
	"context"

	"cosmossdk.io/core/address"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// AuthKeeper defines the expected interface for the Auth module.
type AuthKeeper interface {
	AddressCodec() address.Codec
	GetAccount(context.Context, sdk.AccAddress) sdk.AccountI // only used for simulation
}

// BankKeeper defines the expected interface for the Bank module.
type BankKeeper interface {
	SpendableCoins(context.Context, sdk.AccAddress) sdk.Coins
	// account-to-account transfer (direct Dendra payments: settle-pay)
	SendCoins(context.Context, sdk.AccAddress, sdk.AccAddress, sdk.Coins) error
	// H3 -- ESCROW: deposit into the MODULE account and pay out from the module.
	// The "jobs" module account already exists (declared in app/app_config.go, not blocked); these
	// methods are provided by the real bank keeper, so the interface is satisfied without touching app.go.
	SendCoinsFromAccountToModule(context.Context, sdk.AccAddress, string, sdk.Coins) error
	SendCoinsFromModuleToAccount(context.Context, string, sdk.AccAddress, sdk.Coins) error
	// deflation -- REAL soft burn from the module's escrow (jobs holds the Burner permission).
	BurnCoins(context.Context, string, sdk.Coins) error
}

// EmissionKeeper pays out REAL coins from the emission pools. Implemented by x/emission; PayWork and
// PayAvail return the amount ACTUALLY paid, bounded by the pool and by the module account's balance.
type EmissionKeeper interface {
	// Phase 1a -- WORK: pay the work subsidy from the WorkPool.
	PayWork(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error)
	// Phase 1b -- AVAILABILITY: pay from the AvailPool and read its balance (budget sizing).
	PayAvail(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error)
	AvailPoolBalance(ctx context.Context) (uint64, error)
}

// ModelRegistryKeeper lets the chain check that a model is registered and active.
type ModelRegistryKeeper interface {
	IsActive(ctx context.Context, id string) bool
	// ExpectedWeights: the weights_sha256 ANCHORED in the registry for `id`, plus true when active.
	ExpectedWeights(ctx context.Context, id string) (string, bool)
	// ActiveWithoutAnchor: ACTIVE models with no weights_sha256. Such a model is served WITHOUT any
	// model_id <-> artefact binding; the list exists to refuse a genesis that claims to ENFORCE the
	// registry while leaving unanchored models in it.
	ActiveWithoutAnchor(ctx context.Context) ([]string, error)
}

// ParamSubspace defines the expected Subspace interface for parameters.
type ParamSubspace interface {
	Get(context.Context, []byte, interface{})
	Set(context.Context, []byte, interface{})
}
