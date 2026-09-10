package keeper_test

import (
	"strings"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-037 — CANDIDATE-POOL FREEZE, ACCEPTANCE SUITE.
//
// THE DEFECT PINNED HERE. When the three draws read the LIVE pool, an attacker who knows the revealed
// seed grinds an `id` offline (`sha256(seed|id)` is public), creates a miner, and lands in slot 0 of an
// ALREADY OPEN job with a dust stake — 94% success at 2^24 attempts with 1 of stake against 1,000,000.
// Stake weighting resists the SPLITTING of a stake, never the choice of the identifier. Hashing is
// free; stake is not.
//
// FOUR ACCEPTANCE CONDITIONS (ADR-037 §6), none of which follows from another:
//   (1) one case PER DRAW SITE (3) — three distinct pools, three filters to wire;
//   (2) a GENESIS ROUND-TRIP — the defect that killed the first design is in NO draw site: it lives in
//       their INTERACTION with the import. The three per-site tests would have been green while the
//       entire imported population silently became ineligible;
//   (3) a MISSING anchor must REFUSE — never a silently permissive draw;
//   (4) EQUALITY `registered == freeze` must be ELIGIBLE — that is the case of the WHOLE imported
//       population: this case locks `<=` against a future "simplification" into `<`.

const (
	gvFreeze  = uint64(100)       // job opening height = freeze of its candidate pool
	gvStakeHi = uint64(1_000_000) // the honest miner
	gvStakeLo = uint64(1)         // the attacker: grinding wins WITH A DUST stake
	gvNow     = int64(110)        // the draw happens 10 blocks after the freeze
)

// gvMiner — registers `id` with its stake and its registration height (the two anchors of the predicate).
func gvMiner(t *testing.T, f *fixture, id string, stake, registered uint64) {
	t.Helper()
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: stake}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, registered))
}

// gvJob — an open job, its REVEALED seed (hence public: that is the attack hypothesis) and its freeze
// anchor. `anchor=false` omits the anchor to exercise condition (3).
func gvJob(t *testing.T, f *fixture, jobId string, anchor bool) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, f.keeper.Job.Set(f.ctx, jobId, types.Job{JobId: jobId, State: "open", Fee: 1000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, jobId, types.Beacon{JobId: jobId, Seed: "seed-now-public"}))
	if anchor {
		require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, jobId, gvFreeze))
	}
}

func gvCtx(f *fixture) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(gvNow) }

// --- SITE 1/3: THE PRIMARY DRAW (the one that PAYS — settleOptimistic pays members[0]) ------------
func TestADR037_Primary_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-prim", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)   // registered BEFORE the freeze
	gvMiner(t, f, "grindeur", gvStakeLo, gvFreeze+1) // registered AFTER: the seed was already public

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-prim", keeper.CommitteeSize)
	require.NoError(t, err)
	require.True(t, set["honest"], "a miner registered BEFORE the freeze must stay drawable, otherwise one hole is traded for another")
	require.False(t, set["grindeur"], "an identity created AFTER the seed became public must not enter the PRIMARY committee: it would capture the settlement of an already open job")
}

// --- SITE 2/3: THE AUDIT DRAW (the one that can SLASH an honest miner) ----------------------------
// This site needs its own case precisely because the surrounding VITALITY guard does NOT close this
// hole, and that is deliberate: `eligibleAsJuror` welcomes a NEW miner ("vitality never becomes an
// ENTRY filter"). A case deduced from the primary site would have missed it.
func TestADR037_Audit_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-audit", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)
	gvMiner(t, f, "grindeur", gvStakeLo, gvFreeze+1)

	members, err := f.keeper.DrawAuditCommitteeForTest(gvCtx(f), "audit-seed", "j-audit", "", "")
	require.NoError(t, err)
	require.Contains(t, members, "honest", "a juror registered before the freeze must stay summonable")
	require.NotContains(t, members, "grindeur", "a grinder must not enter the JURY, where it would weigh on a slash verdict")
}

// --- SITE 3/3: THE REDO COMMITTEE (the most exposed: the DISPUTER chooses its height) --------------
func TestADR037_Redo_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-redo", true)
	// The ORIGINAL committee is excluded from the redo pool, so more than 3 incumbents are needed for
	// legitimate candidates to remain; otherwise the case would prove "empty pool", not "grinder excluded".
	for _, id := range []string{"a1", "a2", "a3", "a4", "a5"} {
		gvMiner(t, f, id, gvStakeHi, gvFreeze-1)
	}
	gvMiner(t, f, "grindeur", gvStakeLo, gvFreeze+1)
	// Seed unpredictable to the disputer: without a block hash, anchorRedoCommittee anchors NOTHING
	// (fail closed) and this case would no longer measure the freeze.
	require.NoError(t, f.keeper.BlockHash.Set(f.ctx, gvNow, []byte("hash-de-bloc-de-banc")))

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(gvCtx(f), "j-redo", "", ""))
	anchor, err := f.keeper.AuditCommittee.Get(gvCtx(f), keeper.RedoAnchorKeyForTest("j-redo"))
	require.NoError(t, err, "a redo committee had to be anchored: otherwise this case does not measure the freeze")
	require.NotEmpty(t, strings.TrimSpace(anchor))
	require.NotContains(t, strings.Split(anchor, ","), "grindeur", "the grinder must not sit on the RE-ADJUDICATION committee")
}

// --- THE GENESIS ROUND-TRIP — THE ONLY CASE THAT CATCHES THE REJECTED DESIGN ----------------------
//
// The rejected design (`Job.DisputeHeight` as the anchor) sets `MinerRegisteredHeight = initH` for
// every miner and `DisputeHeight = initH - elapsed` for jobs: `registered <= dispute` then becomes
// FALSE for ALL of them, on EVERY imported disputed job. Empty committees, unbounded deferral, no
// error at all. That defect is in NONE of the three draw sites — it lives in their interaction with
// the import, and the three cases above would have been green.
func TestADR037_GenesisRoundTrip_ImportedMinerStaysEligibleOnImportedJob(t *testing.T) {
	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(500) // restart well above the exported heights

	gen := types.DefaultGenesis()
	importe := registeredMiner(t, 0x11, gvStakeHi)
	gen.MinerMap = []types.Miner{importe}
	// A non-zero `DisputeHeight` AT EXPORT is a count of ELAPSED blocks: this is the case that made the
	// first design wrong, so it is exercised explicitly rather than with a neutral job.
	gen.JobMap = []types.Job{{JobId: "j-importe", State: "open", Fee: 1000, DisputeHeight: 7}}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))
	require.NoError(t, f.keeper.Beacon.Set(ctx, "j-importe", types.Beacon{JobId: "j-importe", Seed: "s"}))

	set, err := f.keeper.AssignedCommitteeForTest(ctx, "j-importe", keeper.CommitteeSize)
	require.NoError(t, err, "an imported job MUST receive its anchor at init: otherwise its three draws are deferred FOREVER")
	require.True(t, set[importe.MinerId], "the whole imported population just became INELIGIBLE on an imported job without a single error: the silent failure described in ADR-037 §3.a")

	// The other half of the property: an identity created AFTER the import stays out.
	gvMiner(t, f, "apres-import", gvStakeHi, 501)
	set2, err := f.keeper.AssignedCommitteeForTest(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(510), "j-importe", keeper.CommitteeSize)
	require.NoError(t, err)
	require.False(t, set2["apres-import"], "the import must not open the door to those arriving after it")
}

// --- A MISSING ANCHOR REFUSES — neither permissive, nor restrictive, nor silent -------------------
func TestADR037_MissingAnchor_TheDrawRefusesInsteadOfAssuming(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-no-anchor", false) // deliberately WITHOUT JobPoolFreezeHeight
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)

	_, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-no-anchor", keeper.CommitteeSize)
	require.Error(t, err, "without an anchor, a PERMISSIVE draw reopens the exact hole, and a RESTRICTIVE draw returns an empty committee that reads like a healthy network")
	require.Contains(t, err.Error(), "pool freeze height MISSING", "the refusal must NAME its cause, otherwise it sends the reader looking elsewhere")
}

// --- EQUALITY IS LOAD-BEARING — a lock against "simplifying" `<=` into `<` ------------------------
// The whole imported population carries `registered == freeze` (both anchors are re-armed to `initH`
// by the same code, at the same block). Switching to `<` would make it ENTIRELY ineligible, on EVERY
// imported job, without a single error — that is, exactly the failure §3.a describes, reintroduced by
// a change that would look like a cleanup.
func TestADR037_Equality_RegisteredEqualsFreezeIsEligible(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-egal", true)
	gvMiner(t, f, "pile-au-gel", gvStakeHi, gvFreeze) // registered == freeze

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-egal", keeper.CommitteeSize)
	require.NoError(t, err)
	require.True(t, set["pile-au-gel"], "`registered == freeze` MUST be eligible: that is the case of the WHOLE imported population")
}

// --- A MINER WITH NO REGISTRATION HEIGHT: EXCLUDED, never included by default ---------------------
// A READ default is never a green light on a security criterion. This also checks that such an entry
// does not FAIL the whole draw: a single abnormal entry would then freeze every committee on the
// network, which would hand out a stop switch.
func TestADR037_MinerWithoutRegistrationHeight_ExcludedWithoutFailingTheDraw(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-sansdate", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)
	// miner set WITHOUT MinerRegisteredHeight (a state no normal path produces)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "no-regheight", types.Miner{MinerId: "no-regheight", Stake: gvStakeHi}))

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-sansdate", keeper.CommitteeSize)
	require.NoError(t, err, "an abnormal entry must not freeze ALL committees")
	require.True(t, set["honest"])
	require.False(t, set["no-regheight"], "a read default is never a green light on a security criterion")
}
