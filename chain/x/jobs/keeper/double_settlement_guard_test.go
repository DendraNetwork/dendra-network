package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// DOUBLE SLASH OF THE k=3 COMMITTEE.
//
// VerifySemantic and FinalizeJob slash the cosine outliers of a k=3 committee. Each one recognised only
// ITS OWN anti-replay marker (`+verified` / `+finalized`), so `settle(k3) -> verify` and then
// `verify -> finalize` re-slashed the SAME outlier (80% of the remainder on each pass). Nothing disabled
// them in OPTIMISTIC mode either, where a permissionless attacker could slash HONEST non-primary miners
// (cosine is dead as a judge there). Two locks: a mode-0 gate plus a UNIFIED anti-replay
// (jobK3Adjudicated).
//
// ⛔ AND FOR MONTHS THIS FILE PROVED NONE OF IT. Every case planted a job with NO registered miner and
// NO commit, so `assignedCommittee` returned an empty set without erroring, the walk found nothing, and
// both handlers refused on "no commit from an assigned miner for this job" — a fallback that has
// nothing to do with either lock. MEASURED, one guard at a time, by replacing its condition with
// `if false`: the mode gate of VerifySemantic, its unified anti-replay, the mode gate of FinalizeJob,
// its unified anti-replay. FOUR mutations, FOUR times the whole file stayed green. A case that asserts
// "X is refused" measures the FIRST refusal on its fixture, not the one it is named after.
//
// So each case now stands up a real committee — three miners, two agreeing commits and one divergent —
// i.e. a fixture on which the handler SUCCEEDS when the guard is absent. And the conclusion is read off
// the outlier's BOND, never off the error: `no commit from an assigned miner`, `no strict majority` and
// the two locks all return the same `ErrInvalidRequest`, so an error assertion cannot tell them apart.
// Stake 1000 intact means the guard refused before touching anything; 200 means it did not.
//
// The last case is the control that makes the other four readable: the same fixture, in the one
// configuration where nothing forbids it, DOES slash. Without it, "the bond is intact" could not be
// told apart from "this fixture never slashes".

// dsComite plants a full k=3 committee on `jobID`: two agreeing commits and one divergent, the shape
// both handlers judge. Returns the outlier's id. Uses gvSetMiner/gvSetJob rather than the raw
// collections because those sow `MinerRegisteredHeight` and `JobPoolFreezeHeight` — without them the
// frozen-pool and vitality predicates empty the committee and the fallback refusal returns.
func dsComite(t *testing.T, f *fixture, jobID, etat string) string {
	t.Helper()
	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: ssAddr20(t, f, "ds-creator-"+id),
			Operator: ssAddr20(t, f, "ds-operator-"+id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, State: etat, Fee: 10000, Client: ssAddr20(t, f, "ds-client-"+jobID),
	}))
	return "mc"
}

// dsIntact asserts the outlier was not touched — the only reading that separates this guard from the
// three other refusals the same handler can return.
func dsIntact(t *testing.T, f *fixture, id, quoi string) {
	t.Helper()
	m, err := f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake,
		"%s: the refusal must land BEFORE any bond moves (200 would mean the handler judged)", quoi)
}

func dsCreator(f *fixture, t *testing.T) string {
	t.Helper()
	c, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("declencheur_ds_2107_")))
	require.NoError(t, err)
	return c
}

// (1) OPTIMISTIC MODE: verify-semantic is REFUSED (cosine plays no role; the VRF audit does the job).
func TestVerifySemanticRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.CommitteeRevealDelay = 1 // Validate refuses mode 1 with a zero reveal delay
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsComite(t, f, "jVS", "open")

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jVS"})
	require.Error(t, err, "verify-semantic must be REFUSED in optimistic mode (griefing of honest non-primary miners)")
	dsIntact(t, f, outlier, "verify in optimistic mode")
}

// (2) ANTI-DOUBLE-SLASH: a job that is ALREADY settled (paid+finalized) can no longer be re-slashed by
// verify-semantic (mode 0). The guard used to test only `+verified`, so settle then verify re-slashed.
func TestVerifySemanticRefusedOnAlreadySettledJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsComite(t, f, "jPaid", "open+paid+finalized")

	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jPaid"})
	require.Error(t, err, "a k=3 committee already adjudicated (settled/verified/finalized) must not be re-slashed")
	dsIntact(t, f, outlier, "settle -> verify")
}

// (3) SYMMETRY: finalize-job shares both locks.
func TestFinalizeJobRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.CommitteeRevealDelay = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	srv := keeper.NewMsgServerImpl(f.keeper)
	outlier := dsComite(t, f, "jFJ", "open")

	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jFJ"})
	require.Error(t, err, "finalize-job must be REFUSED in optimistic mode (same griefing as verify-semantic)")
	dsIntact(t, f, outlier, "finalize in optimistic mode")
}

func TestFinalizeJobRefusedOnAlreadyVerifiedJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	srv := keeper.NewMsgServerImpl(f.keeper)
	// Job already VERIFIED (the k=3 slash was applied by verify) -> finalize must not re-slash.
	outlier := dsComite(t, f, "jVerif", "open+verified")

	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jVerif"})
	require.Error(t, err, "verify -> finalize must not re-slash the same outlier (unified anti-replay)")
	dsIntact(t, f, outlier, "verify -> finalize")
}

// (5) THE CONTROL, and the reason the four above mean anything: the SAME fixture, mode 0, on a job no
// lock applies to. Both handlers judge it and the outlier loses slash_leak_bps of its bond. If this
// case ever goes red, the four refusals above stop proving anything at all — they would be measuring a
// committee that cannot be judged rather than a guard that refuses to judge it.
func TestTheSameFixtureIsReallyJudgedWhenNoLockApplies(t *testing.T) {
	p := types.DefaultParams()
	attendu := uint64(1000) - uint64(1000)*p.SlashLeakBps/10000

	t.Run("verify", func(t *testing.T) {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		srv := keeper.NewMsgServerImpl(f.keeper)
		outlier := dsComite(t, f, "jOkV", "open")

		_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: dsCreator(f, t), JobId: "jOkV"})
		require.NoError(t, err, "mode 0 on a clean job: nothing forbids the judgement")
		m, err := f.keeper.Miner.Get(f.ctx, outlier)
		require.NoError(t, err)
		require.Equal(t, attendu, m.Stake, "the outlier IS slashed here, so the fixture is a success case")
	})

	t.Run("finalize", func(t *testing.T) {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		srv := keeper.NewMsgServerImpl(f.keeper)
		outlier := dsComite(t, f, "jOkF", "open")

		_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: dsCreator(f, t), JobId: "jOkF"})
		require.NoError(t, err, "mode 0 on a clean job: nothing forbids the verdict")
		m, err := f.keeper.Miner.Get(f.ctx, outlier)
		require.NoError(t, err)
		require.Equal(t, attendu, m.Stake, "same for finalize -- both handlers really do slash this fixture")
	})
}
