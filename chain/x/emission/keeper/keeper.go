package keeper

import (
	"fmt"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/address"
	corestore "cosmossdk.io/core/store"
	"github.com/cosmos/cosmos-sdk/codec"

	"dendra/x/emission/types"
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

	// Moteur d'émission Réserve (TK-02 / ADR-023) : état en udndr.
	Reserve      collections.Item[uint64]
	WorkPool     collections.Item[uint64]
	AvailPool    collections.Item[uint64]
	SecurityPool collections.Item[uint64]
	LastEpoch    collections.Item[uint64]
	LastSupply   collections.Item[uint64] // supply udndr à la dernière époque -> delta = demande (burns)

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

		Reserve:      collections.NewItem(sb, types.ReserveKey, "reserve", collections.Uint64Value),
		WorkPool:     collections.NewItem(sb, types.WorkPoolKey, "workpool", collections.Uint64Value),
		AvailPool:    collections.NewItem(sb, types.AvailPoolKey, "availpool", collections.Uint64Value),
		SecurityPool: collections.NewItem(sb, types.SecurityPoolKey, "securitypool", collections.Uint64Value),
		LastEpoch:    collections.NewItem(sb, types.LastEpochKey, "lastepoch", collections.Uint64Value),
		LastSupply:   collections.NewItem(sb, types.LastSupplyKey, "lastsupply", collections.Uint64Value),
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
