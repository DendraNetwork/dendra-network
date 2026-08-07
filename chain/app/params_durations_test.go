package app

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/chaintime"
	emissionkeeper "github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	emissiontypes "github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ ANY BLOCK COUNT THAT MEANS A DURATION MUST SAY WHICH ONE ════════════════════════════════════
//
// The chain counts in BLOCKS because height is the only deterministic clock. But most of these
// numbers mean a DURATION, and the conversion — "about one second per block" — used to live in
// comments, in several places, in several wordings. An absolute value that is correct on the day it
// is written but whose MEANING depends on an external quantity nothing watches is the same shape of
// defect as a threshold whose denominator changed underneath it, applied to time. If the real block
// interval doubles, `epoch_blocks` does not move and an emission of "22%/year" becomes 22% every two
// years — without a single test turning red.
//
// WHAT THIS FILE REQUIRES: every parameter whose NAME announces a duration must be CLASSIFIED —
// real unit, intended duration, and a written reason. The table is BIDIRECTIONAL: a new parameter
// with a time-flavoured name stays red until it is classified, and an entry that no longer matches
// anything is red as well.
//
// AND ONE NAME ALREADY LIES: `avail_fail_window` is called "window" and is NOT in blocks — it is a
// count of availability EPOCHS (64-bit mask, `availability.go:171`). A check that assumed
// "time-flavoured suffix implies blocks" would produce a wrong figure with the confidence of a
// measurement. Hence the EXPLICIT unit in every declaration rather than one inferred from the name.

// THIS FILE CARRIES TWO INVENTORIES, AND THE SECOND IS NOT A SPECIAL CASE OF THE FIRST.
// The SUFFIX scan only sees names that announce a duration, so it misses `work_gate_bps`, present in
// TWO modules with two different effects. A name collision is the same family of trap as an implicit
// unit — a lever that appears to act where it does not — but it cannot be detected from a suffix.
// Hence the second inventory, at the end of the file.

// suffixesTemporels — what, in a parameter name, ANNOUNCES a duration.
var suffixesTemporels = []string{"Blocks", "Window", "Timeout", "Delay", "Stride"}

type declarationDuree struct {
	unite      string // "blocs" | "epoques" | "inerte"
	dureeVisee uint64 // seconds; 0 = dormant (zero default value) or not applicable
	raison     string // >= 60 characters — a reason that is not written down was not had
}

// dureesDeclarees — key "module.Field".
var dureesDeclarees = map[string]declarationDuree{
	"jobs.EpochBlocks": {"inerte", 0,
		"DEAD PARAMETER: no code in x/jobs reads params.EpochBlocks (checked by TestParamsDureeJobsEpochBlocksResteInerte). Its REAL homonym lives in x/emission"},
	"jobs.CommitteeRevealDelay": {"blocs", 0,
		"deferred committee reveal (anti-grinding H6): dormant at 0; once armed, state the intended duration here"},
	"jobs.AvailEpochBlocks": {"blocs", 0,
		"period of the availability challenge (Phase 1b): dormant at 0; the devnet arms it at 300 blocks through config.yml, not here"},
	"jobs.DisputeWindow": {"blocs", 0,
		"window during which a dispute may be opened after an optimistic settlement: dormant at 0; lower bound of audit_resolve_timeout once armed"},
	"jobs.AuditResolveTimeout": {"blocs", 0,
		"delay after which an unresolved audit is decided: dormant at 0; the devnet arms it at 120 blocks, about 2 min at the target interval"},
	"jobs.AppealWindow": {"blocs", 0,
		"appeal window (ADR-028 v2): dormant at 0 and NOT YET CONSUMED by the code - reserved, which is distinct from dormant"},
	"jobs.AvailDeadlineBlocks": {"blocs", 0,
		"blocks granted to a miner to answer the availability challenge: dormant at 0 (no challenge is enforceable)"},
	"jobs.AvailFailWindow": {"epoques", 0,
		"NOT IN BLOCKS despite its name: sliding window counted in availability EPOCHS, bounded to 64 by the capacity of the mask (availability.go:171)"},
	"jobs.JurorFreshnessBlocks": {"blocs", 0,
		"age beyond which a miner stops being summonable as a juror: dormant at 0; ordered before miner_prune_blocks by Validate"},
	"jobs.MinerPruneBlocks": {"blocs", 0,
		"inactivity beyond which a miner is evicted from the registry: dormant at 0; must stay >= juror_freshness_blocks"},
	"emission.EpochBlocks": {"blocs", 31_536_000,
		"release period of the Reserve: ONE YEAR at the target interval. It is the denominator of the monetary curve -- the only number in this table that carries money"},
}

func champsTemporels(t *testing.T, module string, params any) map[string]uint64 {
	t.Helper()
	rv := reflect.ValueOf(params)
	out := map[string]uint64{}
	for i := 0; i < rv.NumField(); i++ {
		nom := rv.Type().Field(i).Name
		temporel := false
		for _, s := range suffixesTemporels {
			if strings.HasSuffix(nom, s) {
				temporel = true
				break
			}
		}
		if !temporel {
			continue
		}
		require.Equal(t, reflect.Uint64, rv.Field(i).Kind(),
			"%s.%s has a time-flavoured name but is not a uint64: this check cannot read it", module, nom)
		out[module+"."+nom] = rv.Field(i).Uint()
	}
	require.NotEmpty(t, out,
		"no time-flavoured field found in %s: reflection no longer bites, this check verifies nothing", module)
	return out
}

func tousLesChampsTemporels(t *testing.T) map[string]uint64 {
	t.Helper()
	tous := map[string]uint64{}
	for k, v := range champsTemporels(t, "jobs", jobstypes.DefaultParams()) {
		tous[k] = v
	}
	for k, v := range champsTemporels(t, "emission", emissiontypes.DefaultParams()) {
		tous[k] = v
	}
	return tous
}

// (1) EVERY TIME-FLAVOURED FIELD IS CLASSIFIED — and every classification matches a field.
func TestParamsDureeInventaireBidirectionnel(t *testing.T) {
	tous := tousLesChampsTemporels(t)

	manquants := []string{}
	for cle := range tous {
		if _, ok := dureesDeclarees[cle]; !ok {
			manquants = append(manquants, cle)
		}
	}
	sort.Strings(manquants)
	require.Empty(t, manquants,
		"UNCLASSIFIED time-flavoured parameter(s): %v.\n"+
			"A block count that means a duration must say WHICH duration, and in WHICH unit: "+
			"`avail_fail_window` counts epochs, not blocks, and nothing in its name says so. "+
			"Add an entry to `dureesDeclarees` with its unit, its intended duration and its reason.",
		manquants)

	perimees := []string{}
	for cle := range dureesDeclarees {
		if _, ok := tous[cle]; !ok {
			perimees = append(perimees, cle)
		}
	}
	sort.Strings(perimees)
	require.Empty(t, perimees,
		"classification(s) that no longer match any parameter: %v. A stale entry describes a state "+
			"that no longer exists, yet it still reads like a measurement.", perimees)

	for cle, d := range dureesDeclarees {
		require.Contains(t, []string{"blocs", "epoques", "inerte"}, d.unite,
			"unknown unit for %s: %q", cle, d.unite)
		require.GreaterOrEqual(t, len(d.raison), 60,
			"the reason for %s is %d characters long: a reason that is not written down was not had",
			cle, len(d.raison))
	}
}

// (2) FIELDS ARMED IN BLOCKS ARE WORTH THE DURATION THEY DECLARE.
func TestParamsDureeLesArmesValentCeQuIlsAnnoncent(t *testing.T) {
	tous := tousLesChampsTemporels(t)
	verifies := 0
	for cle, valeur := range tous {
		d := dureesDeclarees[cle]
		if d.unite != "blocs" || valeur == 0 || d.dureeVisee == 0 {
			continue
		}
		require.Equal(t, d.dureeVisee, chaintime.Secondes(valeur),
			"%s is worth %d blocks = %d s at the target interval (%d s/block), but its classification "+
				"announces %d s. Either the value changed without its MEANING being re-read, or the "+
				"target block interval moved. In both cases the duration this parameter is supposed to "+
				"express is no longer the one it expresses.",
			cle, valeur, chaintime.Secondes(valeur), chaintime.TargetBlockSeconds, d.dureeVisee)
		verifies++
	}
	// ANTI-DECORATION WITNESS — the floor is 1, and that 1 is a FACT, not a relaxation.
	// Exactly ONE duration parameter is armed by default AND consumed: `emission.EpochBlocks`, the
	// denominator of the monetary curve. All the others are dormant at 0. The one that used to carry a
	// non-zero value without a reader, `jobs.miner_vest_blocks`, no longer exists: its proto number is
	// `reserved`, so a value nobody reads can no longer be mistaken for a measurement. If this count
	// drops to 0, the block-to-duration conversion is no longer verified anywhere.
	require.GreaterOrEqual(t, verifies, 1,
		"no armed parameter verified (%d): this check no longer measures anything — the "+
			"block-to-duration conversion is not confronted with any real value", verifies)
}

// (3) CONSENSUS WITNESS — the value of `EpochBlocks` is PINNED, not merely derived.
//
// The derivation (`chaintime.An / chaintime.TargetBlockSeconds`) makes the meaning visible; it also
// makes the value MODIFIABLE from another file. That number is the denominator of the monetary curve
// and it enters the genesis state: changing it is a CONSENSUS change, which requires a chain reset.
// This test exists so that such a change cannot happen as a side effect of adjusting
// `TargetBlockSeconds` for some entirely unrelated reason.
func TestParamsDureeEpochBlocksEstEpingleAUnAn(t *testing.T) {
	const attendu uint64 = 31_536_000
	require.Equal(t, attendu, emissiontypes.DefaultParams().EpochBlocks,
		"the default of `epoch_blocks` in x/emission changed. This number enters the genesis state and "+
			"fixes the release period of the Reserve: modifying it is a CONSENSUS CHANGE (chain reset) "+
			"and a modification of the announced monetary curve (~22 %%/year). If it is intended, say so "+
			"here; otherwise it is a side effect.")
	require.Equal(t, chaintime.An, chaintime.Secondes(attendu),
		"the emission period is no longer one year at the target block interval")

	// THE KEEPER FALLBACK AND THE TYPES DEFAULT WERE TWO COPIES OF THE SAME NUMBER. `31_536_000` was
	// written in two unrelated places: two sources of truth for the denominator of the monetary curve,
	// one of which could be adjusted without the other — and it is the fallback, the one nobody
	// re-reads, that would have survived. They now derive from the same point; this test refuses to let
	// them be split again.
	require.Equal(t, emissiontypes.DefaultEpochBlocks, emissionkeeper.EpochBlocks,
		"the fallback in `x/emission/keeper` has diverged again from the default in `x/emission/types`: "+
			"two numbers for a single emission period, one of which nobody re-reads")
}

// (4) `jobs.epoch_blocks` IS INERT — and must stay inert until its name collision is settled.
//
// WHAT THIS CHECK PROTECTS. `x/jobs` exposes a GOVERNABLE parameter `epoch_blocks`, default 100, that
// **no line of code reads**. Its homonym in `x/emission` is worth 31,536,000 and drives the release of
// the Reserve. An operator reading `dendrad query jobs params` sees `epoch_blocks: 100` and may
// reasonably believe the emission epoch is 100 blocks long; a governance proposal modifying it would
// **pass and do nothing**. A no-op that looks like a decision is worse than the absence of a lever —
// and here the name is right, it is the MODULE that is wrong.
//
// The field cannot be removed (proto field number, query API compatibility). Wiring it would be a
// decision, hence this red, which forces that decision instead of letting it happen by accident.
func TestParamsDureeJobsEpochBlocksResteInerte(t *testing.T) {
	lecteurs := []string{}
	err := filepath.Walk(filepath.Join("..", "x", "jobs"), func(chemin string, info os.FileInfo, e error) error {
		if e != nil {
			return e
		}
		if info.IsDir() || !strings.HasSuffix(chemin, ".go") ||
			strings.HasSuffix(chemin, "_test.go") || strings.HasSuffix(chemin, ".pb.go") {
			return nil
		}
		b, er := os.ReadFile(chemin)
		if er != nil {
			return er
		}
		for _, l := range strings.Split(string(b), "\n") {
			nu := l
			if i := strings.Index(nu, "//"); i >= 0 {
				nu = nu[:i]
			}
			// `.EpochBlocks` preceded by an identifier is a READ of the field. The declaration of the
			// parameter (`EpochBlocks:` in NewParams) does not match: it is not preceded by a dot.
			if strings.Contains(nu, ".EpochBlocks") {
				lecteurs = append(lecteurs, chemin+" : "+strings.TrimSpace(l))
			}
		}
		return nil
	})
	require.NoError(t, err)
	require.Empty(t, lecteurs,
		"code in `x/jobs` now reads `params.EpochBlocks`: %v.\n"+
			"This parameter was INERT, and its name is the exact homonym of the one in `x/emission` that "+
			"drives the release of the Reserve. Wiring it is a decision that must be taken explicitly: "+
			"either rename it to lift the collision, or document both at their two declarations. This "+
			"test turns red to force that choice, not to forbid the use.",
		lecteurs)
}

// ═══ ANY PARAMETER NAME CARRIED BY TWO MODULES MUST SAY WHAT EACH ONE DOES ═══════════════════════
//
// THE DEFECT THE SUFFIX SCAN DOES NOT SEE. `epoch_blocks` was spotted because its name announces a
// duration. `work_gate_bps` carries the same trap and announces nothing: it exists in `x/jobs` (cap on
// the subsidy CLAIMABLE by a miner, `msg_server_claim_subsidy.go:39`) AND in `x/emission` (cap on the
// work subsidy RELEASED per epoch, `emission.go:54`). Both are active, both are governable, and they
// do not command the same thing. A governance proposal that "raises the work gate" raises only ONE of
// them; the other stays where it is, and the measured result will not be the one that was voted.
//
// WHAT THIS INVENTORY REQUIRES: every name present in several modules must be CLASSIFIED, with what
// each one commands. Bidirectional, like the first inventory: a new homonym stays red until it is
// classified, and an entry that no longer matches a collision is red as well — a stale classification
// reads like a measurement.
//
// What this inventory does NOT do: forbid homonyms. Two modules may legitimately use the same name;
// what is not legitimate is that nobody wrote down what each one does.

type declarationHomonyme struct {
	// effets — one text per module carrying the name, keyed by module name.
	effets map[string]string
}

var homonymesDeclares = map[string]declarationHomonyme{
	"EpochBlocks": {map[string]string{
		"jobs":     "INERT: no line of x/jobs reads it (checked by TestParamsDureeJobsEpochBlocksResteInerte)",
		"emission": "ACTIVE: release period of the Reserve, denominator of the monetary curve",
	}},
	"WorkGateBps": {map[string]string{
		"jobs":     "ACTIVE: cap on the subsidy CLAIMABLE by a miner (served demand x gate), msg_server_claim_subsidy.go",
		"emission": "ACTIVE: cap on the work subsidy RELEASED each epoch (non-recoverable demand x gate), emission.go",
	}},
}

// champsParModule — every Params field name, per module.
func champsParModule(t *testing.T) map[string]map[string]bool {
	t.Helper()
	out := map[string]map[string]bool{}
	for module, params := range map[string]any{
		"jobs":     jobstypes.DefaultParams(),
		"emission": emissiontypes.DefaultParams(),
	} {
		rv := reflect.ValueOf(params)
		champs := map[string]bool{}
		for i := 0; i < rv.NumField(); i++ {
			nom := rv.Type().Field(i).Name
			// Internal fields generated by gogoproto (XXX_*) are not parameters.
			if strings.HasPrefix(nom, "XXX_") {
				continue
			}
			champs[nom] = true
		}
		require.NotEmpty(t, champs, "no field read in %s: reflection no longer bites", module)
		out[module] = champs
	}
	return out
}

// TestParamsHomonymesInventaireBidirectionnel — every name carried by >= 2 modules is classified, and
// every classification matches a real collision.
func TestParamsHomonymesInventaireBidirectionnel(t *testing.T) {
	parModule := champsParModule(t)

	porteurs := map[string][]string{}
	for module, champs := range parModule {
		for nom := range champs {
			porteurs[nom] = append(porteurs[nom], module)
		}
	}

	collisions := map[string][]string{}
	for nom, mods := range porteurs {
		if len(mods) > 1 {
			sort.Strings(mods)
			collisions[nom] = mods
		}
	}

	manquants := []string{}
	for nom := range collisions {
		if _, ok := homonymesDeclares[nom]; !ok {
			manquants = append(manquants, nom)
		}
	}
	sort.Strings(manquants)
	require.Empty(t, manquants,
		"unclassified HOMONYM parameter(s): %v.\n"+
			"The same name in two modules is two distinct governance levers. As long as nobody writes "+
			"down what each one commands, a proposal that modifies one reads as if it modified both — "+
			"and the lever one believed to have pulled has not moved. "+
			"Add an entry to `homonymesDeclares` with the REAL effect in each module.",
		manquants)

	perimees := []string{}
	for nom := range homonymesDeclares {
		if _, ok := collisions[nom]; !ok {
			perimees = append(perimees, nom)
		}
	}
	sort.Strings(perimees)
	require.Empty(t, perimees,
		"homonym classification(s) that no longer match any collision: %v. The collision was lifted "+
			"(rename) or the parameter disappeared — the entry describes a state that no longer exists.",
		perimees)

	for nom, mods := range collisions {
		d := homonymesDeclares[nom]
		declares := []string{}
		for module := range d.effets {
			declares = append(declares, module)
		}
		sort.Strings(declares)
		require.Equal(t, mods, declares,
			"the classification of %s does not cover exactly the modules that carry it", nom)
		for module, effet := range d.effets {
			require.GreaterOrEqual(t, len(effet), 40,
				"the declared effect of %s in %s is %d characters long: what it commands does not fit "+
					"in three words", nom, module, len(effet))
		}
	}
}
