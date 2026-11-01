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

// Ghost prefixes: what this file checks, and what it does not.
//
// It used to carry a disjointness check over prefixes ("no prefix is a prefix of another"). That
// check could not fail on its own: making two prefixes overlap did turn the test red, but the red
// came from a `panic` inside `collections.SchemaBuilder.Build()`, which already refuses that case
// (`cosmossdk.io/collections/schema.go`: "schema has overlapping prefixes"). The property is
// guaranteed upstream, at node start, so a test that re-checks it can only fail through another
// component's panic. What remains of it is a contract test on the dependency (see each module's
// tests), which turns red if a future version stops enforcing it.
//
// This file checks what `collections` cannot see because it lives outside the schema: a variable
// `XxxKey = collections.NewPrefix("...")` declared in `types` but never handed to the SchemaBuilder.
// Such a prefix is reachable only through direct store access: the state it carries is not exported
// at genesis, not compared by the round trip, and not readable by the decoder. That is the class
// `NewDecodeStore` reports as a key outside the schema, so it is better to forbid it at declaration.

var reDeclaredPrefix = regexp.MustCompile(`(\w+)\s*=\s*collections\.NewPrefix\("([^"]*)"\)`)

// DeclaredPrefixes reads the `collections.NewPrefix("...")` declarations of a directory's non-test
// sources and returns name -> prefix. An error (unreadable directory) must make the caller fail and
// never pass for "no prefix declared": zero declarations and a missing directory look alike.
func DeclaredPrefixes(dir string) (map[string]string, error) {
	files, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, fmt.Errorf("no .go source in %s: the path is wrong, not the module empty", dir)
	}
	out := map[string]string{}
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, e := os.ReadFile(f)
		if e != nil {
			return nil, e
		}
		for _, m := range reDeclaredPrefix.FindAllStringSubmatch(string(b), -1) {
			out[m[1]] = m[2]
		}
	}
	return out, nil
}

// PhantomPrefixes returns, sorted, the `name = prefix` entries of `declared` that no collection in
// the schema claims. Empty means the property holds.
func PhantomPrefixes(schema collections.Schema, declared map[string]string) []string {
	registered := map[string]bool{}
	for _, c := range schema.ListCollections() {
		registered[string(c.GetPrefix())] = true
	}
	phantoms := []string{}
	for name, p := range declared {
		if !registered[p] {
			phantoms = append(phantoms, name+" = "+p)
		}
	}
	sort.Strings(phantoms)
	return phantoms
}
