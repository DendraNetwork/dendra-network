package keeper_test

import (
	"path/filepath"
	"testing"

	"cosmossdk.io/collections"
	colcodec "cosmossdk.io/collections/codec"
	storetypes "cosmossdk.io/store/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/cosmos/cosmos-sdk/types/kv"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// Schema prefixes, and the decoder that makes them readable.
//
// This file used to carry a test asserting "no prefix is a prefix of another", for a real reason: a
// `Walk` over the short prefix would sweep the long prefix's entries and decode them with the wrong
// codec. Fault injection seemed to validate it, since overlapping prefixes turned the test red, but the
// red came from a `panic` in `collections.SchemaBuilder.Build()`, which already refuses that case
// ("schema has overlapping prefixes"), not from the assertion. A node whose prefixes overlap does not
// start at all, so the test could only fail through another component's panic.
//
// What remains here:
//  1. a contract test on the dependency: does the guarantee still exist upstream? It goes red if a
//     future version of `collections` stops checking, which no other test in this repository would
//     notice;
//  2. the ghost prefix, declared in `types` and never registered in the schema, which `collections`
//     cannot see because it is never presented with it;
//  3. whether the decoder tells the truth: it names a real key, refuses to attribute a foreign key,
//     and survives a codec that panics.

// (1) Disjointness is guaranteed upstream; this test checks that the guarantee still exists.
//
// The property is not tested again on the real schema (it could only fail through a panic in
// `NewKeeper`): `collections` is handed a throwaway schema that violates it, and is required to refuse
// it. If a future version stops checking, this test goes red and the check must move back into the
// repository. Without it, the guarantee could disappear silently.
func TestSchemaPrefixesDisjointnessGuaranteedByCollections(t *testing.T) {
	sb := collections.NewSchemaBuilder(runtime.NewKVStoreService(storetypes.NewKVStoreKey("overlap")))
	_ = collections.NewMap(sb, collections.NewPrefix("job"), "short",
		collections.StringKey, collections.StringValue)
	_ = collections.NewMap(sb, collections.NewPrefix("job/value/"), "long",
		collections.StringKey, collections.StringValue)
	_, err := sb.Build()
	require.Error(t, err,
		"`collections` now accepts two prefixes where one prefixes the other. The consequence is "+
			"real: a Walk over the short one sweeps the long one's entries and decodes them with the "+
			"wrong codec — on this chain those traversals drive payments and committee draws. The "+
			"guarantee is gone upstream, so it has to be re-established here, on the keeper's schema.")
	require.Contains(t, err.Error(), "overlapping",
		"the error no longer mentions overlapping: check what `collections` refuses now")
}

// (3) Every prefix declared in `types` is registered in the schema.
//
// The direction of the check matters. An `XxxKey` variable declared but never handed to the
// SchemaBuilder is a ghost prefix: code can use it for direct store access, and that state belongs to
// no collection, so it is neither exported at genesis, nor seen by the decoder, nor compared by the
// round trip. That is the class of bug the decoder reports as a key outside the schema; it is cheaper
// to forbid it at declaration time.
func TestSchemaPrefixesNoGhostPrefixInTypes(t *testing.T) {
	declared, err := simutil.DeclaredPrefixes(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declared), 25,
		"only %d prefixes read from `types`: the scan no longer bites, so this check verifies nothing",
		len(declared))

	f := initFixture(t)
	ghosts := simutil.PhantomPrefixes(f.keeper.Schema, declared)
	require.Empty(t, ghosts,
		"prefix(es) declared in `x/jobs/types` but absent from the keeper's schema: %v.\n"+
			"Such a prefix can only be reached through direct store access: the state it carries is "+
			"neither exported at genesis, nor compared by the round-trip, nor readable by the decoder.",
		ghosts)
}

// (4) The decoder names a real key, and yields the value rather than raw bytes.
func TestDecodeStoreNamesARealKey(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	key := append(append([]byte{}, types.MinerKey.Bytes()...), []byte("miner-1")...)
	raw, err := f.keeper.Miner.ValueCodec().Encode(types.Miner{MinerId: "miner-1", Stake: 4242})
	require.NoError(t, err)

	out := dec(kv.Pair{Key: key, Value: raw}, kv.Pair{Key: key})
	require.Contains(t, out, "miner-1", "the decoder must render the key readable, not just its hex")
	require.Contains(t, out, "4242",
		"the decoder did not decode the value: it therefore falls back to raw bytes, i.e. to the "+
			"previous behaviour. Output:\n%s", out)
	require.Contains(t, out, "(absent)",
		"an empty value (a key present on one side only) must be reported as absent, not rendered as a "+
			"legitimate zero value — that is the whole difference between \"not there\" and \"set to "+
			"zero\". Output:\n%s", out)
	require.NotContains(t, out, "outside the schema")
}

// (5) The decoder refuses to attribute a foreign key, the failure raw hex could not express.
func TestDecodeStoreFlagsAKeyOutsideSchema(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	out := dec(kv.Pair{Key: []byte("zzz_written_by_hand/"), Value: []byte{0x01}}, kv.Pair{})
	require.Contains(t, out, "outside the schema",
		"the decoder attached an unknown key to a collection: a wrong name is worse than honest hex, "+
			"because it sends the reader looking in the wrong place. Output:\n%s", out)
}

// (6) A corrupt value is reported as undecodable, not rendered as a legitimate value.
func TestDecodeStoreCorruptValueIsReported(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	key := append(append([]byte{}, types.MinerKey.Bytes()...), []byte("miner-1")...)
	out := dec(kv.Pair{Key: key, Value: []byte{0xff, 0xff, 0xff, 0xff}}, kv.Pair{Key: key})
	require.Contains(t, out, "undecodable",
		"an unreadable value must be reported as unreadable: silently rendering it as a zero would send "+
			"the reader hunting for a content divergence where there is corruption. Output:\n%s", out)
}

// (7) The decoder does not panic when a codec panics.
//
// An earlier form of this test fed four `0xff` bytes to the `Miner` codec and required "does not
// panic". Removing the `recover` left that test green, because the proto codec returns an error on
// those bytes instead of panicking: the test measured the nominal path while believing it measured the
// panic path.
//
// Here a throwaway schema has a value codec that really panics, so the `recover` in `renderValue` is
// on the path and removing it turns this test red. This decoder only runs on the failure path of a
// round trip, when the state is abnormal, and an instrument that breaks when the measurement is
// abnormal only measures the normal case.
func TestDecodeStoreSurvivesAPanickingCodec(t *testing.T) {
	sb := collections.NewSchemaBuilder(runtime.NewKVStoreService(storetypes.NewKVStoreKey("trap")))
	_ = collections.NewMap(sb, collections.NewPrefix("trap/value/"), "trap",
		collections.StringKey, colcodec.ValueCodec[string](panickingCodec{}))
	sch, err := sb.Build()
	require.NoError(t, err)

	dec := simutil.NewDecodeStore(sch)
	var out string
	require.NotPanics(t, func() {
		out = dec(kv.Pair{Key: []byte("trap/value/k"), Value: []byte{0x01}}, kv.Pair{})
	}, "the decoder propagates the codec's panic: the round-trip diagnostic is replaced by a stack "+
		"trace, exactly when the diagnostic is needed")
	require.Contains(t, out, "panic while decoding", "the panic must be reported, not swallowed: output:\n%s", out)
	require.Contains(t, out, "trap", "the collection name must survive its codec's panic")
}

// panickingCodec is a value codec whose `Decode` panics. It exists only as fault injection for (7):
// without it, the decoder's `recover` would be code nothing exercises.
type panickingCodec struct{}

func (panickingCodec) Encode(v string) ([]byte, error)   { return []byte(v), nil }
func (panickingCodec) Decode([]byte) (string, error)     { panic("value codec panicking") }
func (panickingCodec) EncodeJSON(string) ([]byte, error) { return []byte(`""`), nil }
func (panickingCodec) DecodeJSON([]byte) (string, error) { return "", nil }
func (panickingCodec) Stringify(v string) string         { return v }
func (panickingCodec) ValueType() string                 { return "panickingCodec" }
