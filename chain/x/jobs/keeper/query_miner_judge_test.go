package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	storetypes "cosmossdk.io/store/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The judge state of one miner is readable before a declaration. A second
// `judge = true` rewrites the declaration height and clears the missed draws, so a daemon that declared
// without reading would reopen the trial window the count was closing. These cases drive `MinerJudge`
// against a state the shipped `DeclareJudge` wrote, an undeclared miner, an id the registry does not
// hold, an empty id and an unreadable entry, and compare the raw store around every read.

// jobsKV returns the raw store of the module under the fixture, to compare every byte around a call
// and to write a value no collection codec would produce.
func jobsKV(t *testing.T, f *fixture) storetypes.KVStore {
	t.Helper()
	sdkCtx := sdk.UnwrapSDKContext(f.ctx)
	named, ok := sdkCtx.MultiStore().(interface {
		StoreKeysByName() map[string]storetypes.StoreKey
	})
	require.True(t, ok, "the fixture's multistore must list its keys")
	key, ok := named.StoreKeysByName()[types.StoreKey]
	require.True(t, ok, "the fixture must mount the jobs store")
	return sdkCtx.KVStore(key)
}

// kvSnapshot copies every key and value of the store.
func kvSnapshot(t *testing.T, kv storetypes.KVStore) map[string]string {
	t.Helper()
	it := kv.Iterator(nil, nil)
	defer it.Close()
	out := map[string]string{}
	for ; it.Valid(); it.Next() {
		out[string(it.Key())] = string(it.Value())
	}
	return out
}

func TestQueryMinerJudgeReadsTheFourEntriesAndWritesNothing(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op := a59Miner(t, f, "mJ", 1000)
	a59Miner(t, f, "mU", 1000)
	at := func(h int64) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h) }

	// A declared judge: declared at 42 through the shipped handler, last answer at 57, two draws missed.
	_, err := srv.DeclareJudge(at(42), &types.MsgDeclareJudge{Creator: op, MinerId: "mJ", Judge: true})
	require.NoError(t, err)
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "mJ", 57))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "mJ", 2))

	kv := jobsKV(t, f)
	before := kvSnapshot(t, kv)
	missedKey, err := collections.EncodeKeyWithPrefix(types.MinerMissedDrawsKey, collections.StringKey, "mJ")
	require.NoError(t, err)
	require.Contains(t, before, string(missedKey), "two empty snapshots would compare equal and prove nothing")

	resp, err := qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: "mJ"})
	require.NoError(t, err)
	require.Equal(t, &types.QueryMinerJudgeResponse{Declared: true, DeclaredHeight: 42, LastVerdictHeight: 57, MissedDraws: 2}, resp)
	require.Equal(t, before, kvSnapshot(t, kv), "a query writes nothing")

	// A registered miner with no declaration.
	resp, err = qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: "mU"})
	require.NoError(t, err, "a miner with no declaration answers zeros without an error")
	require.Equal(t, &types.QueryMinerJudgeResponse{}, resp)
	require.Equal(t, before, kvSnapshot(t, kv))

	// An id the registry does not hold answers the same, with no error: a read is not a registration check.
	registered, err := f.keeper.Miner.Has(f.ctx, "nobody")
	require.NoError(t, err)
	require.False(t, registered)
	resp, err = qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: "nobody"})
	require.NoError(t, err)
	require.Equal(t, &types.QueryMinerJudgeResponse{}, resp)
	require.Equal(t, before, kvSnapshot(t, kv))

	// The comparison can come out false: a second declaration at 60 rewrites the date and clears the
	// count, the snapshot sees the write, and the query reads it.
	_, err = srv.DeclareJudge(at(60), &types.MsgDeclareJudge{Creator: op, MinerId: "mJ", Judge: true})
	require.NoError(t, err)
	require.NotEqual(t, before, kvSnapshot(t, kv), "a write must show in the snapshot, or the equalities above prove nothing")
	resp, err = qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: "mJ"})
	require.NoError(t, err)
	require.Equal(t, &types.QueryMinerJudgeResponse{Declared: true, DeclaredHeight: 60, LastVerdictHeight: 57}, resp)
}

func TestQueryMinerJudgeRefusesAnEmptyIdAndReportsAnUnreadableEntry(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	_, err := qs.MinerJudge(f.ctx, nil)
	require.Equal(t, codes.InvalidArgument, status.Code(err))
	_, err = qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: ""})
	require.Equal(t, codes.InvalidArgument, status.Code(err), "an empty id names no miner and must not answer zeros")

	// A missed-draw count of three bytes, which the uint64 codec cannot decode: an unreadable entry is an
	// error, never a zero that reads as "no missed draw".
	require.NoError(t, f.keeper.MinerJudge.Set(f.ctx, "mX"))
	missedKey, err := collections.EncodeKeyWithPrefix(types.MinerMissedDrawsKey, collections.StringKey, "mX")
	require.NoError(t, err)
	jobsKV(t, f).Set(missedKey, []byte{1, 2, 3})
	_, err = qs.MinerJudge(f.ctx, &types.QueryMinerJudgeRequest{MinerId: "mX"})
	require.Equal(t, codes.Internal, status.Code(err))
}
