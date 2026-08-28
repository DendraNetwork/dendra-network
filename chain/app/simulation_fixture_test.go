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

// ═══ THE GENESIS FIXTURE MUST NOT BE ABLE TO LIE ═════════════════════════════════════════════════
//
// The failure mode this file exists for: `TestAppStateDeterminism` goes red because two runs of the
// SAME seed produce two app-hashes. For an L1 that is the worst possible diagnosis — two validators
// replaying the same sequence do not reach the same state. It is also the easiest one to misread,
// because the culprit can be the FIXTURE rather than the chain: `GenerateGenesisState` filling
// `Creator` fields with `sample.AccAddress()` -> `ed25519.GenPrivKey()` -> `crypto/rand` draws
// outside the seed, and the test then accuses the chain of a defect that lives in its own harness.
// Isolating `WeightedOperations` does not clear the module either: that is only the other half.
//
// Two guards of DIFFERENT NATURES, and both are needed:
//
//	(A) THE PROPERTY (`TestFixtureGenesisSameSeedSameGenesis`). Same seed => same bytes. It knows no
//	    list of forbidden functions: it measures what matters, so it also catches sources of entropy
//	    nobody has thought of yet. It is the judge.
//
//	(B) THE GATE (`TestFixtureGenesisImportsSousAllowList` + `...AucuneEntropieHorsSeed`). An
//	    ALLOW-LIST of imports, not a deny-list: a guard written against a list protects only that
//	    list, whereas an allow-list goes red on what it does not know. It does not replace (A) — it
//	    doubles it upstream, with a message that NAMES the trap so the next reader understands in
//	    seconds rather than in an hour.
//
// What neither covers: (A) proves the determinism of the genesis GENERATORS only — not of
// `BeginBlock`/`EndBlock`, which remain the domain of `TestAppStateDeterminism`, slower and less
// talkative. The two complement each other; neither makes the other obsolete.

// modulesGenerateurs — the three in-house modules, with a flag telling whether their genesis DEPENDS
// on the drawn accounts. That column is not decorative: without it, the "two seeds => two genesis"
// witness would be a FALSE RED for `emission` and `modelregistry`, whose genesis is nothing but
// `DefaultParams()` and is therefore constant BY CONSTRUCTION. A false red disarms a guard as surely
// as a false green empties it.
var modulesGenerateurs = []struct {
	nom              string
	genere           func(*module.SimulationState)
	dependDesComptes bool
}{
	{"jobs", func(s *module.SimulationState) { var m jobsmodule.AppModule; m.GenerateGenesisState(s) }, true},
	{"emission", func(s *module.SimulationState) { var m emissionmodule.AppModule; m.GenerateGenesisState(s) }, false},
	{"modelregistry", func(s *module.SimulationState) { var m modelregistrymodule.AppModule; m.GenerateGenesisState(s) }, false},
}

// genesisPourSeed replays exactly what the simulation harness does: accounts DERIVED from the seed, a
// random source derived from the seed, and nothing else.
func genesisPourSeed(t *testing.T, seed int64) map[string]string {
	t.Helper()
	r := rand.New(rand.NewSource(seed))
	simState := &module.SimulationState{
		AppParams: make(simtypes.AppParams),
		Cdc:       codec.NewProtoCodec(codectypes.NewInterfaceRegistry()),
		Rand:      r,
		GenState:  make(map[string]json.RawMessage),
		Accounts:  simtypes.RandomAccounts(r, 12),
	}
	for _, m := range modulesGenerateurs {
		m.genere(simState)
	}
	out := map[string]string{}
	for _, m := range modulesGenerateurs {
		brut, ok := simState.GenState[m.nom]
		require.True(t, ok,
			"module %s wrote nothing into GenState: the generator no longer generates anything, and a "+
				"silent generator is trivially \"deterministic\"", m.nom)
		require.NotEmpty(t, brut, "empty genesis for %s", m.nom)
		out[m.nom] = string(brut)
	}
	return out
}

// (A) SAME SEED => SAME BYTES, REPEATED.
//
// Eight repetitions rather than two: an unseeded source can return the same value twice by chance (a
// boolean, an index into a short list). Repeating creates no false red — a correct generator returns
// the same bytes however often it is called — but it lowers the probability of a false GREEN.
func TestFixtureGenesisSameSeedSameGenesis(t *testing.T) {
	const repetitions = 8
	reference := genesisPourSeed(t, 42)

	for i := 2; i <= repetitions; i++ {
		suivant := genesisPourSeed(t, 42)
		for _, m := range modulesGenerateurs {
			require.Equal(t, reference[m.nom], suivant[m.nom],
				"`%s.GenerateGenesisState` returns TWO different genesis states for the SAME seed "+
					"(run %d/%d).\n"+
					"This is the defect that reads as \"the chain is non-deterministic\" while the fault "+
					"is in the fixture: a generator drawing from `crypto/rand` through "+
					"`sample.AccAddress()`. Everything a genesis generator writes must derive from "+
					"`simState.Rand` or `simState.Accounts` — never from a global source, nor from the "+
					"clock, nor from a process counter.", m.nom, i, repetitions)
		}
	}
}

// (B) WITNESS — check (A) must be able to go red.
//
// A generator that ignored `simState` entirely (for instance emitting only default parameters where
// it used to emit accounts) would be perfectly "deterministic" and perfectly useless: (A) would stay
// green on a hollow fixture. Hence the requirement that the genesis of the only module that DEPENDS
// on the accounts changes when the accounts change.
func TestFixtureGenesisWitnessTwoSeedsTwoGenesis(t *testing.T) {
	a, b := genesisPourSeed(t, 42), genesisPourSeed(t, 4242)
	for _, m := range modulesGenerateurs {
		if !m.dependDesComptes {
			require.Equal(t, a[m.nom], b[m.nom],
				"the genesis of `%s` depends only on DefaultParams(): if it varies with the seed it has "+
					"acquired a source of entropy, and the `dependDesComptes` column of this file has "+
					"become wrong", m.nom)
			continue
		}
		require.NotEqual(t, a[m.nom], b[m.nom],
			"DEAD WITNESS: `%s` returns the SAME genesis for two different seeds although it is "+
				"supposed to place drawn accounts. Either the generator no longer uses "+
				"`simState.Accounts` (the fixture is hollow and test (A) guards nothing), or it has "+
				"been emptied.", m.nom)
	}
}

// ─── (B) THE GATE: WHAT THE GENERATORS ARE ALLOWED TO IMPORT ─────────────────────────────────────

// sourcesGenerateurs — the three `x/*/module/simulation.go` files, read from `app/`.
func sourcesGenerateurs(t *testing.T) map[string]string {
	t.Helper()
	fichiers, err := filepath.Glob(filepath.Join("..", "x", "*", "module", "simulation.go"))
	require.NoError(t, err)
	require.Len(t, fichiers, len(modulesGenerateurs),
		"%d simulation files found for %d modules: either a module lost its own, or a new module is "+
			"not covered by this guard. Either way this check no longer says what it claims to say.",
		len(fichiers), len(modulesGenerateurs))
	out := map[string]string{}
	for _, f := range fichiers {
		b, e := os.ReadFile(f)
		require.NoError(t, e)
		out[f] = string(b)
	}
	return out
}

// importsAutorises — the ALLOW-LIST, with the rationale of each entry.
//
// The direction of the list is the point. A deny-list ("forbidden: crypto/rand, uuid, time...")
// protects only against what could be named the day it was written; this one goes red on ANY new
// package and forces an explicit decision. The price is an occasional red on a perfectly harmless
// import: that is the cost of failing closed, and it is small — add a line with its rationale.
var importsAutorises = map[string]string{
	"math/rand": "type `*rand.Rand` of the harness callbacks; the SEEDED source is `simState.Rand`",
	"github.com/cosmos/cosmos-sdk/types/module":                           "type `SimulationState`",
	"github.com/cosmos/cosmos-sdk/types/simulation":                       "harness types (WeightedOperation, StoreDecoderRegistry)",
	"github.com/cosmos/cosmos-sdk/x/simulation":                           "weighted-operation helpers",
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/simulation":     "simulation operations of the jobs module",
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types":          "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/emission/types":      "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types": "GenesisState / DefaultParams",
	"github.com/DendraNetwork/dendra-network/chain/x/simutil":             "store decoder derived from the schema",
}

func TestFixtureGenesisImportsSousAllowList(t *testing.T) {
	re := regexp.MustCompile(`(?m)^\s*(?:[\w.]+\s+)?"([^"]+)"\s*$`)
	inconnus := []string{}
	for nom, src := range sourcesGenerateurs(t) {
		bloc := entreDeux(src, "\nimport (", "\n)")
		require.NotEmpty(t, bloc, "import block not found in %s: the guard reads nothing", nom)
		for _, m := range re.FindAllStringSubmatch(bloc, -1) {
			if _, ok := importsAutorises[m[1]]; !ok {
				inconnus = append(inconnus, nom+" : "+m[1])
			}
		}
	}
	sort.Strings(inconnus)
	require.Empty(t, inconnus,
		"UNDECLARED import(s) in a genesis generator: %v.\n"+
			"This is not necessarily a fault — it is a DECISION. A genesis generator must draw ONLY "+
			"from `simState`: `crypto/rand`, `time`, a UUID generator or a utility package that hides "+
			"one produce a different genesis on every run, hence a different app-hash, hence a red "+
			"`TestAppStateDeterminism` that will accuse the CHAIN. If the import is harmless, add it "+
			"to `importsAutorises` WITH ITS RATIONALE.", inconnus)
}

// NO OFF-SEED ENTROPY IN THE GENERATOR BODY — the named deny-list, as a second curtain.
//
// It does not claim to be exhaustive (the import allow-list and the property test cover that): it
// exists to NAME the known traps in the error message. `sample.AccAddress` belongs here because it is
// exactly the trap-by-name pattern: a perfectly innocuous name — "a sample address" — hiding a call
// to `crypto/rand`.
var entropieInterditeDansGenesis = map[string]string{
	"sample.AccAddress": "calls `ed25519.GenPrivKey()` -> `crypto/rand`: OFF-SEED. Use `simState.Accounts`",
	"GenPrivKey":        "generates a key from `crypto/rand`, outside the seed",
	"crypto/rand":       "system entropy, outside the seed",
	"time.Now":          "the clock is not seeded; use `simState.GenTimestamp`",
	"uuid.":             "identifier drawn outside the seed",
}

// reRandGlobal — ANY package-level `rand.Xxx` call in the generator body, EXCEPT the type `rand.Rand`.
//
// A named list (`rand.Int`, `rand.Read`, `rand.Perm`...) had exactly the flaw deny-lists are blamed
// for: it let `rand.Float64`, `rand.Shuffle`, `rand.Uint64`, `rand.NewSource` and whatever the
// library adds next go through. The SEEDED source is written `simState.Rand.…` (a capital preceded by
// a dot) and is therefore untouched; `*rand.Rand` is a TYPE, not a draw, and stays allowed by name.
// Since Go 1.20 the global `math/rand` source is seeded RANDOMLY at process start: it is as off-seed
// as `crypto/rand`.
var reRandGlobal = regexp.MustCompile(`(^|[^.\w])rand\.([A-Za-z_]\w*)`)

func TestFixtureGenesisAucuneEntropieHorsSeed(t *testing.T) {
	vus := 0
	for nom, src := range sourcesGenerateurs(t) {
		corps := corpsGenerateur(src)
		require.NotEmpty(t, corps,
			"body of `GenerateGenesisState` not found in %s (signature changed?). This guard reads the "+
				"SOURCE: if it can no longer find the function it guards nothing, so it goes red "+
				"instead of staying silent.", nom)
		vus++
		nu := sansCommentairesGo(corps)
		for _, l := range strings.Split(nu, "\n") {
			for motif, pourquoi := range entropieInterditeDansGenesis {
				require.NotContains(t, l, motif,
					"%s: `%s` in the body of `GenerateGenesisState`.\n%s\n"+
						"Consequence: same seed => different genesis => different app-hash, and "+
						"`TestAppStateDeterminism` will accuse the CHAIN of a defect that lives here.",
					nom, motif, pourquoi)
			}
		}
		for _, m := range reRandGlobal.FindAllStringSubmatch(nu, -1) {
			require.Equal(t, "Rand", m[2],
				"%s: `rand.%s` in the body of `GenerateGenesisState` — that is the GLOBAL math/rand "+
					"source, seeded randomly at process start, hence outside the simulation seed. The "+
					"seeded source is `simState.Rand`.", nom, m[2])
		}
	}
	require.Equal(t, len(modulesGenerateurs), vus, "not every generator was read")
}

// corpsGenerateur extracts the body of `GenerateGenesisState`, TOLERATING a receiver that is either
// named (`func (am AppModule)`) or anonymous (`func (AppModule)`).
//
// That tolerance is not convenience: matching the literal string `func (AppModule)
// GenerateGenesisState(` makes a single `am` added by a linter turn the guard red without any entropy
// having been introduced. A false red disarms a guard as surely as a false green empties it, so the
// fail-closed behaviour (function not found => red) is kept while the only meaningless cause of red
// is removed.
var reSignatureGenerateur = regexp.MustCompile(`func \((?:\w+ )?AppModule\) GenerateGenesisState\(`)

func corpsGenerateur(src string) string {
	loc := reSignatureGenerateur.FindStringIndex(src)
	if loc == nil {
		return ""
	}
	reste := src[loc[1]:]
	j := strings.Index(reste, "\n}")
	if j < 0 {
		return ""
	}
	return reste[:j]
}

// THE EXTRACTOR ITSELF IS TESTED — because its failure branch cannot be sabotaged any other way.
//
// Honest limitation: "function not found => red" cannot be proven by sabotage on the real repository,
// since removing `GenerateGenesisState` from a module no longer compiles (the `AppModuleSimulation`
// interface requires it) — the compiler goes red before the test does. The fail-closed branch remains
// useful for a change of SHAPE (a body rewritten without a detectable `\n}`), and that is the case
// exercised here, directly on the extraction function.
func TestCorpsGenerateurFailCloseSurExtractionRatee(t *testing.T) {
	require.Empty(t, corpsGenerateur("package x\n\nfunc autreChose() {\n}\n"),
		"a failed extraction must return EMPTY: returning the whole file would make the guard scan the "+
			"wrong text and go green for the wrong reasons")
	require.Empty(t, corpsGenerateur("func (AppModule) GenerateGenesisState(s *T) { x := 1 }"),
		"single-line body: no `\\n}` -> extraction fails -> empty, hence red at the caller")
	require.NotEmpty(t, corpsGenerateur("func (AppModule) GenerateGenesisState(s *T) {\n\tx := 1\n}\n"))
	require.NotEmpty(t, corpsGenerateur("func (am AppModule) GenerateGenesisState(s *T) {\n\tx := 1\n}\n"))
}

// entreDeux returns what follows `debut` up to the first `fin`, or "" if either is missing. Returning
// "" rather than the whole file is deliberate: the callers require a NON-EMPTY result, so a failed
// extraction produces a loud red instead of a check that scans the wrong text.
func entreDeux(src, debut, fin string) string {
	i := strings.Index(src, debut)
	if i < 0 {
		return ""
	}
	reste := src[i+len(debut):]
	j := strings.Index(reste, fin)
	if j < 0 {
		return ""
	}
	return reste[:j]
}

// sansCommentairesGo strips `//…`: the comments of those files DESCRIBE the trap and therefore quote
// `sample.AccAddress()` by name. It is the CODE that must no longer carry it.
func sansCommentairesGo(src string) string {
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
