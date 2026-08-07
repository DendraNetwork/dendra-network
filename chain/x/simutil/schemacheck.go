package simutil

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"cosmossdk.io/collections"
)

// ═══ GHOST PREFIXES ══════════════════════════════════════════════════════════════════════════════
//
// WHAT THIS FILE CHECKS, AND WHAT IT DELIBERATELY DOES NOT.
//
// It used to carry a DISJOINTNESS check over prefixes ("no prefix is a prefix of another"). That
// check was decorative, and the sabotage that appeared to validate it was misleading: making two
// prefixes overlap did turn the test red, but the red came from a `panic` inside
// `collections.SchemaBuilder.Build()`, which already refuses that case
// (`cosmossdk.io/collections/schema.go`: "schema has overlapping prefixes"). The property is
// therefore guaranteed UPSTREAM, at node start-up, and a test that re-checks it can only fail
// through somebody else's panic. Reading the COLOUR of a sabotage instead of its MESSAGE proves
// nothing. What survives is a CONTRACT test on the dependency (see each module's tests), which turns
// red if a future version stops enforcing it.
//
// What remains is what `collections` cannot see, because it lives outside the schema: a variable
// `XxxKey = collections.NewPrefix("…")` DECLARED in `types` but never handed to the SchemaBuilder.
// Such a prefix is reachable only through DIRECT store access: the state it carries is not exported
// at genesis, not compared by the round-trip, and not readable by the decoder. That is exactly the
// class `NewDecodeStore` reports as "KEY OUTSIDE THE SCHEMA" — better to forbid it at declaration.

var rePrefixeDeclare = regexp.MustCompile(`(\w+)\s*=\s*collections\.NewPrefix\("([^"]*)"\)`)

// PrefixesDeclares reads the `collections.NewPrefix("…")` declarations of a directory's non-test
// sources and returns name -> prefix. An error (unreadable directory) must make the caller FAIL, never
// pass for "no prefix declared": zero declarations and a missing directory look alike.
func PrefixesDeclares(dir string) (map[string]string, error) {
	fichiers, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	if len(fichiers) == 0 {
		return nil, fmt.Errorf("no .go source in %s: the path is wrong, not the module empty", dir)
	}
	out := map[string]string{}
	for _, f := range fichiers {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, e := os.ReadFile(f)
		if e != nil {
			return nil, e
		}
		for _, m := range rePrefixeDeclare.FindAllStringSubmatch(string(b), -1) {
			out[m[1]] = m[2]
		}
	}
	return out, nil
}

// PhantomPrefixes returns, sorted, the `name = prefix` entries of `declares` that no collection in
// the schema claims. Empty means the property holds.
func PhantomPrefixes(schema collections.Schema, declares map[string]string) []string {
	registered := map[string]bool{}
	for _, c := range schema.ListCollections() {
		registered[string(c.GetPrefix())] = true
	}
	phantoms := []string{}
	for name, p := range declares {
		if !registered[p] {
			phantoms = append(phantoms, name+" = "+p)
		}
	}
	sort.Strings(phantoms)
	return phantoms
}
