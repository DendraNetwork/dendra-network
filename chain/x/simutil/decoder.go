// Package simutil provides the simulation tooling shared by the three modules of the chain.
//
// Why this package exists, and why it is not in `x/jobs/simulation`.
//
// `TestAppImportExport` compares the store of the original app with the store of the re-imported one,
// then prints the diverging pairs through `simtestutil.GetSimulationLog`. With no registered decoder,
// that function falls back to `store A %X => %X`: the key is printed, but as raw hex, that is
// `6d696e65725f726567697374657265645f6865696768742f76616c75652f...` where one needs to read
// `miner_registered_height`. Diagnosing a divergence then requires instrumenting the test by hand.
//
// The decoder is derived from the schema. A `prefix -> name -> value type` table written alongside it
// would restate what the keeper already declares, a second source of truth that drifts the day a
// collection is added without thinking about it, which is the day the decoder is needed.
// `collections.Schema` already knows every prefix, every name and every value codec, and this code
// only reads them: adding a collection is enough to make it readable here.
//
// The same code serves the three modules: duplicating it would recreate that drift, and putting it in
// `x/jobs/simulation` would force `x/emission` to import `x/jobs` to print its own pools. Hence this
// neutral package, next to the modules.
package simutil

import (
	"bytes"
	"fmt"
	"sort"
	"strings"

	"cosmossdk.io/collections"
	colcodec "cosmossdk.io/collections/codec"

	"github.com/cosmos/cosmos-sdk/types/kv"
)

// NewDecodeStore returns a module's store decoder, built from its collections schema.
//
// Sorting by decreasing prefix length lets the most specific prefix win.
//
// The sort has no effect today, on purpose. `collections.SchemaBuilder.Build()` already refuses two
// prefixes where one prefixes the other ("schema has overlapping prefixes"): an overlapping schema
// does not build, so the node does not start. The sort is for a schema that did not go through that
// check (a legacy store, a schema built by hand in a test), so that the failure message stays as
// close to correct as possible instead of attributing a key to the first prefix encountered.
func NewDecodeStore(schema collections.Schema) func(kvA, kvB kv.Pair) string {
	colls := schema.ListCollections()
	sort.SliceStable(colls, func(i, j int) bool {
		return len(colls[i].GetPrefix()) > len(colls[j].GetPrefix())
	})

	return func(kvA, kvB kv.Pair) string {
		// Reference key: `DiffKVStores` aligns both sides on the same key and empties the value of the
		// missing side, but one side can arrive with a nil key depending on the fill path. Take the one
		// that exists rather than reporting "outside the schema" for a key that was not read.
		ref := kvA.Key
		if len(ref) == 0 {
			ref = kvB.Key
		}

		for _, c := range colls {
			p := c.GetPrefix()
			if !bytes.HasPrefix(ref, p) {
				continue
			}
			vc := c.ValueCodec()
			return fmt.Sprintf("%s%s\n    A: %s\n    B: %s\n",
				c.GetName(), renderKey(ref[len(p):]),
				renderValue(vc, kvA.Value), renderValue(vc, kvB.Value))
		}

		// Raw hex could not state this case, and it is the most serious. A key no collection claims is not
		// a display defect: it was written by direct store access (outside `collections`), or a collection
		// changed prefix without a migration. Either way the state carries a key the keeper can no longer
		// read back, so the export will not see it.
		return fmt.Sprintf("key outside the schema (no collection claims this prefix)%s\n"+
			"    A: %X\n    B: %X\n"+
			"    -> direct store write, or a collection prefix changed without a migration.\n",
			renderKey(ref), kvA.Value, kvB.Value)
	}
}

// renderKey prints the key remainder (after the prefix) both as text and as hex: composite keys
// (`collections.Pair[int64, string]`) mix non-printable height bytes with a readable identifier.
// Printing only the text would lose the height, and printing only the hex would bring back the
// problem this package fixes.
func renderKey(rest []byte) string {
	if len(rest) == 0 {
		return " (Item: no key)"
	}
	txt := strings.Map(func(r rune) rune {
		if r >= 0x20 && r < 0x7f {
			return r
		}
		return '·'
	}, string(rest))
	return fmt.Sprintf("[%s | hex %X]", txt, rest)
}

// renderValue decodes a value with the codec of its collection.
//
// The `recover` is needed because this code runs only on the failure path of a test: if decoding
// panics on a corrupted value, which is the kind of value that fails a round trip, the panic would
// replace the diagnosis with a stack trace. An instrument that breaks when the measurement is
// abnormal only measures the normal.
func renderValue(vc colcodec.UntypedValueCodec, raw []byte) (rendered string) {
	if len(raw) == 0 {
		return "(absent)"
	}
	defer func() {
		if r := recover(); r != nil {
			rendered = fmt.Sprintf("panic while decoding (%v) - raw %X", r, raw)
		}
	}()
	v, err := vc.Decode(raw)
	if err != nil {
		return fmt.Sprintf("undecodable (%v) - raw %X", err, raw)
	}
	if s, err := vc.Stringify(v); err == nil {
		return s
	}
	return fmt.Sprintf("%v", v)
}
