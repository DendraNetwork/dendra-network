// Package simutil provides the SIMULATION tooling shared by the three modules of the chain.
//
// WHY THIS PACKAGE EXISTS, AND WHY IT IS NOT IN `x/jobs/simulation`.
//
// `TestAppImportExport` compares the store of the original app with the store of the re-imported one,
// then prints the diverging pairs through `simtestutil.GetSimulationLog`. With no registered decoder,
// that function falls back to `store A %X => %X`: the key IS printed, but as raw hex — that is,
// `6d696e65725f726567697374657265645f6865696768742f76616c75652f...` where one needs to read
// `miner_registered_height`. The information is not absent, it is UNREADABLE, and diagnosing a
// divergence then requires instrumenting the test by hand.
//
// The decoder is DERIVED FROM THE SCHEMA, not from a table written alongside it. That is the only form
// that does not go stale: a `prefix -> name -> value type` table would restate what the keeper already
// declares, hence constitute a SECOND SOURCE OF TRUTH — and a second source drifts the day a collection
// is added without thinking about it, i.e. precisely the day the decoder is needed.
// `collections.Schema` already knows every prefix, every name and every value codec; this only reads
// them. Adding a collection is enough to make it readable here, with nothing else to write.
//
// The same code serves the three modules: duplicating it three times would recreate the drift just
// avoided, and putting it in `x/jobs/simulation` would force `x/emission` to import `x/jobs` in order
// to print its own pools. Hence this neutral package, a sibling of the modules.
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

// NewDecodeStore returns a module's store decoder, built from ITS collections schema.
//
// Sorting by DECREASING prefix length lets the most specific prefix win.
//
// That sort is inert today, deliberately. `collections.SchemaBuilder.Build()` already REFUSES two
// prefixes where one prefixes the other ("schema has overlapping prefixes"): an overlapping schema does
// not build, so the node does not start. The sort exists for the case where this decoder is one day
// applied to a schema that did not go through that check — a legacy store, a schema hand-built in a
// test — so that the failure message stays as close to correct as possible instead of attributing a key
// to the first prefix encountered.
func NewDecodeStore(schema collections.Schema) func(kvA, kvB kv.Pair) string {
	colls := schema.ListCollections()
	sort.SliceStable(colls, func(i, j int) bool {
		return len(colls[i].GetPrefix()) > len(colls[j].GetPrefix())
	})

	return func(kvA, kvB kv.Pair) string {
		// Reference key: `DiffKVStores` aligns both sides on the SAME key and empties the VALUE of the
		// missing side — but one side can arrive with a nil key depending on the fill path. Take the one
		// that exists rather than reporting "outside the schema" for a key that was simply not read.
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
				c.GetName(), rendreCle(ref[len(p):]),
				rendreValeur(vc, kvA.Value), rendreValeur(vc, kvB.Value))
		}

		// THIS CASE IS THE ONE RAW HEX COULD NOT STATE, and it is the most serious. A key NO collection
		// claims is not a display defect: it was written by direct store access (outside `collections`),
		// or a collection changed prefix without a migration — in both cases the state carries a key the
		// keeper can no longer read back, hence one the export will not see.
		return fmt.Sprintf("KEY OUTSIDE THE SCHEMA (no collection claims this prefix)%s\n"+
			"    A: %X\n    B: %X\n"+
			"    -> direct store write, or a collection prefix changed without a migration.\n",
			rendreCle(ref), kvA.Value, kvB.Value)
	}
}

// rendreCle prints the key remainder (after the prefix) AS TEXT AND AS HEX, never one without the
// other: composite keys (`collections.Pair[int64, string]`) mix non-printable height bytes with a
// readable identifier. Printing only the text would lose the height; printing only the hex would
// reproduce the very defect being fixed.
func rendreCle(reste []byte) string {
	if len(reste) == 0 {
		return " (Item: no key)"
	}
	txt := strings.Map(func(r rune) rune {
		if r >= 0x20 && r < 0x7f {
			return r
		}
		return '·'
	}, string(reste))
	return fmt.Sprintf("[%s | hex %X]", txt, reste)
}

// rendreValeur decodes a value with the codec OF ITS collection.
//
// The `recover` is not superstition. This code runs ONLY on the failure path of a test: if decoding
// panics on a corrupted value — and that is exactly the kind of value that fails a round-trip — the
// panic would replace the diagnosis with a stack trace, and the check would lose the one thing asked of
// it. An instrument that breaks when the measurement is abnormal only measures the normal.
func rendreValeur(vc colcodec.UntypedValueCodec, brut []byte) (rendu string) {
	if len(brut) == 0 {
		return "(absent)"
	}
	defer func() {
		if r := recover(); r != nil {
			rendu = fmt.Sprintf("panic while decoding (%v) - raw %X", r, brut)
		}
	}()
	v, err := vc.Decode(brut)
	if err != nil {
		return fmt.Sprintf("undecodable (%v) - raw %X", err, brut)
	}
	if s, err := vc.Stringify(v); err == nil {
		return s
	}
	return fmt.Sprintf("%v", v)
}
