package app

import (
	"encoding/json"
	"math/rand"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/cosmos/cosmos-sdk/codec"
	codectypes "github.com/cosmos/cosmos-sdk/codec/types"
	"github.com/cosmos/cosmos-sdk/types/module"
	simtypes "github.com/cosmos/cosmos-sdk/types/simulation"
	"github.com/stretchr/testify/require"

	emissionmodule "github.com/DendraNetwork/dendra-network/chain/x/emission/module"
	jobsmodule "github.com/DendraNetwork/dendra-network/chain/x/jobs/module"
	modelregistrymodule "github.com/DendraNetwork/dendra-network/chain/x/modelregistry/module"
)

// The genesis fixture must not be able to lie.
//
// This file exists for one failure: `TestAppStateDeterminism` goes red because two runs of the same
// seed produce two app hashes. For an L1 that is the worst diagnosis (two validators replaying the
// same sequence do not reach the same state), and the easiest to misread, because the culprit can be
// the fixture instead of the chain: `GenerateGenesisState` filling `Creator` fields with
// `sample.AccAddress()` -> `ed25519.GenPrivKey()` -> `crypto/rand` draws outside the seed, and the
// test then blames the chain for a defect in its own harness. Isolating `WeightedOperations` does not
// clear the module either; that is only the other half.
//
// Two guards of different kinds, both needed:
//
//	(A) The property (`TestFixtureGenesisSameSeedSameGenesis`): same seed, same bytes. It knows no
//	    list of forbidden functions and measures what matters, so it also catches sources of
//	    entropy nobody has thought of yet. It is the judge.
//
//	(B) The gate (`TestFixtureGenesisImportsUnderAllowList` and `...NoEntropyOutsideSeed`): an
//	    allow-list of imports. A deny-list protects only against what it names, while an
//	    allow-list goes red on anything it does not know. It adds a second check to (A) that runs
//	    upstream, with a message that names the trap so the next reader understands it in seconds.
//
// What neither covers: (A) proves the determinism of the genesis generators only, not of
// `BeginBlock`/`EndBlock`, which remain the job of `TestAppStateDeterminism`, slower and less
// talkative. The two complement each other.

// generatorModules lists the three in-house modules, with a flag telling whether their genesis depends
// on the drawn accounts. Without that column the "two seeds, two genesis" witness would be a false red
// for `emission` and `modelregistry`, whose genesis is only `DefaultParams()` and is constant by
// construction. A false red disarms a guard as surely as a false green empties it.
var generatorModules = []struct {
	name              string
	generate          func(*module.SimulationState)
	dependsOnAccounts bool
}{
	{"jobs", func(s *module.SimulationState) { var m jobsmodule.AppModule; m.GenerateGenesisState(s) }, true},
	{"emission", func(s *module.SimulationState) { var m emissionmodule.AppModule; m.GenerateGenesisState(s) }, false},
	{"modelregistry", func(s *module.SimulationState) { var m modelregistrymodule.AppModule; m.GenerateGenesisState(s) }, false},
}

// genesisForSeed replays what the simulation harness does: accounts derived from the seed, a random
// source derived from the seed, and nothing else.
func genesisForSeed(t *testing.T, seed int64) map[string]string {
	t.Helper()
	r := rand.New(rand.NewSource(seed))
	simState := &module.SimulationState{
		AppParams: make(simtypes.AppParams),
		Cdc:       codec.NewProtoCodec(codectypes.NewInterfaceRegistry()),
		Rand:      r,
		GenState:  make(map[string]json.RawMessage),
		Accounts:  simtypes.RandomAccounts(r, 12),
	}
	for _, m := range generatorModules {
		m.generate(simState)
	}
	out := map[string]string{}
	for _, m := range generatorModules {
		raw, ok := simState.GenState[m.name]
		require.True(t, ok,
			"module %s wrote nothing into GenState: the generator no longer generates anything, and a "+
				"silent generator is trivially \"deterministic\"", m.name)
		require.NotEmpty(t, raw, "empty genesis for %s", m.name)
		out[m.name] = string(raw)
	}
	return out
}

// (A) Same seed, same bytes, repeated.
//
// Eight repetitions rather than two: an unseeded source can return the same value twice by chance (a
// boolean, an index into a short list). Repeating cannot create a false red, since a correct generator
// returns the same bytes however often it is called, but it lowers the probability of a false green.
func TestFixtureGenesisSameSeedSameGenesis(t *testing.T) {
	const repetitions = 8
	reference := genesisForSeed(t, 42)

	for i := 2; i <= repetitions; i++ {
		next := genesisForSeed(t, 42)
		for _, m := range generatorModules {
			require.Equal(t, reference[m.name], next[m.name],
				"`%s.GenerateGenesisState` returns two different genesis states for the same seed "+
					"(run %d/%d).\n"+
					"This is the defect that reads as \"the chain is non-deterministic\" while the fault "+
					"is in the fixture: a generator drawing from `crypto/rand` through "+
					"`sample.AccAddress()`. Everything a genesis generator writes must derive from "+
					"`simState.Rand` or `simState.Accounts` — never from a global source, nor from the "+
					"clock, nor from a process counter.", m.name, i, repetitions)
		}
	}
}

// (B) Witness: check (A) must be able to go red.
//
// A generator that ignored `simState` entirely (for instance emitting only default parameters instead of
// the accounts) would be perfectly deterministic and useless: (A) would stay green on a hollow
// fixture. Hence the requirement that the genesis of the only module that depends on the accounts
// changes when the accounts change.
func TestFixtureGenesisWitnessTwoSeedsTwoGenesis(t *testing.T) {
	a, b := genesisForSeed(t, 42), genesisForSeed(t, 4242)
	for _, m := range generatorModules {
		if !m.dependsOnAccounts {
			require.Equal(t, a[m.name], b[m.name],
				"the genesis of `%s` depends only on DefaultParams(): if it varies with the seed it has "+
					"acquired a source of entropy, and the `dependsOnAccounts` column of this file has "+
					"become wrong", m.name)
			continue
		}
		require.NotEqual(t, a[m.name], b[m.name],
			"dead witness: `%s` returns the same genesis for two different seeds although it is "+
				"supposed to place drawn accounts. Either the generator no longer uses "+
				"`simState.Accounts` (the fixture is hollow and test (A) guards nothing), or it has "+
				"been emptied.", m.name)
	}
}

// (B) The gate: what the generators are allowed to import.

// generatorSources returns the three `x/*/module/simulation.go` files, read from `app/`.
func generatorSources(t *testing.T) map[string]string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join("..", "x", "*", "module", "simulation.go"))
	require.NoError(t, err)
	require.Len(t, files, len(generatorModules),
		"%d simulation files found for %d modules: either a module lost its own, or a new module is "+
			"not covered by this guard. Either way this check no longer says what it claims to say.",
		len(files), len(generatorModules))
	out := map[string]string{}
	for _, f := range files {
		b, e := os.ReadFile(f)
		require.NoError(t, e)
		out[f] = string(b)
	}
	return out
}

// allowedImports is the allow-list, with the rationale of each entry.
//
// The direction of the list matters. A deny-list ("forbidden: crypto/rand, uuid, time...") protects
// only against what could be named the day it was written; this one goes red on any new package and
// forces an explicit decision. The price is an occasional red on a harmless import, which is the cost
// of failing closed, and it is small: add a line with its rationale.
var allowedImports = map[string]string{
	"math/rand": "type `*rand.Rand` of the harness callbacks; the seeded source is `simState.Rand`",
	"github.com/cosmos/cosmos-sdk/types/module":                           "type `SimulationState`",
	"github.com/cosmos/cosmos-sdk/types/simulation":                       "harness types (WeightedOperation, StoreDecoderRegistry)",
	"github.com/cosmos/cosmos-sdk/x/simulation":                           "weighted-operation helpers",
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/simulation":     "simulation operations of the jobs module",
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types":          "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types":      "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types": "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/simutil":             "store decoder derived from the schema",
}

func TestFixtureGenesisImportsUnderAllowList(t *testing.T) {
	re := regexp.MustCompile(`(?m)^\s*(?:[\w.]+\s+)?"([^"]+)"\s*$`)
	unknown := []string{}
	for name, src := range generatorSources(t) {
		block := between(src, "\nimport (", "\n)")
		require.NotEmpty(t, block, "import block not found in %s: the guard reads nothing", name)
		for _, m := range re.FindAllStringSubmatch(block, -1) {
			if _, ok := allowedImports[m[1]]; !ok {
				unknown = append(unknown, name+" : "+m[1])
			}
		}
	}
	sort.Strings(unknown)
	require.Empty(t, unknown,
		"undeclared import(s) in a genesis generator: %v.\n"+
			"Each import here needs a decision. A genesis generator must draw only "+
			"from `simState`: `crypto/rand`, `time`, a UUID generator or a utility package that hides "+
			"one produce a different genesis on every run, hence a different app-hash, hence a red "+
			"`TestAppStateDeterminism` that will accuse the chain. If the import is harmless, add it "+
			"to `allowedImports` with its rationale.", unknown)
}

// forbiddenGenesisEntropy is a named deny-list of off-seed entropy in the generator body, as a second
// layer.
//
// It does not claim to be exhaustive (the import allow-list and the property test cover that); it
// exists to name the known traps in the error message. `sample.AccAddress` belongs here because it is
// the typical trap: an innocuous name, "a sample address", that hides a call to `crypto/rand`.
var forbiddenGenesisEntropy = map[string]string{
	"sample.AccAddress": "calls `ed25519.GenPrivKey()` -> `crypto/rand`: off-seed. Use `simState.Accounts`",
	"GenPrivKey":        "generates a key from `crypto/rand`, outside the seed",
	"crypto/rand":       "system entropy, outside the seed",
	"time.Now":          "the clock is not seeded; use `simState.GenTimestamp`",
	"uuid.":             "identifier drawn outside the seed",
}

// reRandGlobal matches any package-level `rand.Xxx` call in the generator body, except the type
// `rand.Rand`.
//
// A named list (`rand.Int`, `rand.Read`, `rand.Perm`...) let `rand.Float64`, `rand.Shuffle`,
// `rand.Uint64`, `rand.NewSource` and whatever the library adds next go through. The seeded source is
// written `simState.Rand.Xxx` (a capital preceded by a dot) and is not matched; `*rand.Rand` is a
// type, not a draw, and stays allowed by name. Since Go 1.20 the global `math/rand` source is seeded
// randomly at process start, so it is as off-seed as `crypto/rand`.
var reRandGlobal = regexp.MustCompile(`(^|[^.\w])rand\.([A-Za-z_]\w*)`)

func TestFixtureGenesisNoEntropyOutsideSeed(t *testing.T) {
	seen := 0
	for name, src := range generatorSources(t) {
		body := generatorBody(src)
		require.NotEmpty(t, body,
			"body of `GenerateGenesisState` not found in %s (signature changed?). This guard reads the "+
				"source: if it can no longer find the function it guards nothing, so it goes red "+
				"instead of staying silent.", name)
		seen++
		bare := withoutGoComments(body)
		for _, l := range strings.Split(bare, "\n") {
			for pattern, why := range forbiddenGenesisEntropy {
				require.NotContains(t, l, pattern,
					"%s: `%s` in the body of `GenerateGenesisState`.\n%s\n"+
						"Consequence: same seed => different genesis => different app-hash, and "+
						"`TestAppStateDeterminism` will accuse the chain of a defect that lives here.",
					name, pattern, why)
			}
		}
		for _, m := range reRandGlobal.FindAllStringSubmatch(bare, -1) {
			require.Equal(t, "Rand", m[2],
				"%s: `rand.%s` in the body of `GenerateGenesisState` — that is the global math/rand "+
					"source, seeded randomly at process start, hence outside the simulation seed. The "+
					"seeded source is `simState.Rand`.", name, m[2])
		}
	}
	require.Equal(t, len(generatorModules), seen, "not every generator was read")
}

// generatorBody extracts the body of `GenerateGenesisState`, accepting a receiver that is either named
// (`func (am AppModule)`) or anonymous (`func (AppModule)`).
//
// Matching the literal `func (AppModule) GenerateGenesisState(` would turn the guard red as soon as a
// linter added a receiver name, with no entropy introduced. A false red disarms a guard as surely as a
// false green empties it, so the fail-closed behaviour (function not found means red) is kept and only
// the meaningless cause of red is removed.
var reGeneratorSignature = regexp.MustCompile(`func \((?:\w+ )?AppModule\) GenerateGenesisState\(`)

func generatorBody(src string) string {
	loc := reGeneratorSignature.FindStringIndex(src)
	if loc == nil {
		return ""
	}
	rest := src[loc[1]:]
	j := strings.Index(rest, "\n}")
	if j < 0 {
		return ""
	}
	return rest[:j]
}

// The extractor itself is tested, because its failure branch cannot be exercised any other way.
//
// "Function not found means red" cannot be shown by sabotage on the real repository: removing
// `GenerateGenesisState` from a module no longer compiles (the `AppModuleSimulation` interface
// requires it), so the compiler fails before the test does. The fail-closed branch still matters for
// a change of shape (a body rewritten without a detectable `\n}`), and that is the case exercised
// here, directly on the extraction function.
func TestGeneratorBodyFailsClosedOnFailedExtraction(t *testing.T) {
	require.Empty(t, generatorBody("package x\n\nfunc otherThing() {\n}\n"),
		"a failed extraction must return empty: returning the whole file would make the guard scan the "+
			"wrong text and go green for the wrong reasons")
	require.Empty(t, generatorBody("func (AppModule) GenerateGenesisState(s *T) { x := 1 }"),
		"single-line body: no `\\n}` -> extraction fails -> empty, hence red at the caller")
	require.NotEmpty(t, generatorBody("func (AppModule) GenerateGenesisState(s *T) {\n\tx := 1\n}\n"))
	require.NotEmpty(t, generatorBody("func (am AppModule) GenerateGenesisState(s *T) {\n\tx := 1\n}\n"))
}

// between returns what follows `start` up to the first `end`, or "" if either is missing. Returning
// "" rather than the whole file is deliberate: callers require a non-empty result, so a failed
// extraction fails loudly instead of scanning the wrong text.
func between(src, start, end string) string {
	i := strings.Index(src, start)
	if i < 0 {
		return ""
	}
	rest := src[i+len(start):]
	j := strings.Index(rest, end)
	if j < 0 {
		return ""
	}
	return rest[:j]
}

// withoutGoComments strips `//...` comments: the comments of those files describe the trap and quote
// `sample.AccAddress()` by name, and only the code must stop carrying it.
func withoutGoComments(src string) string {
	var b strings.Builder
	for _, l := range strings.Split(src, "\n") {
		if i := strings.Index(l, "//"); i >= 0 {
			l = l[:i]
		}
		b.WriteString(l)
		b.WriteString("\n")
	}
	return b.String()
}
