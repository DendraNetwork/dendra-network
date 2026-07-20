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
	// transfert compte->compte (paiements directs Dendra : settle-pay)
	SendCoins(context.Context, sdk.AccAddress, sdk.AccAddress, sdk.Coins) error
	// H3 -- ESCROW : depot vers le compte de MODULE et versement depuis le module.
	// Le compte de module "jobs" existe deja (declare dans app/app_config.go, non bloque) ; ces
	// methodes sont fournies par le vrai bank keeper -> interface satisfaite sans toucher app.go.
	SendCoinsFromAccountToModule(context.Context, sdk.AccAddress, string, sdk.Coins) error
	SendCoinsFromModuleToAccount(context.Context, string, sdk.AccAddress, sdk.Coins) error
	// v5 : deflation -- burn doux REEL depuis l'escrow du module (jobs a la permission Burner).
	BurnCoins(context.Context, string, sdk.Coins) error
}

// EmissionKeeper : verser depuis les pools d'émission en VRAIS coins. Implementee par x/emission ;
// PayWork/PayAvail renvoient le montant REELLEMENT verse (borne par le pool/solde du module).
type EmissionKeeper interface {
	// Phase 1a -- TRAVAIL : verser la subvention de travail depuis le WorkPool.
	PayWork(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error)
	// Phase 1b -- DISPONIBILITE : verser depuis l'AvailPool + lire son solde (dimensionnement du budget).
	PayAvail(ctx context.Context, recipient sdk.AccAddress, amt uint64) (uint64, error)
	AvailPoolBalance(ctx context.Context) (uint64, error)
}

// ModelRegistryKeeper : la chaîne peut vérifier qu'un modèle est enregistré + actif (incrément C).
type ModelRegistryKeeper interface {
	IsActive(ctx context.Context, id string) bool
	// ExpectedWeights : weights_sha256 ANCRE au registre pour `id` + true si actif (NEW-MR-03).
	ExpectedWeights(ctx context.Context, id string) (string, bool)
}

// ParamSubspace defines the expected Subspace interface for parameters.
type ParamSubspace interface {
	Get(context.Context, []byte, interface{})
	Set(context.Context, []byte, interface{})
}
