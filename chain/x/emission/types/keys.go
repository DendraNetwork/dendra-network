package types

import "cosmossdk.io/collections"

const (
	// ModuleName defines the module name
	ModuleName = "emission"

	// StoreKey defines the primary module store key
	StoreKey = ModuleName

	// GovModuleName duplicates the gov module's name to avoid a dependency with x/gov.
	// It should be synced with the gov module's name if it is ever changed.
	// See: https://github.com/cosmos/cosmos-sdk/blob/v0.52.0-beta.2/x/gov/types/keys.go#L9
	GovModuleName = "gov"
)

// ParamsKey is the prefix to retrieve all Params
var ParamsKey = collections.NewPrefix("p_emission")

// État du moteur d'émission (TK-02 / ADR-023). udndr en collections.Item[uint64].
var (
	ReserveKey      = collections.NewPrefix("reserve_emission")      // Réserve restante (udndr)
	WorkPoolKey     = collections.NewPrefix("workpool_emission")     // cumul flux travail
	AvailPoolKey    = collections.NewPrefix("availpool_emission")    // cumul flux disponibilité
	SecurityPoolKey = collections.NewPrefix("securitypool_emission") // cumul flux sécurité
	LastEpochKey    = collections.NewPrefix("lastepoch_emission")    // hauteur de la dernière époque
	LastSupplyKey   = collections.NewPrefix("lastsupply_emission")   // supply udndr à la dernière époque (signal demande)
)
