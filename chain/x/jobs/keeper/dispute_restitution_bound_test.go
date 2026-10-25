package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Restitution is bounded by the Treasury.
//
// The settlement handlers write a `SlashRecord` "so that an upheld dispute can restore it". In
// `ResolveDispute` the re-credit is `if amt > pools.Treasury { amt = pools.Treasury }`, so that promise
// holds only while the Treasury still carries what was taken.
//
// Coverage showed the happy path covered and both clamps uncovered. They are reachable behaviour with a
// money consequence: a miner cleared by a dispute can get back less than it lost, and nothing in the
// response says so. These cases pin it instead of leaving "a successful dispute restores the slash"
// unqualified.

func drSetup(t *testing.T, f *fixture, jobID, minerID string, slashed, treasury uint64) string {
	t.Helper()
	require.NoError(t, gvSetMiner(f, f.ctx, minerID, types.Miner{
		MinerId: minerID, Creator: ssAddr20(t, f, "creator-"+minerID),
		Operator: ssAddr20(t, f, "operator-"+minerID), Stake: 200,
	}))
	disputer := ssAddr20(t, f, "disputer-"+jobID)
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, MinerId: minerID, Fee: 10000,
		State:    "open+paid+disputed",
		Disputer: disputer, DisputeBond: 0, // bond 0: this case is about restitution; the reward is tested elsewhere
		SlashRecords: []types.SlashRecord{{MinerId: minerID, Amount: slashed}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: treasury}))
	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	return authority
}

// The full case: the Treasury still holds the slash, so the miner is made whole.
func TestResolveDisputeRestoresTheWholeSlashWhenTheTreasuryCoversIt(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	authority := drSetup(t, f, "jDR1", "mDR1", 800, 800)

	_, err := srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{
		Authority: authority, JobId: "jDR1", Upheld: true,
	})
	require.NoError(t, err)

	m, err := f.keeper.Miner.Get(f.ctx, "mDR1")
	require.NoError(t, err)
	require.Equal(t, uint64(200+800), m.Stake, "an upheld dispute gives the slash back in full")
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Zero(t, pools.Treasury, "and the Treasury is debited by exactly what it gave back")
}

// The bound: the Treasury has been drained since the slash, so the miner gets back only part of it. The
// handler answers success either way; nothing in the response distinguishes a full restitution from a
// partial one, and this case makes that visible.
func TestResolveDisputeRestoresOnlyWhatTheTreasuryStillHolds(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	authority := drSetup(t, f, "jDR2", "mDR2", 800, 300) // slashed 800, Treasury down to 300

	_, err := srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{
		Authority: authority, JobId: "jDR2", Upheld: true,
	})
	require.NoError(t, err, "the resolution succeeds: a partial restitution is not an error path")

	m, err := f.keeper.Miner.Get(f.ctx, "mDR2")
	require.NoError(t, err)
	require.Equal(t, uint64(200+300), m.Stake,
		"the re-credit is clamped to the Treasury: the miner is not made whole, and is told nothing")
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Zero(t, pools.Treasury, "the Treasury is emptied rather than driven negative (uint64 underflow)")

	job, err := f.keeper.Job.Get(f.ctx, "jDR2")
	require.NoError(t, err)
	require.Contains(t, job.State, "+resolved")
}

// A rejected dispute restores nothing, and the bond funds the Treasury (anti-grief).
func TestResolveDisputeRejectedRestoresNothingAndKeepsTheBond(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, gvSetMiner(f, f.ctx, "mDR3", types.Miner{
		MinerId: "mDR3", Creator: ssAddr20(t, f, "creator-mDR3"),
		Operator: ssAddr20(t, f, "operator-mDR3"), Stake: 200,
	}))
	require.NoError(t, gvSetJob(f, f.ctx, "jDR3", types.Job{
		JobId: "jDR3", MinerId: "mDR3", Fee: 10000, State: "open+paid+disputed",
		Disputer: ssAddr20(t, f, "disputer-jDR3"), DisputeBond: 50000,
		SlashRecords: []types.SlashRecord{{MinerId: "mDR3", Amount: 800}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 800}))
	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)

	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{
		Authority: authority, JobId: "jDR3", Upheld: false,
	})
	require.NoError(t, err)

	m, err := f.keeper.Miner.Get(f.ctx, "mDR3")
	require.NoError(t, err)
	require.Equal(t, uint64(200), m.Stake, "a rejected dispute restores nothing")
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(800+50000), pools.Treasury,
		"and the bond funds the Treasury: griefing must cost the griefer, not the miner")
}

// Anti-replay, checked on the stake as well as the error: a second resolution must not re-credit.
func TestResolveDisputeCannotBeReplayedToInflateAStake(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	authority := drSetup(t, f, "jDR4", "mDR4", 800, 5000)
	msg := &types.MsgResolveDispute{Authority: authority, JobId: "jDR4", Upheld: true}

	_, err := srv.ResolveDispute(f.ctx, msg)
	require.NoError(t, err)
	after, err := f.keeper.Miner.Get(f.ctx, "mDR4")
	require.NoError(t, err)

	_, err = srv.ResolveDispute(f.ctx, msg)
	require.Error(t, err, "a resolved dispute must not resolve twice")

	replayed, err := f.keeper.Miner.Get(f.ctx, "mDR4")
	require.NoError(t, err)
	require.Equal(t, after.Stake, replayed.Stake,
		"the replay must not credit the stake again: the error code alone would not prove it")
}
