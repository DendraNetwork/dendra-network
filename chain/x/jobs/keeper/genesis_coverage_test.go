package keeper_test

import (
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
)

// ═══ EVERY KEEPER COLLECTION IS CLASSIFIED: CARRIED · RE-ANCHORED ON IMPORT · EPHEMERAL ══════════
//
// What this test closes, and why the round-trip test does not close it.
//
// The existing round-trip writes into every collection it KNOWS, exports, re-imports and compares:
// it catches a field forgotten in `ExportGenesis` or in `InitGenesis`. But the property that matters
// is broader — a collection can be added to the keeper without ever being wired into genesis, and
// nothing says so — and that is exactly what a hand-written list cannot see: a collection added
// tomorrow is not on it, so the test stays GREEN by ignoring it. A guard written for a list protects
// only that list. That is the failure that produced the original hole (a GenesisState stopping at
// field 6 while the keeper held 23), and it can recur identically.
//
// The principle: the list is no longer maintained, it is DERIVED. Reflection enumerates the
// collections actually declared in `Keeper`, and each must appear in the table below with a
// decision. Adding a collection without classifying it TURNS THIS TEST RED — at the exact moment the
// person adding it knows why it exists, the only moment the answer is known.
//
// The table is BIDIRECTIONAL: an entry that no longer matches any collection is red too. Otherwise
// the table becomes a graveyard of dead names that gives the illusion of coverage.
//
// Each entry below was checked against `genesis.go` and against its readers one by one: none is lost
// silently. The defect was never in the wiring — it was that nothing OBLIGED the wiring to stay.

type classeCollection int

const (
	transportee classeCollection = iota // exported AND re-imported by genesis.go
	reancree                            // NOT exported, but RE-ARMED on import; a reason is mandatory
	ephemere                            // NOT exported and NOT touched on import; a reason is mandatory
)

// raisonTropCourte — one message for both classes that require a justification. Twin messages would
// eventually diverge, and that is the most frequent failure mode in this file.
const raisonTropCourte = "%q is declared without transport, with a reason of %d characters. " +
	"A reason one does not write down is a reason one did not have: this field, and this field alone, " +
	"distinguishes a decision from an oversight"

type decision struct {
	classe classeCollection
	raison string
}

// couverture — THE decision table. One line per keeper collection, without exception.
var couverture = map[string]decision{
	"Params":               {transportee, ""},
	"Miner":                {transportee, ""},
	"Job":                  {transportee, ""},
	"Pools":                {transportee, ""},
	"Commit":               {transportee, ""},
	"Beacon":               {transportee, ""},
	"PendingReveal":        {transportee, ""},
	"AvailChallenge":       {transportee, ""},
	"ValidatorVrfPubkey":   {transportee, ""},
	"AuditCommittee":       {transportee, ""},
	"PendingAudit":         {transportee, ""},
	"MinerOptimisticCount": {transportee, ""},
	"AuditSelected":        {transportee, ""},
	"PendingHeldRelease":   {transportee, ""},
	"AuditsOpened":         {transportee, ""},
	"HeldSince":            {transportee, ""},
	"CommitteeSince":       {transportee, ""},
	"PendingAuditResolve":  {transportee, ""},
	"PendingAppealResolve": {transportee, ""},
	"HeldFee":              {transportee, ""},
	"HeldBurn":             {transportee, ""},
	"AvailFailCount":       {transportee, ""},
	"AvailFailWindowStart": {transportee, ""},

	// ─── RE-ANCHORED: not exported, but RE-POSTED on import — otherwise the guard they serve DIES ──
	//
	// This third class was born from the first red of this test. The table said "carried" for these
	// two; the check answered "re-imported but never exported". The TABLE was wrong, not the code —
	// and the manual grep that had filled it counted occurrences across the WHOLE file, so it read the
	// `Get` and `Set` of `InitGenesis` as an export plus an import. The first red of a new instrument
	// is read before it is believed: here it was right, and it said that a two-class model did not
	// describe reality. "Lost in silence" and "deliberately re-armed" are not the same thing, and
	// conflating them is precisely what let the eviction defect through in the first place.
	"MinerRegisteredHeight": {reancree, "ADR-034 M7. Not exported; adding it would require a proto regeneration. An " +
		"imported miner therefore arrived WITHOUT a registration height, and `pruneInactiveMiners` never touches a " +
		"miner without one: the whole imported population became UNEVICTABLE FOR LIFE, the eviction guard silently " +
		"disabled by the migration. `InitGenesis` re-posts it at the import height, giving a FRESH grace window and " +
		"a re-armed guard."},
	"JobPoolFreezeHeight": {reancree, "ADR-037 §2.5, the SAME PATTERN as MinerRegisteredHeight, deliberately so: two " +
		"anchors of the same shape are handled together. Not exported, and its ABSENCE is an ERROR for " +
		"`drawAuditCommittee` and `assignedCommitteeOrdered` — without re-anchoring, every job predating the import " +
		"would see its draw FAIL, hence neither audit nor payment. `InitGenesis` re-posts it at the import height."},

	// ─── EPHEMERAL: the reason is the substance of the test, not a decorative comment ──────────────
	"Available": {ephemere, "keyed by (height, miner) on the PREVIOUS chain. Heights restart at 1 after a reset, so " +
		"carrying a past availability window over would leave it either already expired or open for a million blocks. " +
		"The availability challenge is replayed after the restart; it is not migrated."},
	"DecentralizedSeed": {ephemere, "VRF seed INDEXED BY HEIGHT on the previous chain. Re-importing it would supply " +
		"seeds for heights that no longer exist and, worse, publish in advance the seeds the new network will reach: " +
		"a carried seed is a PREDICTABLE seed."},
	"DecentralizedSeedContributors": {ephemere, "contributor count per height, same reason as the seed it accompanies: " +
		"without the corresponding heights it describes nothing."},
	"DecentralizedSeedContributorPower": {ephemere, "contributor power per height; twin of the counter above, carried " +
		"or not EXACTLY with it, since two tables of the same shape are handled together."},
	"BlockHash": {ephemere, "block hashes of the previous chain, indexed by height. Meaningless after a reset, and " +
		"their retention is already bounded by a sliding window in the EndBlocker."},
	"MinerLastCommitHeight": {ephemere, "height of the last commit, on the PREVIOUS chain's scale. Deliberately not " +
		"carried: `eligibleAsJuror` handles its absence by falling back to `MinerRegisteredHeight`, which IS " +
		"re-anchored at the import height (ADR-034 M7), so the imported population receives a FRESH grace window " +
		"instead of being judged on a scale that no longer exists. Carrying the raw value would evict everyone at " +
		"once; exporting it as an ELAPSED duration would be defensible, but that is a choice to make, not an oversight."},
}

// mentionne reports whether `genesis.go` touches the collection named `nom`.
//
// A plain `strings.Contains(code, "k."+nom+".")` would not do. That form assumes the code CALLS a
// method on the collection (`k.HeldFee.Get(...)`). `x/emission` does it differently: it passes the
// collection BY VALUE to an adapter — `get(k.Reserve)` — and a test written on the first form would
// conclude "genesis.go never touches it" about a perfectly wired module. This is not hypothetical:
// it is the mistake the grep that filled this table made twice, and a guard that produces a FALSE
// RED gets disarmed as fast as a false green empties it.
// So `k.<Name>` followed by ANY non-identifier character — `.`, `)`, `,`, `}` — counts, which covers
// both styles without confusing `k.HeldFee` with `k.HeldFeeSomethingElse`.
func mentionne(code, nom string) bool {
	cible := "k." + nom
	for i := 0; ; {
		j := strings.Index(code[i:], cible)
		if j < 0 {
			return false
		}
		fin := i + j + len(cible)
		if fin >= len(code) {
			return true
		}
		c := code[fin]
		estIdent := c == '_' || (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
		if !estIdent {
			return true
		}
		i = fin
	}
}

// collectionsDuKeeper — the AUTHORITATIVE list, derived from the type instead of copied.
func collectionsDuKeeper() []string {
	ty := reflect.TypeOf(keeper.Keeper{})
	out := []string{}
	for i := 0; i < ty.NumField(); i++ {
		f := ty.Field(i)
		// `collections.Schema` is not a store: it is the descriptor that registers them.
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
// Tests (2) and (3) rest entirely on `mentionne`. A "realistic" sabotage — rewriting `genesis.go` in
// the other style — DOES NOT COMPILE: it introduces an unknown identifier, the package stops
// building, and the test measures NOTHING. A compilation red is not a guard red. The property
// belongs to the matcher, so the matcher is exercised directly, on strings.
func TestGenesisCouvertureMatcherVoitLesDeuxStyles(t *testing.T) {
	require.True(t, mentionne("if err := k.HeldFee.Walk(ctx, nil, f); err != nil {", "HeldFee"),
		"METHOD CALL style (x/jobs): `k.X.` must count")
	require.True(t, mentionne("Reserve: get(k.HeldFee),", "HeldFee"),
		"BY-VALUE style (x/emission: `get(k.Reserve)`): without it a correctly wired module would be "+
			"declared unwired, which is the mistake the grep that filled this table made")
	require.True(t, mentionne("x := []C{k.HeldFee, k.HeldBurn}", "HeldFee"), "followed by a comma")
	require.True(t, mentionne("return k.HeldFee", "HeldFee"), "at the very end of the source")

	require.False(t, mentionne("k.HeldFeeAutre.Set(ctx, v)", "HeldFee"),
		"NEIGHBOURING PREFIX: `k.HeldFeeAutre` must NOT count as `k.HeldFee`, otherwise an unwired "+
			"collection would pass on the back of its neighbour")
	require.False(t, mentionne("k.HeldBurn.Get(ctx)", "HeldFee"), "another collection does not count")
	require.False(t, mentionne("// a comment mentioning HeldFee without the k.", "HeldFee"),
		"a mention in prose is not a wiring")
}

// (1) NO UNCLASSIFIED COLLECTION, AND NO DEAD ENTRY. Both directions count.
func TestGenesisCouvertureToutesCollectionsClassees(t *testing.T) {
	reelles := collectionsDuKeeper()
	require.GreaterOrEqual(t, len(reelles), 25,
		"reflection sees almost nothing: the type filter is broken and this test would prove nothing")

	vues := map[string]bool{}
	for _, nom := range reelles {
		vues[nom] = true
		_, ok := couverture[nom]
		require.True(t, ok,
			"UNCLASSIFIED COLLECTION: %q is declared in Keeper but absent from the coverage table. "+
				"Decide NOW: carried by genesis, or ephemeral with a written reason. Without that decision, "+
				"an export/import will lose it IN SILENCE, as the original defect lost seventeen of them.", nom)
	}
	for nom := range couverture {
		require.True(t, vues[nom],
			"STALE entry: %q is classified but no longer exists in Keeper. A table that keeps dead names "+
				"gives the illusion of a coverage it no longer has", nom)
	}
}

// (2) THE CLASSIFICATION TELLS THE TRUTH ABOUT THE REAL WIRING. A decision that does not match the
// code is worse than a missing one: it reassures. Each line is therefore confronted with `genesis.go`.
//
// This test reads the SOURCE, deliberately: genesis wiring is a STRUCTURAL property ("is this
// collection wired at all?"), not a value property. The neighbouring round-trip proves that values
// survive; this one proves that no collection escapes the question.
func TestGenesisCouvertureClassificationConformeAuCode(t *testing.T) {
	src, err := os.ReadFile("genesis.go")
	require.NoError(t, err, "genesis.go must be readable from the package, otherwise this check measures nothing")
	code := string(src)

	for _, nom := range collectionsDuKeeper() {
		d, ok := couverture[nom]
		if !ok {
			continue // already reported red by test (1); do not double the noise
		}
		mentionnee := mentionne(code, nom)
		switch d.classe {
		case transportee:
			require.True(t, mentionnee,
				"%q is classified CARRIED but genesis.go never touches it: the table LIES, and an "+
					"export/import loses it silently", nom)
		case reancree:
			require.True(t, mentionnee,
				"%q is classified RE-ANCHORED but genesis.go does not touch it: the guard it serves is DEAD "+
					"after the first import, with no error raised", nom)
			require.GreaterOrEqual(t, len(d.raison), 80, raisonTropCourte, nom, len(d.raison))
		case ephemere:
			require.False(t, mentionnee,
				"%q is classified EPHEMERAL but genesis.go manipulates it: either the code carries it, in "+
					"which case classify it CARRIED or RE-ANCHORED, or the mention is a leftover. Either way the table lies", nom)
			require.GreaterOrEqual(t, len(d.raison), 80, raisonTropCourte, nom, len(d.raison))
		}
	}
}

// (3) CARRIED COLLECTIONS ARE CARRIED BOTH WAYS. Exporting without re-importing loses the data;
// re-importing without exporting replaces it with emptiness. A single wired direction is half a
// hole, and it is the half that is hardest to see, because the other half reassures.
func TestGenesisCouvertureTransportBidirectionnel(t *testing.T) {
	src, err := os.ReadFile("genesis.go")
	require.NoError(t, err)
	code := string(src)

	// `genesis.go` holds InitGenesis and THEN ExportGenesis: cut at the boundary and question each
	// half separately. Searching the whole file would conflate "exported" with "imported" — exactly
	// the error this test exists to catch.
	i := strings.Index(code, "func (k Keeper) ExportGenesis")
	require.Greater(t, i, 0, "InitGenesis/ExportGenesis boundary not found: the split below would measure nothing")
	partieImport, partieExport := code[:i], code[i:]

	for nom, d := range couverture {
		if d.classe == reancree {
			// A re-anchored collection must be posted on IMPORT and absent from the EXPORT: that pair is
			// what distinguishes it from a half-wired carried collection. Without the second half,
			// "re-anchored" would just be a label stuck on an oversight.
			require.True(t, mentionne(partieImport, nom),
				"%q is declared RE-ANCHORED but the import does not post it", nom)
			require.False(t, mentionne(partieExport, nom),
				"%q is exported: it is no longer re-anchored, it is carried — reclassify it", nom)
			continue
		}
		if d.classe != transportee {
			continue
		}
		require.True(t, mentionne(partieImport, nom),
			"%q is exported but NEVER re-imported: the state leaves in the file and never comes back", nom)
		require.True(t, mentionne(partieExport, nom),
			"%q is re-imported but NEVER exported: the produced genesis does not contain it, so the "+
				"import writes emptiness over a state that existed", nom)
	}
}
