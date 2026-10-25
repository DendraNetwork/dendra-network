package keeper_test

import (
	"strings"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Candidate-pool freeze, acceptance suite.
//
// When the three draws read the live pool, an attacker who knows the revealed seed grinds an `id`
// offline (`sha256(seed|id)` is public), creates a miner, and lands in slot 0 of an already open job
// with a dust stake: 94% success at 2^24 attempts with 1 of stake against 1,000,000. Stake weighting
// resists the splitting of a stake, never the choice of the identifier. Hashing is free; stake is not.
//
// Four acceptance conditions, none of which follows from another:
//   (1) one case per draw site (3): three distinct pools, three filters to wire;
//   (2) a genesis round trip: the defect that killed the first design is in no draw site; it lives in
//       their interaction with the import. The three per-site tests would have been green while the
//       entire imported population silently became ineligible;
//   (3) a missing anchor must refuse, never draw permissively;
//   (4) equality `registered == freeze` must be eligible, the case of the whole imported population:
//       this case locks `<=` against a future "simplification" into `<`.

const (
	gvFreeze  = uint64(100)       // job opening height = freeze of its candidate pool
	gvStakeHi = uint64(1_000_000) // the honest miner
	gvStakeLo = uint64(1)         // the attacker: grinding wins with a dust stake
	gvNow     = int64(110)        // the draw happens 10 blocks after the freeze
)

// gvMiner registers `id` with its stake and its registration height (the two anchors of the predicate).
func gvMiner(t *testing.T, f *fixture, id string, stake, registered uint64) {
	t.Helper()
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: stake}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, registered))
}

// gvJob creates an open job, its revealed seed (hence public, the attack hypothesis) and its freeze
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

// Site 1/3: the primary draw (the one that pays, since settleOptimistic pays members[0]).
func TestADR037_Primary_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-prim", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)  // registered before the freeze
	gvMiner(t, f, "grinder", gvStakeLo, gvFreeze+1) // registered after: the seed was already public

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-prim", keeper.CommitteeSize)
	require.NoError(t, err)
	require.True(t, set["honest"], "a miner registered before the freeze must stay drawable, otherwise one hole is traded for another")
	require.False(t, set["grinder"], "an identity created after the seed became public must not enter the primary committee: it would capture the settlement of an already open job")
}

// Site 2/3: the audit draw (the one that can slash an honest miner).
// This site needs its own case because the surrounding vitality guard does not close this hole, on
// purpose: `eligibleAsJuror` welcomes a new miner ("vitality never becomes an entry filter"). A case
// deduced from the primary site would have missed it.
func TestADR037_Audit_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-audit", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)
	gvMiner(t, f, "grinder", gvStakeLo, gvFreeze+1)

	members, err := f.keeper.DrawAuditCommitteeForTest(gvCtx(f), "audit-seed", "j-audit", "", "")
	require.NoError(t, err)
	require.Contains(t, members, "honest", "a juror registered before the freeze must stay summonable")
	require.NotContains(t, members, "grinder", "a grinder must not enter the jury, where it would weigh on a slash verdict")
}

// Site 3/3: the redo committee (the most exposed, since the disputer chooses its height).
func TestADR037_Redo_IdentityCreatedAfterTheFreezeIsRefused(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-redo", true)
	// The original committee is excluded from the redo pool, so more than 3 incumbents are needed for
	// legitimate candidates to remain; otherwise the case would prove "empty pool", not "grinder excluded".
	for _, id := range []string{"a1", "a2", "a3", "a4", "a5"} {
		gvMiner(t, f, id, gvStakeHi, gvFreeze-1)
	}
	gvMiner(t, f, "grinder", gvStakeLo, gvFreeze+1)
	// Seed unpredictable to the disputer: without a block hash, anchorRedoCommittee anchors nothing
	// (fail closed) and this case would no longer measure the freeze.
	require.NoError(t, f.keeper.BlockHash.Set(f.ctx, gvNow, []byte("the-bench-block-hash")))

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(gvCtx(f), "j-redo", "", ""))
	anchor, err := f.keeper.AuditCommittee.Get(gvCtx(f), keeper.RedoAnchorKeyForTest("j-redo"))
	require.NoError(t, err, "a redo committee had to be anchored: otherwise this case does not measure the freeze")
	require.NotEmpty(t, strings.TrimSpace(anchor))
	require.NotContains(t, strings.Split(anchor, ","), "grinder", "the grinder must not sit on the re-adjudication committee")
}

// The genesis round trip, the only case that catches the rejected design.
//
// The rejected design (`Job.DisputeHeight` as the anchor) sets `MinerRegisteredHeight = initH` for
// every miner and `DisputeHeight = initH - elapsed` for jobs, so `registered <= dispute` becomes false
// for all of them, on every imported disputed job: empty committees, unbounded deferral, no error at
// all. That defect is in none of the three draw sites; it lives in their interaction with the import,
// and the three cases above would have been green.
func TestADR037_GenesisRoundTrip_ImportedMinerStaysEligibleOnImportedJob(t *testing.T) {
	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(500) // restart well above the exported heights

	gen := types.DefaultGenesis()
	imported := registeredMiner(t, 0x11, gvStakeHi)
	gen.MinerMap = []types.Miner{imported}
	// A non-zero `DisputeHeight` at export is a count of elapsed blocks: this is the case that made the
	// first design wrong, so it is exercised explicitly rather than with a neutral job.
	gen.JobMap = []types.Job{{JobId: "j-imported", State: "open", Fee: 1000, DisputeHeight: 7}}
	require.NoError(t, f.keeper.InitGenesis(ctx, *gen))
	require.NoError(t, f.keeper.Beacon.Set(ctx, "j-imported", types.Beacon{JobId: "j-imported", Seed: "s"}))

	set, err := f.keeper.AssignedCommitteeForTest(ctx, "j-imported", keeper.CommitteeSize)
	require.NoError(t, err, "an imported job must receive its anchor at init: otherwise its three draws are deferred forever")
	require.True(t, set[imported.MinerId], "the whole imported population just became ineligible on an imported job without a single error: the silent failure the pool freeze guards against")

	// The other half of the property: an identity created after the import stays out.
	gvMiner(t, f, "after-import", gvStakeHi, 501)
	set2, err := f.keeper.AssignedCommitteeForTest(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(510), "j-imported", keeper.CommitteeSize)
	require.NoError(t, err)
	require.False(t, set2["after-import"], "the import must not open the door to those arriving after it")
}

// A missing anchor refuses: neither permissive, nor restrictive, nor silent.
func TestADR037_MissingAnchor_TheDrawRefusesInsteadOfAssuming(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-no-anchor", false) // deliberately without JobPoolFreezeHeight
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)

	_, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-no-anchor", keeper.CommitteeSize)
	require.Error(t, err, "without an anchor, a permissive draw reopens the exact hole, and a restrictive draw returns an empty committee that reads like a healthy network")
	require.Contains(t, err.Error(), "pool freeze height missing", "the refusal must name its cause, otherwise it sends the reader looking elsewhere")
}

// Equality matters: this locks `<=` against a "simplification" into `<`.
// The whole imported population carries `registered == freeze` (both anchors are re-armed to `initH`
// by the same code, at the same block). Switching to `<` would make it entirely ineligible, on every
// imported job, without a single error: the failure §3.a describes, brought back by a change that
// would look like a cleanup.
func TestADR037_Equality_RegisteredEqualsFreezeIsEligible(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-equal", true)
	gvMiner(t, f, "at-the-freeze", gvStakeHi, gvFreeze) // registered == freeze

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-equal", keeper.CommitteeSize)
	require.NoError(t, err)
	require.True(t, set["at-the-freeze"], "`registered == freeze` must be eligible: that is the case of the whole imported population")
}

// A miner with no registration height is excluded, never included by default.
// A read default is never a green light on a security check. This also checks that such an entry does
// not fail the whole draw: a single abnormal entry would then freeze every committee on the network
// and hand out a stop switch.
func TestADR037_MinerWithoutRegistrationHeight_ExcludedWithoutFailingTheDraw(t *testing.T) {
	f := initFixture(t)
	gvJob(t, f, "j-undated", true)
	gvMiner(t, f, "honest", gvStakeHi, gvFreeze-1)
	// miner set without MinerRegisteredHeight (a state no normal path produces)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "no-regheight", types.Miner{MinerId: "no-regheight", Stake: gvStakeHi}))

	set, err := f.keeper.AssignedCommitteeForTest(gvCtx(f), "j-undated", keeper.CommitteeSize)
	require.NoError(t, err, "an abnormal entry must not freeze all committees")
	require.True(t, set["honest"])
	require.False(t, set["no-regheight"], "a read default is never a green light on a security criterion")
}
