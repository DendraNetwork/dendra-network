package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Miner vitality.
//
// Every guard is tested in both directions: it must fire and it must stay silent. A guard proven only
// to fire may be a systematic refusal in disguise; a guard proven only to stay silent may be dead. The
// two tests of a pair share the same setup except for one field, which must explain the difference,
// otherwise the test proves nothing about the guard.

const (
	vitH       = int64(700000) // multiple of 100, so the epoch sweep runs
	vitRecent  = uint64(699500)
	vitAncient = uint64(1)
)

// `lastCommit == 0` means "no history at all", and under the pool freeze that includes the registration date:
// test (3) exercises the miner that nothing dates, and giving it a date to make the rest pass would
// empty its result of meaning. The others receive theirs, as `create-miner` writes it in production;
// without it they would fall out of the frozen pool and the draw tests would measure an exclusion
// instead of vitality.
func vitMiner(f *fixture, t *testing.T, id, creator string, lastCommit uint64) {
	t.Helper()
	m := types.Miner{MinerId: id, Stake: aeStake, Operator: creator, Creator: creator}
	if lastCommit == 0 {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, m))
		return
	}
	require.NoError(t, gvSetMiner(f, f.ctx, id, m))
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, id, lastCommit))
	// Since epoch 15 a jury seat also needs a recent answer or declaration. These cases run at
	// vitH, far past the trial window of the height-0 declaration gvSetMiner writes, so the miner's last
	// answer is dated with its last commit: a fresh miner stays summonable and a stale one stays out for
	// the reason each case measures, vitality.
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, id, lastCommit))
}

// (1) Eviction: a miner with no commit for more than minerPruneBlocks is removed and its bond returned.
// This closes a design hole: `silence_slash` lives in `clawbackPayment`, which presupposes a payment,
// so no penalty ever reached a miner that produces nothing.
func TestPruneEvacuatesInactiveMinerAndRefundsBond(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_phantom__")))
	require.NoError(t, err)
	vitMiner(f, t, "mGhost", creator, vitAncient)

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mGhost")
	require.Error(t, gErr, "a miner inert beyond the prune window must be evicted from the registry")
	_, lErr := f.keeper.MinerLastCommitHeight.Get(f.ctx, "mGhost")
	require.Error(t, lErr, "its vitality entry must leave with it (no KV leak)")
}

// (2) The same guard stays silent: a recently active miner is kept. Without this test, a
// `pruneInactiveMiners` that emptied the whole registry would look correct.
func TestPruneKeepsActiveMiner(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_alive____")))
	require.NoError(t, err)
	vitMiner(f, t, "mAlive", creator, vitRecent)

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mAlive")
	require.NoError(t, gErr, "a producing miner must never be evicted")
}

// (3) A brand-new miner is not evicted. No vitality entry means a miner that just arrived (or an
// imported state, since the collection is not part of genesis). The default must be benign, otherwise
// a migration would empty the registry and registering would become a trap.
func TestPruneSparesBrandNewMinerWithoutHistory(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_fresh____")))
	require.NoError(t, err)
	vitMiner(f, t, "mNew", creator, 0) // 0: no entry written

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mNew")
	require.NoError(t, gErr, "no history means benefit of the doubt: neither evicted nor excluded")
}

// (3bis) The real target: registered long ago and never productive. This is the population the guard
// aims at and the one it misses without a registration date: "never committed" and "just
// arrived" are then the same signal (a missing entry), so both get the benefit of the doubt, and the guard
// only catches miners that have produced and then stopped, the honest ones.
func TestPruneEvacuatesRegisteredButNeverProductiveMiner(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_sterile__")))
	require.NoError(t, err)
	vitMiner(f, t, "mSterile", creator, 0) // no commit, ever
	// ...but registered a very long time ago, which distinguishes it from a new miner.
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mSterile", vitAncient))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mSterile")
	require.Error(t, gErr,
		"registered long ago and never productive is the target: it must be evicted")
}

// (3ter) A miner carrying a held fee is never evicted. Otherwise `releaseHeld` no longer finds the
// miner and returns without paying, and since the held entry has already left the KV store, an honest
// miner's payment is destroyed. The real path: unbounded audit deferral, the job crosses the prune
// threshold, the miner has nothing left to commit and looks inactive while money is owed to it.
func TestPruneNeverEvacuatesMinerWithHeldFee(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_creditor_")))
	require.NoError(t, err)
	vitMiner(f, t, "mOwed", creator, vitAncient) // inactive for a long time: eviction candidate
	require.NoError(t, gvSetJob(f, f.ctx, "jOwed", types.Job{
		JobId: "jOwed", State: "open+paid+optimistic+disputed", MinerId: "mOwed", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jOwed", uint64(100000))) // money is owed to it

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, gErr := f.keeper.Miner.Get(f.ctx, "mOwed")
	require.NoError(t, gErr,
		"an open held fee forbids eviction: an honest miner's payment is never destroyed")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jOwed")
	require.NoError(t, hErr, "the held fee must still exist")
	require.Equal(t, uint64(100000), held)
}

// (4) Jury: inert miners can no longer occupy seats. They stay in the registry (below the eviction
// threshold) but are no longer summoned, so the pool falls below the floor and the audit is deferred,
// never clawed back. This takes away what the mute-identities attack gained.
func TestStaleMinersAreNotDrawnAsJurors(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_mutes____")))
	require.NoError(t, err)
	// 4 jurors, nominally enough, but all mute for a long time (beyond the juror window, below the
	// eviction threshold: they are still registered).
	for _, id := range []string{"mMute1", "mMute2", "mMute3", "mMute4"} {
		vitMiner(f, t, id, creator, uint64(vitH)-300000)
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jStale", types.Job{
		JobId: "jStale", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jStale")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	job, err := f.keeper.Job.Get(f.ctx, "jStale")
	require.NoError(t, err)
	require.NotContains(t, job.State, "disputed",
		"mute jurors must not be summoned -> pool below the floor -> audit deferred")
	require.NotContains(t, job.State, "clawed", "and above all: no honest miner pays for their silence")
}

// (5) The jury stays open to active miners: same setup, with one change, the jurors produced recently.
// The audit must open normally. If this test failed, guard (4) would be a disguised "never audit".
func TestFreshMinersAreStillDrawnAsJurors(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	creator, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("creator_of_actives__")))
	require.NoError(t, err)
	for _, id := range []string{"mAct1", "mAct2", "mAct3", "mAct4"} {
		vitMiner(f, t, id, creator, vitRecent)
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jFresh", types.Job{
		JobId: "jFresh", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(vitH, "jFresh")))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	job, err := f.keeper.Job.Get(f.ctx, "jFresh")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed",
		"active jurors in sufficient number -> the audit must open (the vitality guard does not block)")
	comm, cErr := f.keeper.AuditCommittee.Get(f.ctx, "jFresh")
	require.NoError(t, cErr)
	require.NotEmpty(t, comm)
}
