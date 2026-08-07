package app

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// ═══ AN ADVERTISED BUT INERT PARAMETER IS A GUARD ONE ONLY BELIEVES IN ═══════════════════════════
//
// What is at stake. `dendrad query jobs params` displays `demand_client_cap: 100` and
// `miner_vest_blocks: 200`. An operator concludes that a client is capped and that part of the
// miner's payment is locked for 200 blocks. No line of the chain reads either field. A governance
// proposal changing them would PASS and do NOTHING — the worst of both worlds, because the decision
// has the appearance of having taken place.
//
// How that ended. Those fields were WITHDRAWN: proto numbers 6, 8, 9 and 12 are now `reserved` in
// dendra/jobs/v1/params.proto and no longer appear in `query jobs params`. This changes the parameter
// encoding, hence the state, hence the app-hash — a CONSENSUS change, admissible only because the
// launch requires a reset anyway. The numbers are reserved rather than freed: recycling one would let
// a node built before the withdrawal decode a NEW field as the old one.
//
// What remains, and what this file now guards, in three directions:
//   - a field declared inert that acquires a reader goes red (wiring it is a decision);
//   - a field declared inert that no longer exists goes red (the entry describes a stale state);
//   - a WITHDRAWN name that comes back into `Params` goes red, and so does a `.proto` that stops
//     reserving its number — either one silently reopens the illusion that was paid for to close.
//
// This check says NOTHING about dormant-but-wired parameters: `silence_slash_bps` is 0 by default
// and yet fully consumed. Dormant is not inert — the first is a value, the second is an absence of
// code.

// champsInertes — a field of `jobs.Params` with no reader at all, mapped to what its displayed value
// LEADS ONE TO BELIEVE. The reason describes the illusion, not the field: the illusion is the defect.
//
// `EpochBlocks` is the last one standing, and it is the most misleading of the set: it is a HOMONYM of
// `emission.epoch_blocks`, the parameter that really does drive the monetary curve. It was kept rather
// than withdrawn with the other four because a separate decision holds it inert under test; this entry
// is what keeps that decision from decaying into an oversight.
var champsInertes = map[string]string{
	"EpochBlocks": "displays an epoch length for x/jobs; the epoch that drives emission belongs to x/emission, and availability has its own period in avail_epoch_blocks",
}

// champsRetires — proto field NUMBER -> the name withdrawn from `jobs.Params`, mapped to the illusion
// that name used to sustain. A number is never recycled and a name never comes back: the pair is what
// TestParamsRetiresNeReviennentPas re-reads, in the generated type AND in the .proto.
var champsRetires = map[int]struct{ nom, illusion string }{
	6:  {"miner_vest_blocks", "displayed a vesting duration for the miner payment; no release schedule ever existed"},
	8:  {"burn_bps", "displayed a burn share of a slash; the whole slashed amount is credited to pools.Treasury and nothing is burned"},
	9:  {"reporter_bps", "displayed a reporter share of a slash; no slash path pays a reporter"},
	12: {"demand_client_cap", "displayed a per-client cap on the demand base; no cap was applied anywhere"},
}

// lecteursDuChamp walks a module's production code and returns the lines that READ the field
// (`.Field` preceded by an identifier). Declarations (`Field:` inside a struct literal) do not
// match, since they are not preceded by a dot.
func lecteursDuChamp(t *testing.T, racine, champ string) []string {
	t.Helper()
	trouves := []string{}
	err := filepath.Walk(racine, func(chemin string, info os.FileInfo, e error) error {
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
			if strings.Contains(nu, "."+champ) {
				trouves = append(trouves, chemin+" : "+strings.TrimSpace(l))
			}
		}
		return nil
	})
	require.NoError(t, err)
	sort.Strings(trouves)
	return trouves
}

// (1) FIELDS DECLARED INERT STAY INERT — and they still exist.
func TestInertParamsStillHaveNoReader(t *testing.T) {
	tousLesChamps := champsParModule(t)["jobs"]

	for champ, illusion := range champsInertes {
		require.True(t, tousLesChamps[champ],
			"`jobs.%s` is declared inert but no longer exists in Params: this entry describes a stale "+
				"state. If the field was removed, remove its declaration here too.", champ)

		lecteurs := lecteursDuChamp(t, filepath.Join("..", "x", "jobs"), champ)
		require.Empty(t, lecteurs,
			"production code now reads `jobs.%s`: %v.\n"+
				"This parameter was INERT — it %s. Wiring it is a decision: it changes what the chain does "+
				"with parameter values already posted on live networks. This test goes red to force that "+
				"choice, not to forbid the usage.",
			champ, lecteurs, illusion)
	}
}

// (3) WITHDRAWN FIELDS DO NOT COME BACK — neither the name, nor the number.
//
// A withdrawal is only worth what its irreversibility is worth. Two ways to undo it by accident, and
// this test closes both:
//
//	(a) THE NAME returns to `Params`. Someone re-adds `demand_client_cap` under a fresh number,
//	    convinced they are wiring the guard ADR-017 describes; `query jobs params` displays it again,
//	    and unless the wiring came with it, the exact illusion that was paid for in a chain reset is
//	    back. The test does not forbid the mechanism — it forbids resurrecting the NAME quietly.
//	(b) THE NUMBER is freed. Drop the `reserved 6, 8, 9, 12;` line and the next field added to the
//	    message may take one of those numbers. A node built before the withdrawal then decodes the new
//	    field AS THE OLD ONE: the wire format carries numbers, not names. That is a silent
//	    misinterpretation of state across binaries, which no amount of testing downstream can catch.
//
// The `.proto` is re-read rather than trusted: the generated code contains no trace of a `reserved`
// declaration, so the source file is the only place where (b) is observable.
func TestParamsRetiresNeReviennentPas(t *testing.T) {
	tousLesChamps := champsParModule(t)["jobs"]

	brut, err := os.ReadFile(filepath.Join("..", "proto", "dendra", "jobs", "v1", "params.proto"))
	require.NoError(t, err)
	proto := string(brut)

	// The `reserved` declarations must be inside `message Params`, not in some other message of the
	// file: a number reserved elsewhere protects nothing here.
	debut := strings.Index(proto, "message Params {")
	require.GreaterOrEqual(t, debut, 0, "`message Params` not found in params.proto")
	fin := strings.Index(proto[debut:], "\n}")
	require.Greater(t, fin, 0, "the body of `message Params` is not delimited as expected")
	corps := proto[debut : debut+fin]

	for numero, retire := range champsRetires {
		champ := nomGoDuChampProto(retire.nom)

		require.False(t, tousLesChamps[champ],
			"`jobs.%s` is BACK in Params. It was withdrawn because it %s.\n"+
				"If the guard is genuinely being built, it needs a NEW field number and a name that is not "+
				"the one operators learned to read as inert — and the entry for %d must move out of "+
				"champsRetires in the same pass.", champ, retire.illusion, numero)

		require.Regexp(t, `(?m)^\s*reserved\b[^;]*\b`+strconv.Itoa(numero)+`\b[^;]*;`, corps,
			"field number %d (%s) is no longer `reserved` in message Params. Freeing it lets a future "+
				"field take that number, and a node built before the withdrawal would decode the new field "+
				"as the old one — names live in source, the wire carries numbers.", numero, retire.nom)

		require.Regexp(t, `(?m)^\s*reserved\b[^;]*"`+regexp.QuoteMeta(retire.nom)+`"[^;]*;`, corps,
			"the name %q is no longer `reserved` in message Params. Reserving the number alone still "+
				"allows the name to be reused for a different field, which is how a parameter set ends up "+
				"with a familiar name that means something else.", retire.nom)
	}
}

// nomGoDuChampProto converts a snake_case proto field name into the Go field name gogoproto generates
// (`miner_vest_blocks` -> `MinerVestBlocks`). Derived rather than written down twice: a hand-written
// pair would let the two halves drift apart without anything going red.
func nomGoDuChampProto(nom string) string {
	var b strings.Builder
	for _, morceau := range strings.Split(nom, "_") {
		if morceau == "" {
			continue
		}
		b.WriteString(strings.ToUpper(morceau[:1]))
		b.WriteString(morceau[1:])
	}
	return b.String()
}

// (2) `MinerLocked` IS NEVER DECREMENTED — a counter that only goes up.
//
// What the name promises and what the code does. `pools.MinerLocked` accumulates the "locked" share
// of a miner's payment (`miner_vest_bps`, 30 % by default). Nothing releases it: no site decrements
// it, and the schedule that ought to — `miner_vest_blocks` — never had a reader and has now been
// withdrawn from the parameter set (case 3). The quantity is therefore not a locked balance but a
// HISTORICAL TOTAL; reading it as a balance awaiting release yields a wrong figure with the
// confidence of a measurement.
//
// A second fact, just as important: the ONLY write site is `SettleJob`, the authority-gated legacy
// settlement — the production settlement paths do not vest at all. Pinning the full set of sites
// makes any widening of this mechanism visible.
func TestPoolsMinerLockedStaysACumulativeTotalWithNoRelease(t *testing.T) {
	sites := lecteursDuChamp(t, filepath.Join("..", "x", "jobs"), "MinerLocked")

	fichiers := map[string]int{}
	for _, s := range sites {
		fichiers[filepath.Base(strings.SplitN(s, " : ", 2)[0])]++
		require.NotContains(t, s, "-=",
			"`MinerLocked` is now DECREMENTED (%s), so a release mechanism exists. It then needs a "+
				"schedule, and `miner_vest_blocks` is not one: it has no reader.", s)
	}
	require.Equal(t, map[string]int{"msg_server_settle_job.go": 1}, fichiers,
		"the set of sites touching `pools.MinerLocked` has changed: %v.\n"+
			"This counter has a single write, in the authority-gated legacy settlement, and no release. "+
			"Widening its mechanics without a release schedule would grow a quantity that its name "+
			"presents as a restitutable balance.", sites)
}
