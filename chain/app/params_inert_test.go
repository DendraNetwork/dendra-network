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

// A parameter that is displayed but inert is a guard people only believe in.
//
// A field that no line of the chain reads can still show in `dendrad query jobs params`. Shown as
// `demand_client_cap: 100` and `miner_vest_blocks: 200`, two such fields would tell an operator that a
// client is capped and that part of the miner's payment is locked for 200 blocks, and a governance
// proposal changing them would pass and do nothing while looking like a decision.
//
// Those fields are withdrawn: proto numbers 6, 8, 9 and 12 are `reserved` in
// dendra/jobs/v1/params.proto and no longer appear in `query jobs params`. That changes the parameter
// encoding, hence the state and the app hash: a consensus change, acceptable only because the launch
// requires a reset anyway. The numbers are reserved and never freed: recycling one would let a node
// built before the withdrawal decode a new field as the old one.
//
// What remains, and what this file guards, in three directions:
//   - a field declared inert that gains a reader goes red (wiring it is a decision);
//   - a field declared inert that no longer exists goes red (the entry describes a stale state);
//   - a withdrawn name that comes back into `Params` goes red, and so does a `.proto` that stops
//     reserving its number, since either one silently reopens the illusion the reset closed.
//
// This check says nothing about dormant but wired parameters: `silence_slash_bps` is 0 by default and
// fully consumed. A dormant parameter has a value; an inert one has no code.

// inertFields maps a field of `jobs.Params` that has no reader at all to what its displayed value
// leads people to believe. The reason describes the illusion, because the illusion is the defect.
//
// `EpochBlocks` is the last one left, and the most misleading: it is a homonym of
// `emission.epoch_blocks`, the parameter that does drive the monetary curve. It was kept when the
// other four were withdrawn because a separate decision holds it inert under test; this entry keeps
// that decision from decaying into an oversight.
var inertFields = map[string]string{
	"EpochBlocks": "displays an epoch length for x/jobs; the epoch that drives emission belongs to x/emission, and availability has its own period in avail_epoch_blocks",
}

// retiredFields maps a proto field number to the name withdrawn from `jobs.Params` and the illusion
// that name gives a reader. A number is never recycled and a name never comes back:
// TestParamsRetiredFieldsNeverComeBack re-reads each pair in the generated type and in the .proto.
var retiredFields = map[int]struct{ name, illusion string }{
	6:  {"miner_vest_blocks", "displayed a vesting duration for the miner payment; no release schedule ever existed"},
	8:  {"burn_bps", "displayed a burn share of a slash; the whole slashed amount is credited to pools.Treasury and nothing is burned"},
	9:  {"reporter_bps", "displayed a reporter share of a slash; no slash path pays a reporter"},
	12: {"demand_client_cap", "displayed a per-client cap on the demand base; no cap was applied anywhere"},
}

// fieldReaders walks a module's production code and returns the lines that read the field
// (`.Field` preceded by an identifier). Declarations (`Field:` inside a struct literal) do not
// match, since they are not preceded by a dot.
func fieldReaders(t *testing.T, root, field string) []string {
	t.Helper()
	found := []string{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, e error) error {
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
			if strings.Contains(bare, "."+field) {
				found = append(found, path+" : "+strings.TrimSpace(l))
			}
		}
		return nil
	})
	require.NoError(t, err)
	sort.Strings(found)
	return found
}

// (1) Fields declared inert stay inert, and they still exist.
func TestInertParamsStillHaveNoReader(t *testing.T) {
	allFields := fieldsByModule(t)["jobs"]

	for field, illusion := range inertFields {
		require.True(t, allFields[field],
			"`jobs.%s` is declared inert but no longer exists in Params: this entry describes a stale "+
				"state. If the field was removed, remove its declaration here too.", field)

		readers := fieldReaders(t, filepath.Join("..", "x", "jobs"), field)
		require.Empty(t, readers,
			"production code now reads `jobs.%s`: %v.\n"+
				"This parameter was inert — it %s. Wiring it is a decision: it changes what the chain does "+
				"with parameter values already posted on live networks. This test goes red to force that "+
				"choice, not to forbid the usage.",
			field, readers, illusion)
	}
}

// (3) Withdrawn fields do not come back, neither the name nor the number.
//
// A withdrawal is only worth as much as its irreversibility. There are two ways to undo it by
// accident, and this test closes both:
//
//	(a) The name returns to `Params`. Someone re-adds `demand_client_cap` under a fresh number,
//	    convinced they are wiring the demand guard; `query jobs params` displays it
//	    again, and unless the wiring came with it, the illusion that cost a chain reset is back.
//	    The test does not forbid the mechanism, only the quiet return of the name.
//	(b) The number is freed. Drop the `reserved 6, 8, 9, 12;` line and the next field added to the
//	    message may take one of those numbers. A node built before the withdrawal then decodes the
//	    new field as the old one, since the wire format carries numbers and not names: a silent
//	    misreading of state across binaries that no downstream test can catch.
//
// The `.proto` is re-read rather than trusted: the generated code keeps no trace of a `reserved`
// declaration, so the source file is the only place where (b) can be observed.
func TestParamsRetiredFieldsNeverComeBack(t *testing.T) {
	allFields := fieldsByModule(t)["jobs"]

	raw, err := os.ReadFile(filepath.Join("..", "proto", "dendra", "jobs", "v1", "params.proto"))
	require.NoError(t, err)
	proto := string(raw)

	// The `reserved` declarations must be inside `message Params`: a number reserved in another
	// message of the file protects nothing here.
	start := strings.Index(proto, "message Params {")
	require.GreaterOrEqual(t, start, 0, "`message Params` not found in params.proto")
	end := strings.Index(proto[start:], "\n}")
	require.Greater(t, end, 0, "the body of `message Params` is not delimited as expected")
	body := proto[start : start+end]

	for number, retired := range retiredFields {
		field := goFieldNameOfProto(retired.name)

		require.False(t, allFields[field],
			"`jobs.%s` is back in Params. It was withdrawn because it %s.\n"+
				"If the guard is genuinely being built, it needs a new field number and a name that is not "+
				"the one operators learned to read as inert — and the entry for %d must move out of "+
				"retiredFields in the same pass.", field, retired.illusion, number)

		require.Regexp(t, `(?m)^\s*reserved\b[^;]*\b`+strconv.Itoa(number)+`\b[^;]*;`, body,
			"field number %d (%s) is no longer `reserved` in message Params. Freeing it lets a future "+
				"field take that number, and a node built before the withdrawal would decode the new field "+
				"as the old one — names live in source, the wire carries numbers.", number, retired.name)

		require.Regexp(t, `(?m)^\s*reserved\b[^;]*"`+regexp.QuoteMeta(retired.name)+`"[^;]*;`, body,
			"the name %q is no longer `reserved` in message Params. Reserving the number alone still "+
				"allows the name to be reused for a different field, which is how a parameter set ends up "+
				"with a familiar name that means something else.", retired.name)
	}
}

// goFieldNameOfProto converts a snake_case proto field name into the Go field name gogoproto generates
// (`miner_vest_blocks` -> `MinerVestBlocks`). It is derived rather than written down twice, so the two
// halves cannot drift apart unnoticed.
func goFieldNameOfProto(name string) string {
	var b strings.Builder
	for _, piece := range strings.Split(name, "_") {
		if piece == "" {
			continue
		}
		b.WriteString(strings.ToUpper(piece[:1]))
		b.WriteString(piece[1:])
	}
	return b.String()
}

// (2) `MinerLocked` is never decremented: a counter that only goes up.
//
// `pools.MinerLocked` accumulates the "locked" share of a miner's payment (`miner_vest_bps`, 30 % by
// default). Nothing releases it: no site decrements it, and the schedule that should have,
// `miner_vest_blocks`, never had a reader and has been withdrawn from the parameter set (case 3). The
// quantity is therefore a historical total, and reading it as a balance awaiting release gives a
// wrong figure that looks like a measurement.
//
// The only write site is `SettleJob`, the authority-gated legacy settlement; the production settlement
// paths do not vest at all. Pinning the full set of sites makes any widening of this mechanism visible.
func TestPoolsMinerLockedStaysACumulativeTotalWithNoRelease(t *testing.T) {
	sites := fieldReaders(t, filepath.Join("..", "x", "jobs"), "MinerLocked")

	files := map[string]int{}
	for _, s := range sites {
		files[filepath.Base(strings.SplitN(s, " : ", 2)[0])]++
		require.NotContains(t, s, "-=",
			"`MinerLocked` is now decremented (%s), so a release mechanism exists. It then needs a "+
				"schedule, and `miner_vest_blocks` is not one: it has no reader.", s)
	}
	require.Equal(t, map[string]int{"msg_server_settle_job.go": 1}, files,
		"the set of sites touching `pools.MinerLocked` has changed: %v.\n"+
			"This counter has a single write, in the authority-gated legacy settlement, and no release. "+
			"Widening its mechanics without a release schedule would grow a quantity that its name "+
			"presents as a restitutable balance.", sites)
}
