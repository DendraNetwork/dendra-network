package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// RESTITUTION IS BOUNDED BY THE TREASURY, AND THAT BOUND WAS UNTESTED.
//
// The settlement handlers write a `SlashRecord` "so that an UPHELD dispute can restore it" — their own
// words, and I asserted the same thing in the verify-semantic bench. Reading `ResolveDispute` closes
// that sentence honestly: the re-credit is `if amt > pools.Treasury { amt = pools.Treasury }`. The
// promise therefore holds only while the Treasury still carries what was taken.
//
// Measured with `go tool cover`: the happy path was covered, both CLAMPS were not. They are not error
// branches — they are reachable behaviour with a money consequence: a miner cleared by a dispute can
// get back LESS than it lost, and nothing in the response says so. That is worth a case, and worth
// saying out loud rather than leaving "a successful dispute restores the slash" standing unqualified.

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
		Disputer: disputer, DisputeBond: 0, // bond 0: this case is about RESTITUTION, not the reward
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

// ⛔ THE BOUND: the Treasury has been drained since the slash, so the miner gets back only part of it.
// The handler answers SUCCESS either way — nothing in the response distinguishes a full restitution
// from a partial one, and this case is what makes that visible instead of surprising.
func TestResolveDisputeRestoresOnlyWhatTheTreasuryStillHolds(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	authority := drSetup(t, f, "jDR2", "mDR2", 800, 300) // slashed 800, Treasury down to 300

	_, err := srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{
		Authority: authority, JobId: "jDR2", Upheld: true,
	})
	require.NoError(t, err, "the resolution SUCCEEDS: a partial restitution is not an error path")

	m, err := f.keeper.Miner.Get(f.ctx, "mDR2")
	require.NoError(t, err)
	require.Equal(t, uint64(200+300), m.Stake,
		"the re-credit is clamped to the Treasury: the miner is NOT made whole, and is told nothing")
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Zero(t, pools.Treasury, "the Treasury is emptied rather than driven negative (uint64 underflow)")

	job, err := f.keeper.Job.Get(f.ctx, "jDR2")
	require.NoError(t, err)
	require.Contains(t, job.State, "+resolved")
}

// A REJECTED dispute must not restore anything, and the bond funds the Treasury (anti-grief).
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

// ANTI-REPLAY, verified on the STAKE rather than on the error: a second resolution must not re-credit.
func TestResolveDisputeCannotBeReplayedToInflateAStake(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	authority := drSetup(t, f, "jDR4", "mDR4", 800, 5000)
	msg := &types.MsgResolveDispute{Authority: authority, JobId: "jDR4", Upheld: true}

	_, err := srv.ResolveDispute(f.ctx, msg)
	require.NoError(t, err)
	apres, err := f.keeper.Miner.Get(f.ctx, "mDR4")
	require.NoError(t, err)

	_, err = srv.ResolveDispute(f.ctx, msg)
	require.Error(t, err, "a resolved dispute must not resolve twice")

	rejoue, err := f.keeper.Miner.Get(f.ctx, "mDR4")
	require.NoError(t, err)
	require.Equal(t, apres.Stake, rejoue.Stake,
		"the replay must not credit the stake again: the error code alone would not prove it")
}
