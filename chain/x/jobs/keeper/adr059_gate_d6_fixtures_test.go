package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Fixtures for the rule that the two-thirds bar does not move. Each case anchors one
// audit jury, posts verdicts in seat order and leaves the other seats silent.

// gateD6Vote is one verdict of an anchored seat and the stake of the seat that casts it.
type gateD6Vote struct {
	valid bool
	stake uint64
}

const gateD6JurorStake = uint64(1_000_000)

// gateD6Votes returns n verdicts of one direction, each cast by a seat of the given stake.
func gateD6Votes(n int, valid bool, stake uint64) []gateD6Vote {
	out := make([]gateD6Vote, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, gateD6Vote{valid: valid, stake: stake})
	}
	return out
}

// gateD6Join concatenates vote lists, in seat order.
func gateD6Join(parts ...[]gateD6Vote) []gateD6Vote {
	var out []gateD6Vote
	for _, p := range parts {
		out = append(out, p...)
	}
	return out
}

// gateD6TwoThirds is the smallest count that is at least two thirds of `seats`. It counts up instead
// of dividing, so it shares no arithmetic with the code it checks.
func gateD6TwoThirds(seats int) int {
	q := 0
	for 3*q < 2*seats {
		q++
	}
	return q
}

// gateD6Jury registers `seats` jurors, anchors them as the audit jury of jobId and posts `votes` on
// the first seats. A silent seat keeps the default juror stake.
func gateD6Jury(t *testing.T, f *fixture, jobId string, seats int, votes []gateD6Vote) []string {
	t.Helper()
	require.LessOrEqual(t, len(votes), seats, "fixture: more votes than seats")
	ids := make([]string, 0, seats)
	for i := 0; i < seats; i++ {
		id := fmt.Sprintf("d6j%02d", i)
		stake := gateD6JurorStake
		if i < len(votes) {
			stake = votes[i].stake
		}
		aeReg(f, t, id, stake)
		ids = append(ids, id)
	}
	aeAnchor(f, t, jobId, ids...)
	for i, v := range votes {
		verdict := "0"
		if v.valid {
			verdict = "1"
		}
		aeVerdict(f, t, jobId, ids[i], verdict)
	}
	return ids
}

// gateD6Operator is the operator account of the primary "mP"; a vindication pays the held fee to it.
func gateD6Operator(t *testing.T, f *fixture) (sdk.AccAddress, string) {
	t.Helper()
	acc := sdk.AccAddress([]byte("operator_gate_d6____"))
	op, err := f.addressCodec.BytesToString(acc)
	require.NoError(t, err)
	f.bank.setBalance(acc, sdk.NewCoins())
	return acc, op
}

// gateD6AtTheTimeout builds a sampled optimistic job under dispute with the given jury and votes, a
// held fee of 80000, and runs its audit deadline at height 6 (resolveDisputedAudit through EndBlock).
func gateD6AtTheTimeout(t *testing.T, jobId string, p types.Params, seats int, votes []gateD6Vote) (*fixture, sdk.AccAddress) {
	t.Helper()
	return gateD6AtTheTimeoutAfter(t, jobId, p, seats, votes, nil)
}

// gateD6AtTheTimeoutAfter is gateD6AtTheTimeout with a hook that changes the state after the jury is
// anchored and before the deadline runs.
func gateD6AtTheTimeoutAfter(t *testing.T, jobId string, p types.Params, seats int, votes []gateD6Vote, after func(f *fixture, seatIds []string)) (*fixture, sdk.AccAddress) {
	t.Helper()
	f := initFixture(t)
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	opAcc, op := gateD6Operator(t, f)
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	seatIds := gateD6Jury(t, f, jobId, seats, votes)
	if after != nil {
		after(f, seatIds)
	}
	client, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("client_gate_d6______")))
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
		JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, uint64(80000)))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), jobId)))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
	return f, opAcc
}

// gateD6RequireDeferred asserts that the deadline concluded nothing: no slash, no vindication, the
// held fee in place, the operator unpaid, and a later deadline scheduled for the same job.
func gateD6RequireDeferred(t *testing.T, f *fixture, opAcc sdk.AccAddress, jobId, why string) {
	t.Helper()
	job, err := f.keeper.Job.Get(f.ctx, jobId)
	require.NoError(t, err)
	require.NotContains(t, job.State, "resolved", why)
	require.Empty(t, job.SlashRecords, why)
	m, err := f.keeper.Miner.Get(f.ctx, "mP")
	require.NoError(t, err)
	require.Equal(t, aeStake, m.Stake, why)
	held, err := f.keeper.HeldFee.Get(f.ctx, jobId)
	require.NoError(t, err, why)
	require.Equal(t, uint64(80000), held, why)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), why)
	later := false
	require.NoError(t, f.keeper.PendingAuditResolve.Walk(f.ctx, nil, func(k collections.Pair[int64, string]) (bool, error) {
		if k.K2() == jobId && k.K1() > 6 {
			later = true
		}
		return false, nil
	}))
	require.True(t, later, "a deferral schedules the next deadline: %s", why)
}

// gateD6RequireSlashed asserts a hard slash of the primary at the bar.
func gateD6RequireSlashed(t *testing.T, f *fixture, p types.Params, jobId, why string) {
	t.Helper()
	job, err := f.keeper.Job.Get(f.ctx, jobId)
	require.NoError(t, err)
	require.Contains(t, job.State, "+resolved+clawed+quorum", why)
	m, err := f.keeper.Miner.Get(f.ctx, "mP")
	require.NoError(t, err)
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, m.Stake, why)
}

// gateD6RequireVindicated asserts a vindication at the bar: the held fee goes to the operator.
func gateD6RequireVindicated(t *testing.T, f *fixture, opAcc sdk.AccAddress, jobId, why string) {
	t.Helper()
	job, err := f.keeper.Job.Get(f.ctx, jobId)
	require.NoError(t, err)
	require.Contains(t, job.State, "+resolved+vindicated+quorum", why)
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), why)
	m, err := f.keeper.Miner.Get(f.ctx, "mP")
	require.NoError(t, err)
	require.Equal(t, aeStake, m.Stake, why)
}
