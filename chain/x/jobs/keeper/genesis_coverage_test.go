package keeper_test

import (
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
)

// Every keeper collection is classified: carried, re-anchored on import, or ephemeral.
//
// The round-trip test writes into every collection it knows, exports, imports again and compares, so
// it catches a field forgotten in `ExportGenesis` or `InitGenesis`. The property that matters is
// broader: a collection can be added to the keeper without ever being wired into genesis, and nothing
// says so. A hand-written list cannot see that, because a collection added tomorrow is not on it and
// the test stays green by ignoring it. That is how the original hole appeared (a GenesisState stopping
// at field 6 while the keeper held 23), and it could happen again in the same way.
//
// So the list is derived instead of maintained. Reflection enumerates the collections actually
// declared in `Keeper`, and each must appear in the table below with a decision. Adding a collection
// without classifying it turns this test red, at the moment the person adding it knows why it exists,
// the only moment the answer is known.
//
// The table works both ways: an entry that no longer matches any collection is red too, otherwise the
// table fills with dead names that give the illusion of coverage.
//
// Each entry below was checked against `genesis.go` and against its readers one by one, and none is
// lost silently. The wiring was never the problem; the problem was that nothing forced it to stay.

type collectionClass int

const (
	carried    collectionClass = iota // exported and re-imported by genesis.go
	reanchored                        // not exported, but re-armed on import; a reason is mandatory
	ephemeral                         // not exported and not touched on import; a reason is mandatory
)

// reasonTooShort is one message for both classes that require a justification; twin messages would
// eventually diverge.
const reasonTooShort = "%q is declared without transport, with a reason of %d characters. " +
	"A reason one does not write down is a reason one did not have: this field, and this field alone, " +
	"distinguishes a decision from an oversight"

type decision struct {
	class  collectionClass
	reason string
}

// coverage is the decision table: one line per keeper collection, without exception.
var coverage = map[string]decision{
	"Params":             {carried, ""},
	"Miner":              {carried, ""},
	"Job":                {carried, ""},
	"Pools":              {carried, ""},
	"Commit":             {carried, ""},
	"Beacon":             {carried, ""},
	"PendingReveal":      {carried, ""},
	"AvailChallenge":     {carried, ""},
	"ValidatorVrfPubkey": {carried, ""},
	"AuditCommittee":     {carried, ""},
	// A work committee that did not survive the export would be derived again on import,
	// from the stake of that moment, which is the defect the anchoring closes, back on the first
	// restart. Carried, like its jury twin.
	"WorkCommittee":        {carried, ""},
	"PendingAudit":         {carried, ""},
	"PendingJobExpiry":     {carried, ""},
	"MinerOptimisticCount": {carried, ""},
	"AuditSelected":        {carried, ""},
	"PendingHeldRelease":   {carried, ""},
	"AuditsOpened":         {carried, ""},
	"HeldSince":            {carried, ""},
	"CommitteeSince":       {carried, ""},
	"PendingAuditResolve":  {carried, ""},
	"PendingAppealResolve": {carried, ""},
	"HeldFee":              {carried, ""},
	"HeldBurn":             {carried, ""},
	// Coins the module holds per job, and a subsidy right a miner earned. Carried
	// like the retention and the deferred burn they sit beside.
	"HeldValidatorShare":   {carried, ""},
	"HeldTeamShare":        {carried, ""},
	"HeldTreasuryShare":    {carried, ""},
	"HeldDemand":           {carried, ""},
	"AvailFailCount":       {carried, ""},
	"AvailFailWindowStart": {carried, ""},
	// Epoch 15: the jury pool (a signed declaration) and the missed-draw counts travel; the two
	// heights of the judge state are handled below, one re-anchored and one ephemeral.
	"MinerJudge":       {carried, ""},
	"MinerMissedDraws": {carried, ""},

	// Re-anchored: not exported, but posted again on import, otherwise the guard they serve dies.
	//
	// This third class came from the first red of this test. The table said "carried" for these two, and
	// the check answered "re-imported but never exported". The table was wrong, not the code: the manual
	// grep that had filled it counted occurrences across the whole file, so it read the `Get` and `Set` of
	// `InitGenesis` as an export plus an import. A two-class model did not describe reality: "lost
	// silently" and "deliberately re-armed" are different things, and confusing them is what let the
	// eviction defect through in the first place.
	"MinerRegisteredHeight": {reanchored, "Not exported; adding it would require a proto regeneration. An " +
		"imported miner therefore arrived without a registration height, and `pruneInactiveMiners` never touches a " +
		"miner without one: the whole imported population became unevictable for life, the eviction guard silently " +
		"disabled by the migration. `InitGenesis` re-posts it at the import height, giving a fresh grace window and " +
		"a re-armed guard."},
	"JobPoolFreezeHeight": {reanchored, "The same pattern as MinerRegisteredHeight, deliberately so: two " +
		"anchors of the same shape are handled together. Not exported, and its absence is an error for " +
		"`drawAuditCommittee` and `assignedCommitteeOrdered` — without re-anchoring, every job predating the import " +
		"would see its draw fail, hence neither audit nor payment. `InitGenesis` re-posts it at the import height."},
	"MinerJudgeDeclaredHeight": {reanchored, "Epoch 15. The start of a judge's trial window, on the previous " +
		"chain's scale. Not exported: `InitGenesis` re-dates every imported judge at the import anchor, like the " +
		"registration height it mirrors, so an imported judge gets a fresh trial window instead of being excluded on a " +
		"scale that no longer exists. Carrying the raw height would leave every jury empty after a migration."},

	// Ephemeral: the reason is the substance of the test.
	"Available": {ephemeral, "keyed by (height, miner) on the previous chain. Heights restart at 1 after a reset, so " +
		"carrying a past availability window over would leave it either already expired or open for a million blocks. " +
		"The availability challenge is replayed after the restart; it is not migrated."},
	"DecentralizedSeed": {ephemeral, "VRF seed indexed by height on the previous chain. Re-importing it would supply " +
		"seeds for heights that no longer exist and, worse, publish in advance the seeds the new network will reach: " +
		"a carried seed is a predictable seed."},
	"DecentralizedSeedContributors": {ephemeral, "contributor count per height, same reason as the seed it accompanies: " +
		"without the corresponding heights it describes nothing."},
	"DecentralizedSeedContributorPower": {ephemeral, "contributor power per height; twin of the counter above, carried " +
		"or not exactly with it, since two tables of the same shape are handled together."},
	"AvailChallengeHeight": {ephemeral, "the height whose EndBlock rolled the current challenge, on the previous " +
		"chain's scale. Not carried: `availChallengeEpoch` reads its absence as no roll at this height, which is " +
		"exact for an imported challenge, since it was rolled before the import height. A proof in a boundary block " +
		"before the first roll therefore counts for the epoch that block closes, and the first roll writes the entry."},
	"BlockHash": {ephemeral, "block hashes of the previous chain, indexed by height. Meaningless after a reset, and " +
		"their retention is already bounded by a sliding window in the EndBlocker."},
	"MinerLastPresentEpoch": {ephemeral, "item 5. The last availability epoch a miner proved, on the previous " +
		"chain's scale: epochs are heights divided by avail_epoch_blocks, and heights restart after a reset. Not " +
		"carried: `minerPresentAt` falls back to `MinerRegisteredHeight`, which is re-anchored at the import height, " +
		"so every imported miner gets the newcomer's grace (the current and the next window to prove presence) " +
		"instead of being judged absent on a scale that no longer exists."},
	"MinerLastCommitHeight": {ephemeral, "height of the last commit, on the previous chain's scale. Deliberately not " +
		"carried: `eligibleAsJuror` handles its absence by falling back to `MinerRegisteredHeight`, which is " +
		"re-anchored at the import height, so the imported population receives a fresh grace window " +
		"instead of being judged on a scale that no longer exists. Carrying the raw value would evict everyone at " +
		"once; exporting it as an elapsed duration would be defensible, but that is a choice to make, not an oversight."},
	"MinerLastVerdictHeight": {ephemeral, "Epoch 15. The height of a judge's last answer, on the previous " +
		"chain's scale. Not carried: `eligibleAsJudgeAt` reads the declaration height when no answer is dated, and the " +
		"import re-dates that one at the anchor, so every imported judge is a candidate for a full trial window and " +
		"nobody is judged on a height the new chain never had. The same shape as MinerLastCommitHeight, on purpose."},
}

// mentions reports whether `genesis.go` touches the collection named `name`.
//
// A plain `strings.Contains(code, "k."+name+".")` would not do, because that form assumes the code
// calls a method on the collection (`k.HeldFee.Get(...)`). `x/emission` passes the collection by value
// to an adapter instead (`get(k.Reserve)`), and a test written on the first form would conclude
// "genesis.go never touches it" about a correctly wired module. The grep that filled this table made
// that mistake twice, and a guard that produces a false red gets disarmed as fast as a false green
// empties it.
// So `k.<Name>` followed by any non-identifier character (`.`, `)`, `,`, `}`) counts, which covers
// both styles without confusing `k.HeldFee` with `k.HeldFeeSomethingElse`.
func mentions(code, name string) bool {
	target := "k." + name
	for i := 0; ; {
		j := strings.Index(code[i:], target)
		if j < 0 {
			return false
		}
		end := i + j + len(target)
		if end >= len(code) {
			return true
		}
		c := code[end]
		isIdent := c == '_' || (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
		if !isIdent {
			return true
		}
		i = end
	}
}

// keeperCollections returns the authoritative list, derived from the type instead of copied.
func keeperCollections() []string {
	ty := reflect.TypeOf(keeper.Keeper{})
	out := []string{}
	for i := 0; i < ty.NumField(); i++ {
		f := ty.Field(i)
		// `collections.Schema` is the descriptor that registers the stores, and holds none itself.
		if f.Name == "Schema" {
			continue
		}
		if strings.HasPrefix(f.Type.String(), "collections.") {
			out = append(out, f.Name)
		}
	}
	return out
}

// (0) The matcher proves itself before it is allowed to judge.
//
// Tests (2) and (3) rest entirely on `mentions`. A realistic sabotage, rewriting `genesis.go` in the
// other style, does not compile: it introduces an unknown identifier, the package stops building, and
// the test measures nothing, since a compilation failure is not a guard failure. The property belongs
// to the matcher, so the matcher is exercised directly, on strings.
func TestGenesisCoverageMatcherSeesBothStyles(t *testing.T) {
	require.True(t, mentions("if err := k.HeldFee.Walk(ctx, nil, f); err != nil {", "HeldFee"),
		"method call style (x/jobs): `k.X.` must count")
	require.True(t, mentions("Reserve: get(k.HeldFee),", "HeldFee"),
		"by-value style (x/emission: `get(k.Reserve)`): without it a correctly wired module would be "+
			"declared unwired, which is the mistake the grep that filled this table made")
	require.True(t, mentions("x := []C{k.HeldFee, k.HeldBurn}", "HeldFee"), "followed by a comma")
	require.True(t, mentions("return k.HeldFee", "HeldFee"), "at the very end of the source")

	require.False(t, mentions("k.HeldFeeOther.Set(ctx, v)", "HeldFee"),
		"neighbouring prefix: `k.HeldFeeOther` must not count as `k.HeldFee`, otherwise an unwired "+
			"collection would pass on the back of its neighbour")
	require.False(t, mentions("k.HeldBurn.Get(ctx)", "HeldFee"), "another collection does not count")
	require.False(t, mentions("// a comment mentioning HeldFee without the k.", "HeldFee"),
		"a mention in prose is not a wiring")
}

// (1) No unclassified collection, and no dead entry. Both directions count.
func TestGenesisCoverageEveryCollectionClassified(t *testing.T) {
	actual := keeperCollections()
	require.GreaterOrEqual(t, len(actual), 25,
		"reflection sees almost nothing: the type filter is broken and this test would prove nothing")

	seen := map[string]bool{}
	for _, name := range actual {
		seen[name] = true
		_, ok := coverage[name]
		require.True(t, ok,
			"unclassified collection: %q is declared in Keeper but absent from the coverage table. "+
				"Decide now: carried by genesis, or ephemeral with a written reason. Without that decision, "+
				"an export/import will lose it in silence, as the original defect lost seventeen of them.", name)
	}
	for name := range coverage {
		require.True(t, seen[name],
			"stale entry: %q is classified but no longer exists in Keeper. A table that keeps dead names "+
				"gives the illusion of a coverage it no longer has", name)
	}
}

// (2) The classification matches the real wiring. A decision that does not match the code is worse
// than a missing one, because it reassures, so each line is compared with `genesis.go`.
//
// This test reads the source on purpose: genesis wiring is a structural property ("is this collection
// wired at all?"), not a value property. The neighbouring round trip proves that values survive; this
// one proves that no collection escapes the question.
func TestGenesisCoverageClassificationMatchesTheCode(t *testing.T) {
	src, err := os.ReadFile("genesis.go")
	require.NoError(t, err, "genesis.go must be readable from the package, otherwise this check measures nothing")
	code := string(src)

	for _, name := range keeperCollections() {
		d, ok := coverage[name]
		if !ok {
			continue // already reported red by test (1); do not double the noise
		}
		mentioned := mentions(code, name)
		switch d.class {
		case carried:
			require.True(t, mentioned,
				"%q is classified carried but genesis.go never touches it: the table lies, and an "+
					"export/import loses it silently", name)
		case reanchored:
			require.True(t, mentioned,
				"%q is classified re-anchored but genesis.go does not touch it: the guard it serves is dead "+
					"after the first import, with no error raised", name)
			require.GreaterOrEqual(t, len(d.reason), 80, reasonTooShort, name, len(d.reason))
		case ephemeral:
			require.False(t, mentioned,
				"%q is classified ephemeral but genesis.go manipulates it: either the code carries it, in "+
					"which case classify it carried or re-anchored, or the mention is a leftover. Either way the table lies", name)
			require.GreaterOrEqual(t, len(d.reason), 80, reasonTooShort, name, len(d.reason))
		}
	}
}

// (3) Carried collections are carried both ways. Exporting without importing again loses the data;
// importing without exporting replaces it with emptiness. A single wired direction is half a hole,
// and the harder half to see, because the other half reassures.
func TestGenesisCoverageRoundTripsBothWays(t *testing.T) {
	src, err := os.ReadFile("genesis.go")
	require.NoError(t, err)
	code := string(src)

	// `genesis.go` holds InitGenesis and then ExportGenesis: cut at the boundary and question each half
	// separately. Searching the whole file would confuse "exported" with "imported", the error this test
	// exists to catch.
	i := strings.Index(code, "func (k Keeper) ExportGenesis")
	require.Greater(t, i, 0, "InitGenesis/ExportGenesis boundary not found: the split below would measure nothing")
	importPart, exportPart := code[:i], code[i:]

	for name, d := range coverage {
		if d.class == reanchored {
			// A re-anchored collection must be posted on import and absent from the export: that pair is what
			// distinguishes it from a half-wired carried collection. Without the second half, "re-anchored"
			// would just be a label on an oversight.
			require.True(t, mentions(importPart, name),
				"%q is declared re-anchored but the import does not post it", name)
			require.False(t, mentions(exportPart, name),
				"%q is exported: it is no longer re-anchored, it is carried — reclassify it", name)
			continue
		}
		if d.class != carried {
			continue
		}
		require.True(t, mentions(importPart, name),
			"%q is exported but never re-imported: the state leaves in the file and never comes back", name)
		require.True(t, mentions(exportPart, name),
			"%q is re-imported but never exported: the produced genesis does not contain it, so the "+
				"import writes emptiness over a state that existed", name)
	}
}
