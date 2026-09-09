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

// ═══ SCHEMA PREFIXES, AND THE DECODER THAT MAKES THEM READABLE ═══════════════════════════════════
//
// This file used to carry a test asserting "no prefix is a prefix of another", justified by a real
// consequence: a `Walk` over the short prefix would sweep the long prefix's entries and decode them
// with the wrong codec. Fault injection appeared to validate it — overlapping prefixes turned the
// test red. But the red was NOT the assertion: it was a `panic` from
// `collections.SchemaBuilder.Build()`, which ALREADY refuses that case ("schema has overlapping
// prefixes"). A node whose prefixes overlap does not start at all, so the test could only ever go red
// through someone else's panic.
//
// The COLOUR of the injected fault had been read, not its MESSAGE. What remains here is what carries
// weight:
//  1. a CONTRACT test on the dependency — does the guarantee still exist upstream? It goes red if a
//     future version of `collections` stops checking, which no other test in this repository would
//     notice;
//  2. the GHOST PREFIX — declared in `types`, never registered in the schema. `collections` cannot
//     see that one: it is never presented with it;
//  3. whether the DECODER tells the truth — it names a real key, refuses to attribute a foreign key,
//     and survives a codec that panics.

// (1) DISJOINTNESS IS GUARANTEED UPSTREAM — this test checks that the GUARANTEE STILL EXISTS.
//
// The property is not re-tested on the real schema (it could only go red through a panic in
// `NewKeeper`): `collections` is handed a throwaway schema that violates it, and is required to
// REFUSE it. If a future version stops checking, this test goes red and it is time to move the check
// back into the repository. Without it, the guarantee could disappear silently.
func TestSchemaPrefixesDisjointnessGuaranteedByCollections(t *testing.T) {
	sb := collections.NewSchemaBuilder(runtime.NewKVStoreService(storetypes.NewKVStoreKey("chevauche")))
	_ = collections.NewMap(sb, collections.NewPrefix("job"), "court",
		collections.StringKey, collections.StringValue)
	_ = collections.NewMap(sb, collections.NewPrefix("job/value/"), "long",
		collections.StringKey, collections.StringValue)
	_, err := sb.Build()
	require.Error(t, err,
		"⛔ `collections` now ACCEPTS two prefixes where one prefixes the other. The consequence is "+
			"real: a Walk over the short one sweeps the long one's entries and decodes them with the "+
			"wrong codec — on this chain those traversals drive payments and committee draws. The "+
			"guarantee is gone upstream, so it has to be re-established here, on the keeper's schema.")
	require.Contains(t, err.Error(), "overlapping",
		"the error no longer mentions overlapping: check what `collections` refuses now")
}

// (3) EVERY PREFIX DECLARED IN `types` IS REGISTERED IN THE SCHEMA.
//
// The direction of the check matters. An `XxxKey` variable declared but never handed to the
// SchemaBuilder is a GHOST prefix: code can use it for direct store access, and that state belongs to
// no collection — so it is neither exported at genesis, nor seen by the decoder, nor compared by the
// round-trip. That is exactly the class of bug the decoder reports as "KEY OUTSIDE SCHEMA"; it is
// cheaper to forbid it at declaration time.
func TestSchemaPrefixesNoGhostPrefixInTypes(t *testing.T) {
	declared, err := simutil.PrefixesDeclares(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declared), 25,
		"only %d prefixes read from `types`: the scan no longer bites, so this check verifies nothing",
		len(declared))

	f := initFixture(t)
	ghosts := simutil.PhantomPrefixes(f.keeper.Schema, declared)
	require.Empty(t, ghosts,
		"⛔ prefix(es) declared in `x/jobs/types` but ABSENT from the keeper's schema: %v.\n"+
			"Such a prefix can only be reached through direct store access: the state it carries is "+
			"neither exported at genesis, nor compared by the round-trip, nor readable by the decoder.",
		ghosts)
}

// (4) THE DECODER NAMES A REAL KEY — and yields the value, not raw bytes.
func TestDecodeStoreNamesARealKey(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	key := append(append([]byte{}, types.MinerKey.Bytes()...), []byte("miner-1")...)
	raw, err := f.keeper.Miner.ValueCodec().Encode(types.Miner{MinerId: "miner-1", Stake: 4242})
	require.NoError(t, err)

	out := dec(kv.Pair{Key: key, Value: raw}, kv.Pair{Key: key})
	require.Contains(t, out, "miner-1", "the decoder must render the KEY readable, not just its hex")
	require.Contains(t, out, "4242",
		"⛔ the decoder did not decode the VALUE: it therefore falls back to raw bytes, i.e. to the "+
			"previous behaviour. Output:\n%s", out)
	require.Contains(t, out, "(absent)",
		"an empty value (a key present on one side only) must be reported as ABSENT, not rendered as a "+
			"legitimate zero value — that is the whole difference between \"not there\" and \"set to "+
			"zero\". Output:\n%s", out)
	require.NotContains(t, out, "OUTSIDE THE SCHEMA")
}

// (5) THE DECODER REFUSES TO ATTRIBUTE A FOREIGN KEY — the failure raw hex could not express.
func TestDecodeStoreFlagsAKeyOutsideSchema(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	out := dec(kv.Pair{Key: []byte("zzz_written_by_hand/"), Value: []byte{0x01}}, kv.Pair{})
	require.Contains(t, out, "OUTSIDE THE SCHEMA",
		"⛔ the decoder attached an unknown key to a collection: a WRONG name is worse than honest hex, "+
			"because it sends the reader looking in the wrong place. Output:\n%s", out)
}

// (6) A CORRUPT VALUE IS REPORTED AS UNDECODABLE — not rendered as a legitimate value.
func TestDecodeStoreCorruptValueIsReported(t *testing.T) {
	f := initFixture(t)
	dec := simutil.NewDecodeStore(f.keeper.Schema)

	key := append(append([]byte{}, types.MinerKey.Bytes()...), []byte("miner-1")...)
	out := dec(kv.Pair{Key: key, Value: []byte{0xff, 0xff, 0xff, 0xff}}, kv.Pair{Key: key})
	require.Contains(t, out, "undecodable",
		"an unreadable value must be REPORTED as unreadable: silently rendering it as a zero would send "+
			"the reader hunting for a content divergence where there is corruption. Output:\n%s", out)
}

// (7) THE DECODER DOES NOT PANIC WHEN A CODEC PANICS.
//
// ⛔ An earlier form of this test fed four `0xff` bytes to the `Miner` codec and required "does not
// panic". Removing the `recover` left that test GREEN, because the proto codec does not panic on those
// bytes — it returns an error. The test measured the nominal path while believing it measured the
// panic path: a check that cannot go red verifies nothing.
//
// The form that carries weight: a THROWAWAY schema whose value codec really does panic. The `recover`
// in `renderValue` is then on the path, and removing it turns this test red. What it protects is not
// theoretical: this decoder only runs on the failure path of a round-trip, i.e. precisely when the
// state is abnormal. An instrument that breaks when the measurement is abnormal only measures the
// normal case.
func TestDecodeStoreSurvivesAPanickingCodec(t *testing.T) {
	sb := collections.NewSchemaBuilder(runtime.NewKVStoreService(storetypes.NewKVStoreKey("piege")))
	_ = collections.NewMap(sb, collections.NewPrefix("piege/value/"), "piege",
		collections.StringKey, colcodec.ValueCodec[string](panickingCodec{}))
	sch, err := sb.Build()
	require.NoError(t, err)

	dec := simutil.NewDecodeStore(sch)
	var out string
	require.NotPanics(t, func() {
		out = dec(kv.Pair{Key: []byte("piege/value/k"), Value: []byte{0x01}}, kv.Pair{})
	}, "⛔ the decoder propagates the codec's panic: the round-trip diagnostic is replaced by a stack "+
		"trace, exactly when the diagnostic is needed")
	require.Contains(t, out, "panic while decoding", "the panic must be REPORTED, not swallowed: output:\n%s", out)
	require.Contains(t, out, "piege", "the collection name must survive its codec's panic")
}

// panickingCodec — a value codec whose `Decode` panics. It exists ONLY as live fault injection for
// (7): without it, the decoder's `recover` would be code nothing exercises.
type panickingCodec struct{}

func (panickingCodec) Encode(v string) ([]byte, error)   { return []byte(v), nil }
func (panickingCodec) Decode([]byte) (string, error)     { panic("value codec panicking") }
func (panickingCodec) EncodeJSON(string) ([]byte, error) { return []byte(`""`), nil }
func (panickingCodec) DecodeJSON([]byte) (string, error) { return "", nil }
func (panickingCodec) Stringify(v string) string         { return v }
func (panickingCodec) ValueType() string                 { return "panickingCodec" }
