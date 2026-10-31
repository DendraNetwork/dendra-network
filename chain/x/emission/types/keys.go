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

// State of the emission engine. Amounts are udndr, held in collections.Item[uint64].
var (
	ReserveKey   = collections.NewPrefix("reserve_emission")   // remaining Reserve (udndr)
	WorkPoolKey  = collections.NewPrefix("workpool_emission")  // cumulative work flow
	AvailPoolKey = collections.NewPrefix("availpool_emission") // cumulative availability flow
	// Cumulative security flow, already sent to fee_collector. The item was renamed from
	// `SecurityPool` at consensus epoch 15 because the old name read like a
	// claimable pool. The prefix bytes keep the old spelling on purpose: a prefix is read by no
	// human and by no query, and changing it would move state at the upgrade for a label; a test
	// pins the bytes so that the choice cannot be undone by accident.
	SecurityDistributedTotalKey = collections.NewPrefix("securitypool_emission")
	LastEpochKey                = collections.NewPrefix("lastepoch_emission")  // height of the last epoch
	LastSupplyKey               = collections.NewPrefix("lastsupply_emission") // udndr supply at the last epoch (demand signal)
	// Cumulative payouts of the two claimable pools (PayWork, PayAvail), read by the Pools query
	// (since epoch 15). Absent reads as 0 everywhere they are read.
	WorkPaidTotalKey  = collections.NewPrefix("workpaidtotal_emission")
	AvailPaidTotalKey = collections.NewPrefix("availpaidtotal_emission")
)
