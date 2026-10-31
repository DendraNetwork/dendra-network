package keeper

import (
	"fmt"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/address"
	corestore "cosmossdk.io/core/store"
	"github.com/cosmos/cosmos-sdk/codec"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

type Keeper struct {
	storeService corestore.KVStoreService
	cdc          codec.Codec
	addressCodec address.Codec
	// Address capable of executing a MsgUpdateParams message.
	// Typically, this should be the x/gov module account.
	authority []byte

	Schema collections.Schema
	Params collections.Item[types.Params]

	// Reserve emission engine: state denominated in udndr.
	// WorkPool and AvailPool are claimable pools (coins held in the module account until their payout).
	// SecurityDistributedTotal is a telemetry accumulator, not a pool: the security coins have already
	// left for the fee_collector (epoch.go, SendCoinsFromModuleToModule) when this counter is
	// incremented, so there is nothing left to claim. A `PaySecurity` that read it as a claimable pool
	// would spend the same coins twice (breaking invariant #3). It was named `SecurityPool` until
	// consensus epoch 15; the name now carries the "already paid out" invariant,
	// and its key bytes are unchanged (types/keys.go).
	Reserve                  collections.Item[uint64]
	WorkPool                 collections.Item[uint64]
	AvailPool                collections.Item[uint64]
	SecurityDistributedTotal collections.Item[uint64]
	LastEpoch                collections.Item[uint64]
	LastSupply               collections.Item[uint64] // udndr supply at the last epoch -> delta = demand (burns)
	// Cumulative payouts of the two claimable pools, the split the Pools query exposes:
	// PayWork and PayAvail add what they send. Absent reads as 0.
	WorkPaidTotal  collections.Item[uint64]
	AvailPaidTotal collections.Item[uint64]

	bankKeeper types.BankKeeper
}

func NewKeeper(
	storeService corestore.KVStoreService,
	cdc codec.Codec,
	addressCodec address.Codec,
	authority []byte,

	bankKeeper types.BankKeeper,
) Keeper {
	if _, err := addressCodec.BytesToString(authority); err != nil {
		panic(fmt.Sprintf("invalid authority address %s: %s", authority, err))
	}

	sb := collections.NewSchemaBuilder(storeService)

	k := Keeper{
		storeService: storeService,
		cdc:          cdc,
		addressCodec: addressCodec,
		authority:    authority,

		bankKeeper: bankKeeper,
		Params:     collections.NewItem(sb, types.ParamsKey, "params", codec.CollValue[types.Params](cdc)),

		Reserve:                  collections.NewItem(sb, types.ReserveKey, "reserve", collections.Uint64Value),
		WorkPool:                 collections.NewItem(sb, types.WorkPoolKey, "workpool", collections.Uint64Value),
		AvailPool:                collections.NewItem(sb, types.AvailPoolKey, "availpool", collections.Uint64Value),
		SecurityDistributedTotal: collections.NewItem(sb, types.SecurityDistributedTotalKey, "security_distributed_total", collections.Uint64Value),
		LastEpoch:                collections.NewItem(sb, types.LastEpochKey, "lastepoch", collections.Uint64Value),
		LastSupply:               collections.NewItem(sb, types.LastSupplyKey, "lastsupply", collections.Uint64Value),
		WorkPaidTotal:            collections.NewItem(sb, types.WorkPaidTotalKey, "work_paid_total", collections.Uint64Value),
		AvailPaidTotal:           collections.NewItem(sb, types.AvailPaidTotalKey, "avail_paid_total", collections.Uint64Value),
	}

	schema, err := sb.Build()
	if err != nil {
		panic(err)
	}
	k.Schema = schema

	return k
}

// GetAuthority returns the module's authority.
func (k Keeper) GetAuthority() []byte {
	return k.authority
}
