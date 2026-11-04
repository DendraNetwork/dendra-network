package keeper_test

import (
	"encoding/binary"
	"testing"

	storetypes "cosmossdk.io/store/types"
	addresscodec "github.com/cosmos/cosmos-sdk/codec/address"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/testutil"
	sdk "github.com/cosmos/cosmos-sdk/types"
	moduletestutil "github.com/cosmos/cosmos-sdk/types/module/testutil"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	module "github.com/DendraNetwork/dendra-network/chain/x/emission/module"
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// Epoch 15 renamed a counter. `SecurityPool` became
// `SecurityDistributedTotal`; whether the renamed item changed its key bytes decides whether the
// upgrade handler must copy state. It did not, by choice (types/keys.go), and this case is what keeps
// that choice from being undone by accident: a value written under the raw epoch-14 key is what the
// renamed item reads, so the handler copies nothing.
func TestEpoch15RenamedSecurityTotalKeepsItsKeyBytes(t *testing.T) {
	require.Equal(t, []byte("securitypool_emission"), types.SecurityDistributedTotalKey.Bytes(),
		"the prefix bytes are the epoch-14 bytes; changing them would move state at the upgrade for a label nobody reads")

	encCfg := moduletestutil.MakeTestEncodingConfig(module.AppModule{})
	addressCodec := addresscodec.NewBech32Codec(sdk.GetConfig().GetBech32AccountAddrPrefix())
	storeKey := storetypes.NewKVStoreKey(types.StoreKey)
	ctx := testutil.DefaultContextWithDB(t, storeKey, storetypes.NewTransientStoreKey("transient_e15")).Ctx
	k := keeper.NewKeeper(runtime.NewKVStoreService(storeKey), encCfg.Codec, addressCodec,
		authtypes.NewModuleAddress(types.GovModuleName), &mockEmBank{spendable: keeper.GenesisReserveU})

	// What an epoch-14 binary wrote: the raw key, an 8-byte big-endian value (collections.Uint64Value).
	raw := make([]byte, 8)
	binary.BigEndian.PutUint64(raw, 123456)
	ctx.KVStore(storeKey).Set([]byte("securitypool_emission"), raw)

	v, err := k.SecurityDistributedTotal.Get(ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(123456), v, "the renamed item reads the epoch-14 bytes in place")
}

// Epoch 15: the Pools query reports the six counters, the two paid totals move with the
// payouts, and an absent counter is 0.
func TestEpoch15PoolsQueryReportsTheCountersAndThePaidTotals(t *testing.T) {
	f := initFixture(t)
	q := keeper.NewQueryServerImpl(f.keeper)

	resp, err := q.Pools(f.ctx, &types.QueryPoolsRequest{})
	require.NoError(t, err)
	require.Equal(t, &types.QueryPoolsResponse{}, resp, "before anything is written every counter reads 0, and that 0 is measured")

	require.NoError(t, f.keeper.InitGenesis(f.ctx, *types.DefaultGenesis()))
	require.NoError(t, f.keeper.WorkPool.Set(f.ctx, 1000))
	require.NoError(t, f.keeper.AvailPool.Set(f.ctx, 500))
	require.NoError(t, f.keeper.SecurityDistributedTotal.Set(f.ctx, 77))
	recipient := sdk.AccAddress([]byte("recipient_e15_______"))

	paid, err := f.keeper.PayWork(f.ctx, recipient, 300)
	require.NoError(t, err)
	require.Equal(t, uint64(300), paid)
	paidA, err := f.keeper.PayAvail(f.ctx, recipient, 200)
	require.NoError(t, err)
	require.Equal(t, uint64(200), paidA)

	resp, err = q.Pools(f.ctx, &types.QueryPoolsRequest{})
	require.NoError(t, err)
	require.Equal(t, &types.QueryPoolsResponse{
		Reserve: keeper.GenesisReserveU, WorkPool: 700, AvailPool: 300,
		SecurityDistributedTotal: 77, WorkPaidTotal: 300, AvailPaidTotal: 200,
	}, resp)

	paid, err = f.keeper.PayWork(f.ctx, recipient, 100)
	require.NoError(t, err)
	require.Equal(t, uint64(100), paid)
	resp, err = q.Pools(f.ctx, &types.QueryPoolsRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(400), resp.WorkPaidTotal, "the total is cumulative: it never counts a payment the pool did not release")
	require.Equal(t, uint64(600), resp.WorkPool)
}
