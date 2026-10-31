package keeper_test

import (
	"fmt"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The two-thirds bar on the permissionless path. AdjudicateDispute is the second caller of
// auditSlashDecision and auditVindicateDecision, and "unchanged in callers" holds only if it passes the
// anchored seats and the count of valid votes exactly as the audit deadline does.

// gateD6Adjudicate builds an optimistic job under dispute, past its dispute window, with the given
// audit jury and votes and a held fee of 80000, then sends AdjudicateDispute.
func gateD6Adjudicate(t *testing.T, jobId string, seats int, votes []gateD6Vote) (*fixture, sdk.AccAddress, error) {
	t.Helper()
	f, srv, disp := adr033Setup(t, "disputer_gate_d6____")
	opAcc, op := gateD6Operator(t, f)
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	gateD6Jury(t, f, jobId, seats, votes)
	require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
		JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, uint64(80000)))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: jobId})
	return f, opAcc, err
}

// gateD6RequireRefused asserts that the adjudication concluded nothing and left the audit to its deadline.
func gateD6RequireRefused(t *testing.T, f *fixture, opAcc sdk.AccAddress, jobId string, err error, why string) {
	t.Helper()
	require.Error(t, err, why)
	require.ErrorContains(t, err, "relative bar", why)
	job, gErr := f.keeper.Job.Get(f.ctx, jobId)
	require.NoError(t, gErr)
	require.NotContains(t, job.State, "resolved", why)
	require.Empty(t, job.SlashRecords, why)
	m, mErr := f.keeper.Miner.Get(f.ctx, "mP")
	require.NoError(t, mErr)
	require.Equal(t, aeStake, m.Stake, why)
	held, hErr := f.keeper.HeldFee.Get(f.ctx, jobId)
	require.NoError(t, hErr, why)
	require.Equal(t, uint64(80000), held, why)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), why)
}

// The adjudication slashes only with two thirds of the anchored seats invalid: 9 of 15 is above the
// participation floor and refused, 10 of 15 slashes; at 8 seats, 5 is refused and 6 slashes.
func TestADR059_TwoThirdsBarHoldsOnTheAdjudicationSlash(t *testing.T) {
	for _, seats := range []int{15, 8} {
		bar := gateD6TwoThirds(seats)
		t.Run(fmt.Sprintf("seats=%d/invalid=%d refused", seats, bar-1), func(t *testing.T) {
			f, op, err := gateD6Adjudicate(t, "jD6a", seats, gateD6Votes(bar-1, false, gateD6JurorStake))
			gateD6RequireRefused(t, f, op, "jD6a", err, "invalid votes below two thirds of the anchored seats")
		})
		t.Run(fmt.Sprintf("seats=%d/invalid=%d slashes", seats, bar), func(t *testing.T) {
			f, _, err := gateD6Adjudicate(t, "jD6a", seats, gateD6Votes(bar, false, gateD6JurorStake))
			require.NoError(t, err, "two thirds of the anchored seats invalid concludes")
			job, gErr := f.keeper.Job.Get(f.ctx, "jD6a")
			require.NoError(t, gErr)
			require.Contains(t, job.State, "+resolved+adjudicated")
			require.Len(t, job.SlashRecords, 1, "the slash is recorded")
			p, pErr := f.keeper.Params.Get(f.ctx)
			require.NoError(t, pErr)
			m, mErr := f.keeper.Miner.Get(f.ctx, "mP")
			require.NoError(t, mErr)
			require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, m.Stake, "the primary is slashed at the bar")
		})
	}
}

// The adjudication vindicates only with two thirds of the anchored seats voting valid and the valid
// stake majority. Ten voters with six valid votes, or ten valid votes against the stake majority, are
// refused; ten valid votes with the stake majority vindicate.
func TestADR059_TwoThirdsBarHoldsOnTheAdjudicationVindication(t *testing.T) {
	const seats = 15
	bar := gateD6TwoThirds(seats)

	t.Run("6 valid and 4 invalid refused", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(6, true, 10*gateD6JurorStake), gateD6Votes(bar-6, false, gateD6JurorStake))
		f, op, err := gateD6Adjudicate(t, "jD6b", seats, votes)
		gateD6RequireRefused(t, f, op, "jD6b", err, "10 voters with 6 valid votes do not vindicate")
	})
	t.Run("two thirds valid against the stake majority refused", func(t *testing.T) {
		votes := gateD6Join(gateD6Votes(bar, true, gateD6JurorStake), gateD6Votes(seats-bar, false, 3*gateD6JurorStake))
		f, op, err := gateD6Adjudicate(t, "jD6b", seats, votes)
		gateD6RequireRefused(t, f, op, "jD6b", err, "a vindication needs the valid stake majority")
	})
	t.Run("two thirds valid vindicates", func(t *testing.T) {
		f, op, err := gateD6Adjudicate(t, "jD6b", seats, gateD6Votes(bar, true, gateD6JurorStake))
		require.NoError(t, err, "two thirds of the anchored seats valid concludes")
		job, gErr := f.keeper.Job.Get(f.ctx, "jD6b")
		require.NoError(t, gErr)
		require.Contains(t, job.State, "+resolved+adjudicated")
		require.Empty(t, job.SlashRecords)
		require.Equal(t, int64(80000), f.bank.balOf(op).AmountOf("udndr").Int64(), "the held fee is released to the operator")
	})
}
