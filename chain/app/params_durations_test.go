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

// Any block count that means a duration must say which duration.
//
// The chain counts in blocks because height is the only deterministic clock, but most of these numbers
// mean a duration, and a conversion ("about one second per block") kept in comments drifts across
// places and wordings. A value that is correct on the day it is written but whose meaning
// depends on an external quantity nobody watches is the time version of a threshold whose denominator
// changed underneath it. If the real block interval doubles, `epoch_blocks` does not move, and an
// emission of "22%/year" becomes 22% every two years without a single test turning red.
//
// This file requires every parameter whose name announces a duration to be classified: real unit,
// intended duration, and a written reason. The table works both ways: a new parameter with a
// time-flavoured name stays red until it is classified, and an entry that no longer matches anything
// is red as well.
//
// One name is already misleading: `avail_fail_window` says "window" but counts availability
// epochs, not blocks (a 64-bit mask, see `availability.go`). A check that assumed a time-flavoured
// suffix implies blocks would produce a wrong figure that looks like a measurement, so every
// declaration states its unit explicitly.

// This file carries two inventories, and the second is not a special case of the first.
// The suffix scan only sees names that announce a duration, so it misses `work_gate_bps`, present in
// two modules with two different effects. A name collision is the same kind of trap as an implicit
// unit (a lever that seems to act where it does not), but no suffix reveals it. Hence the second
// inventory, at the end of the file.

// durationSuffixes lists what, in a parameter name, announces a duration.
var durationSuffixes = []string{"Blocks", "Window", "Timeout", "Delay", "Stride"}

type durationDeclaration struct {
	unit             string // "blocks" | "epochs" | "inert"
	intendedDuration uint64 // seconds; 0 = dormant (zero default value) or not applicable
	reason           string // at least 60 characters: a reason has to be written down
}

// declaredDurations is keyed by "module.Field".
var declaredDurations = map[string]durationDeclaration{
	"jobs.EpochBlocks": {"inert", 0,
		"dead parameter: no code in x/jobs reads params.EpochBlocks (checked by TestParamsDurationJobsEpochBlocksStaysInert). Its real homonym lives in x/emission"},
	"jobs.CommitteeRevealDelay": {"blocks", 0,
		"deferred committee reveal (anti-grinding H6): dormant at 0; once armed, state the intended duration here"},
	"jobs.AvailEpochBlocks": {"blocks", 0,
		"period of the availability challenge (Phase 1b): dormant at 0; the devnet arms it at 300 blocks through config.yml, not here"},
	"jobs.DisputeWindow": {"blocks", 0,
		"window during which a dispute may be opened after an optimistic settlement: dormant at 0; lower bound of audit_resolve_timeout once armed"},
	"jobs.AuditResolveTimeout": {"blocks", 0,
		"delay after which an unresolved audit is decided: dormant at 0; the devnet arms it at 120 blocks, about 2 min at the target interval"},
	"jobs.AppealWindow": {"blocks", 0,
		"appeal window (ADR-028 v2): dormant at 0 and not yet consumed by the code - reserved, which is distinct from dormant"},
	"jobs.AuditUnwindBlocks": {"blocks", 0,
		"ADR-044 gesture 2: age of a retention past which it is unwound instead of deferred again. Dormant at 0, and 0 is what ships - the age is the operator's decision about their network, not a number the code can derive. Once armed it counts blocks and Validate keeps it >= audit_resolve_timeout, so an unwind can never fire before the audit it waits for has been retried once"},
	"jobs.AvailDeadlineBlocks": {"blocks", 0,
		"blocks granted to a miner to answer the availability challenge: dormant at 0 (no challenge is enforceable)"},
	"jobs.AvailFailWindow": {"epochs", 0,
		"not in blocks despite its name: sliding window counted in availability epochs, bounded to 64 by the capacity of the mask (availability.go)"},
	"jobs.JurorFreshnessBlocks": {"blocks", 0,
		"age beyond which a miner stops being summonable as a juror: dormant at 0; ordered before miner_prune_blocks by Validate"},
	"jobs.JobExpiryBlocks": {"blocks", 0,
		"ADR-048 item 6: how long an unserved job keeps the client's escrow before it is returned. 0 selects the compiled 100 000 blocks; a posted value is bounded to [100, 10 000 000] and must outlast committee_reveal_delay"},
	"jobs.MinerPruneBlocks": {"blocks", 0,
		"inactivity beyond which a miner is evicted from the registry: dormant at 0; must stay >= juror_freshness_blocks"},
	"emission.EpochBlocks": {"blocks", 31_536_000,
		"release period of the Reserve: one year at the target interval. It is the denominator of the monetary curve -- the only number in this table that carries money"},
}

func durationFields(t *testing.T, module string, params any) map[string]uint64 {
	t.Helper()
	rv := reflect.ValueOf(params)
	out := map[string]uint64{}
	for i := 0; i < rv.NumField(); i++ {
		name := rv.Type().Field(i).Name
		isDuration := false
		for _, s := range durationSuffixes {
			if strings.HasSuffix(name, s) {
				isDuration = true
				break
			}
		}
		if !isDuration {
			continue
		}
		require.Equal(t, reflect.Uint64, rv.Field(i).Kind(),
			"%s.%s has a time-flavoured name but is not a uint64: this check cannot read it", module, name)
		out[module+"."+name] = rv.Field(i).Uint()
	}
	require.NotEmpty(t, out,
		"no time-flavoured field found in %s: reflection no longer bites, this check verifies nothing", module)
	return out
}

func allDurationFields(t *testing.T) map[string]uint64 {
	t.Helper()
	all := map[string]uint64{}
	for k, v := range durationFields(t, "jobs", jobstypes.DefaultParams()) {
		all[k] = v
	}
	for k, v := range durationFields(t, "emission", emissiontypes.DefaultParams()) {
		all[k] = v
	}
	return all
}

// (1) Every time-flavoured field is classified, and every classification matches a field.
func TestParamsDurationInventoryRoundTripsBothWays(t *testing.T) {
	all := allDurationFields(t)

	missing := []string{}
	for key := range all {
		if _, ok := declaredDurations[key]; !ok {
			missing = append(missing, key)
		}
	}
	sort.Strings(missing)
	require.Empty(t, missing,
		"unclassified time-flavoured parameter(s): %v.\n"+
			"A block count that means a duration must say which duration, and in which unit: "+
			"`avail_fail_window` counts epochs, not blocks, and nothing in its name says so. "+
			"Add an entry to `declaredDurations` with its unit, its intended duration and its reason.",
		missing)

	stale := []string{}
	for key := range declaredDurations {
		if _, ok := all[key]; !ok {
			stale = append(stale, key)
		}
	}
	sort.Strings(stale)
	require.Empty(t, stale,
		"classification(s) that no longer match any parameter: %v. A stale entry describes a state "+
			"that no longer exists, yet it still reads like a measurement.", stale)

	for key, d := range declaredDurations {
		require.Contains(t, []string{"blocks", "epochs", "inert"}, d.unit,
			"unknown unit for %s: %q", key, d.unit)
		require.GreaterOrEqual(t, len(d.reason), 60,
			"the reason for %s is %d characters long: a reason that is not written down was not had",
			key, len(d.reason))
	}
}

// (2) Fields armed in blocks are worth the duration they declare.
func TestParamsDurationTheArmedValuesAreWhatTheyAnnounce(t *testing.T) {
	all := allDurationFields(t)
	verifies := 0
	for key, value := range all {
		d := declaredDurations[key]
		if d.unit != "blocks" || value == 0 || d.intendedDuration == 0 {
			continue
		}
		require.Equal(t, d.intendedDuration, chaintime.Seconds(value),
			"%s is worth %d blocks = %d s at the target interval (%d s/block), but its classification "+
				"announces %d s. Either the value changed without its meaning being re-read, or the "+
				"target block interval moved. In both cases the duration this parameter is supposed to "+
				"express is no longer the one it expresses.",
			key, value, chaintime.Seconds(value), chaintime.TargetBlockSeconds, d.intendedDuration)
		verifies++
	}
	// The floor is 1, and that 1 is a fact about the code. Exactly one duration parameter is armed
	// by default and consumed: `emission.EpochBlocks`, the denominator of the monetary curve. All
	// the others are dormant at 0. The one that used to carry a non-zero value without a reader,
	// `jobs.miner_vest_blocks`, no longer exists: its proto number is `reserved`, so a value nobody
	// reads cannot be mistaken for a measurement. If this count drops to 0, the block-to-duration
	// conversion is no longer verified anywhere.
	require.GreaterOrEqual(t, verifies, 1,
		"no armed parameter verified (%d): this check no longer measures anything — the "+
			"block-to-duration conversion is not compared with any real value", verifies)
}

// (3) Consensus witness: the value of `EpochBlocks` is pinned, on top of being derived.
//
// The derivation (`chaintime.Year / chaintime.TargetBlockSeconds`) makes the meaning visible, and it
// also makes the value changeable from another file. That number is the denominator of the monetary
// curve and it enters the genesis state, so changing it is a consensus change that requires a chain
// reset. This test keeps such a change from happening as a side effect of adjusting
// `TargetBlockSeconds` for an unrelated reason.
func TestParamsDurationEpochBlocksIsPinnedToOneYear(t *testing.T) {
	const expected uint64 = 31_536_000
	require.Equal(t, expected, emissiontypes.DefaultParams().EpochBlocks,
		"the default of `epoch_blocks` in x/emission changed. This number enters the genesis state and "+
			"fixes the release period of the Reserve: modifying it is a consensus change (chain reset) "+
			"and a modification of the announced monetary curve (~22 %%/year). If it is intended, say so "+
			"here; otherwise it is a side effect.")
	require.Equal(t, chaintime.Year, chaintime.Seconds(expected),
		"the emission period is no longer one year at the target block interval")

	// The keeper fallback and the types default must not become two copies of the same number
	// (`31_536_000`), two sources of truth for the denominator of the monetary curve, since the
	// fallback is the one nobody re-reads. Both derive from the same constant, and this test
	// keeps them together.
	require.Equal(t, emissiontypes.DefaultEpochBlocks, emissionkeeper.EpochBlocks,
		"the fallback in `x/emission/keeper` has diverged again from the default in `x/emission/types`: "+
			"two numbers for a single emission period, one of which nobody re-reads")
}

// (4) `jobs.epoch_blocks` is inert, and must stay inert until its name collision is settled.
//
// `x/jobs` exposes a governable parameter `epoch_blocks`, default 100, that no line of code reads. Its
// homonym in `x/emission` is 31,536,000 and drives the release of the Reserve. An operator reading
// `dendrad query jobs params` sees `epoch_blocks: 100` and may believe the emission epoch is 100 blocks
// long, and a governance proposal changing it would pass and do nothing. A no-op that looks like a
// decision is worse than having no lever; here the name is right and the module is wrong.
//
// The field cannot be removed (proto field number, query API compatibility). Wiring it would be a
// decision, hence this red, which forces that decision instead of letting it happen by accident.
func TestParamsDurationJobsEpochBlocksStaysInert(t *testing.T) {
	readers := []string{}
	err := filepath.Walk(filepath.Join("..", "x", "jobs"), func(path string, info os.FileInfo, e error) error {
		if e != nil {
			return e
		}
		if info.IsDir() || !strings.HasSuffix(path, ".go") ||
			strings.HasSuffix(path, "_test.go") || strings.HasSuffix(path, ".pb.go") {
			return nil
		}
		b, er := os.ReadFile(path)
		if er != nil {
			return er
		}
		for _, l := range strings.Split(string(b), "\n") {
			bare := l
			if i := strings.Index(bare, "//"); i >= 0 {
				bare = bare[:i]
			}
			// `.EpochBlocks` preceded by an identifier is a read of the field. The declaration of the
			// parameter (`EpochBlocks:` in NewParams) does not match, since it is not preceded by a dot.
			if strings.Contains(bare, ".EpochBlocks") {
				readers = append(readers, path+" : "+strings.TrimSpace(l))
			}
		}
		return nil
	})
	require.NoError(t, err)
	require.Empty(t, readers,
		"code in `x/jobs` now reads `params.EpochBlocks`: %v.\n"+
			"This parameter was inert, and its name is the exact homonym of the one in `x/emission` that "+
			"drives the release of the Reserve. Wiring it is a decision that must be taken explicitly: "+
			"either rename it to lift the collision, or document both at their two declarations. This "+
			"test turns red to force that choice, not to forbid the use.",
		readers)
}

// Any parameter name carried by two modules must say what each one does.
//
// The suffix scan does not see this defect. `epoch_blocks` was spotted because its name announces a
// duration; `work_gate_bps` carries the same trap and announces nothing. It exists in `x/jobs` (cap on
// the subsidy a miner can claim, `msg_server_claim_subsidy.go::ClaimSubsidy`) and in `x/emission` (cap
// on the work subsidy released per epoch, `emission.go::EpochRelease`). Both are active and
// governable, and they do not control the same thing: a proposal that "raises the work gate" raises
// only one of them, and the measured result will not be the one that was voted.
//
// This inventory requires every name present in several modules to be classified, with what each one
// controls. It works both ways, like the first inventory: a new homonym stays red until it is
// classified, and an entry that no longer matches a collision is red as well, since a stale
// classification reads like a measurement.
//
// It does not forbid homonyms. Two modules may use the same name; what matters is that somebody wrote
// down what each one does.

type homonymDeclaration struct {
	// effects holds one text per module carrying the name, keyed by module name.
	effects map[string]string
}

var declaredHomonyms = map[string]homonymDeclaration{
	"EpochBlocks": {map[string]string{
		"jobs":     "inert: no line of x/jobs reads it (checked by TestParamsDurationJobsEpochBlocksStaysInert)",
		"emission": "active: release period of the Reserve, denominator of the monetary curve",
	}},
	"WorkGateBps": {map[string]string{
		"jobs":     "active: cap on the subsidy claimable by a miner (served demand x gate), msg_server_claim_subsidy.go",
		"emission": "active: cap on the work subsidy released each epoch (non-recoverable demand x gate), emission.go",
	}},
}

// fieldsByModule returns every Params field name, per module.
func fieldsByModule(t *testing.T) map[string]map[string]bool {
	t.Helper()
	out := map[string]map[string]bool{}
	for module, params := range map[string]any{
		"jobs":     jobstypes.DefaultParams(),
		"emission": emissiontypes.DefaultParams(),
	} {
		rv := reflect.ValueOf(params)
		fields := map[string]bool{}
		for i := 0; i < rv.NumField(); i++ {
			name := rv.Type().Field(i).Name
			// Internal fields generated by gogoproto (XXX_*) are not parameters.
			if strings.HasPrefix(name, "XXX_") {
				continue
			}
			fields[name] = true
		}
		require.NotEmpty(t, fields, "no field read in %s: reflection no longer bites", module)
		out[module] = fields
	}
	return out
}

// TestParamsHomonymInventoryIsBidirectional checks that every name carried by at least two modules is
// classified, and that every classification matches a real collision.
func TestParamsHomonymInventoryIsBidirectional(t *testing.T) {
	byModule := fieldsByModule(t)

	carriers := map[string][]string{}
	for module, fields := range byModule {
		for name := range fields {
			carriers[name] = append(carriers[name], module)
		}
	}

	collisions := map[string][]string{}
	for name, mods := range carriers {
		if len(mods) > 1 {
			sort.Strings(mods)
			collisions[name] = mods
		}
	}

	missing := []string{}
	for name := range collisions {
		if _, ok := declaredHomonyms[name]; !ok {
			missing = append(missing, name)
		}
	}
	sort.Strings(missing)
	require.Empty(t, missing,
		"unclassified homonym parameter(s): %v.\n"+
			"The same name in two modules is two distinct governance levers. As long as nobody writes "+
			"down what each one commands, a proposal that modifies one reads as if it modified both — "+
			"and the lever one believed to have pulled has not moved. "+
			"Add an entry to `declaredHomonyms` with the real effect in each module.",
		missing)

	stale := []string{}
	for name := range declaredHomonyms {
		if _, ok := collisions[name]; !ok {
			stale = append(stale, name)
		}
	}
	sort.Strings(stale)
	require.Empty(t, stale,
		"homonym classification(s) that no longer match any collision: %v. The collision was lifted "+
			"(rename) or the parameter disappeared — the entry describes a state that no longer exists.",
		stale)

	for name, mods := range collisions {
		d := declaredHomonyms[name]
		declared := []string{}
		for module := range d.effects {
			declared = append(declared, module)
		}
		sort.Strings(declared)
		require.Equal(t, mods, declared,
			"the classification of %s does not cover exactly the modules that carry it", name)
		for module, effect := range d.effects {
			require.GreaterOrEqual(t, len(effect), 40,
				"the declared effect of %s in %s is %d characters long: what it commands does not fit "+
					"in three words", name, module, len(effect))
		}
	}
}
