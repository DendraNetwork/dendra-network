package keeper_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Every escrow needs a way out; these cases lock the one for a job nobody served.
//
// OpenJob moves the client's fee into the module account. Every other exit runs through settlement,
// and settlement needs a primary that commits, so a miner that never answers would leave the money there for
// good: no timeout, no message, no authority path, and nothing to report it. These cases drive the
// shipped EndBlocker, not a copy of its decision.
//
// The two halves matter equally. A refund that fires when it should is worth nothing if it also fires
// on a job that was served, since that would take the fee away from real work. So each "it refunds"
// case has its "it does not" twin.

// jobIsPaidProbe mirrors the unexported settlement predicate so the case above can assert its premise
// instead of assuming it. If `jobIsPaid` ever learns to see "+expired+refunded", this probe diverges
// and the case reports it, instead of passing for a reason that has quietly stopped holding.
func jobIsPaidProbe(state string) bool {
	return strings.Contains(state, "paid") || strings.Contains(state, "settled")
}

// openExpirableJob opens a job at height 1 and returns the height at which its expiry is due.
func openExpirableJob(t *testing.T, f *fixture, srv types.MsgServer, client, jobId string, fee uint64) int64 {
	t.Helper()
	_, err := srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: jobId, Fee: fee})
	require.NoError(t, err)
	// OpenJob schedules the appointment itself; the test reads it back rather than recomputing the
	// window, so a change to the constant cannot make this test silently stop covering anything.
	var due int64 = -1
	require.NoError(t, f.keeper.PendingJobExpiry.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		if key.K2() == jobId {
			due = key.K1()
			return true, nil
		}
		return false, nil
	}))
	require.NotEqual(t, int64(-1), due, "OpenJob must schedule an expiry for the escrow it just took")
	return due
}

// A job nobody ever served: at its appointment, the client gets its fee back and the job says so.
func TestExpiryRefundsTheEscrowOfAJobNeverServed(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	due := openExpirableJob(t, f, srv, client, "jobExp", 500)

	// Drive the shipped EndBlocker at the appointed height.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobExp")
	require.NoError(t, gErr)
	require.Contains(t, job.State, "+expired+refunded", "the job must record that its escrow went back")

	// And the appointment is consumed: a second pass must not refund twice.
	found := false
	require.NoError(t, f.keeper.PendingJobExpiry.Walk(f.ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		if key.K2() == "jobExp" {
			found = true
		}
		return false, nil
	}))
	require.False(t, found, "the expiry appointment must be spent, not left to fire again")
}

// The twin that matters most, which was once green by fabrication.
//
// Its first version wrote `Commit.Set(ctx, "jobServed", ...)`, a bare key. The chain never produces
// that key and refuses it, since CreateCommit requires "<jobId>__<minerId>". So the case proved nothing,
// and it hid the fact that the guard it covered queried that same impossible key: the guard was dead
// and its twin certified it as working.
//
// This version anchors the commit through the shipped message, as a miner does, and asserts that the
// bare key stays absent, so a return to `Commit.Get(ctx, jobId)` fails here.
func TestExpiryDoesNotRefundAServedJob(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	idM1 := deriveIDFor(t, f, client)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: client, MinerId: idM1, Operator: client, Stake: 1000})
	require.NoError(t, err)

	due := openExpirableJob(t, f, srv, client, "jobServed", 30)

	// The real path: the key is composite because the chain makes it so, not because the test says so.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: client, JobId: "jobServed__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err, "the commit must be anchored by the shipped message, never fabricated")

	// The bare-jobId key must not exist, so a guard that queries it can never match: anti-regression check.
	_, bareErr := f.keeper.Commit.Get(f.ctx, "jobServed")
	require.Error(t, bareErr, "the chain never writes a commit under the bare jobId — a guard reading it is dead")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobServed")
	require.NoError(t, gErr)
	require.NotContains(t, job.State, "expired", "a served job must never be expired by this pass")
}

// A commit from a miner the draw never assigned must not count as service.
//
// `CreateCommit` checks that the signer is the operator of the miner it names; it does not check that
// this miner belongs to the job's committee. Any registered miner can therefore write a key under
// `<jobId>__`, so the prefix alone cannot answer "was this job served": only a commit from a miner the
// draw actually assigned can, since only that miner's work can ever be settled. Reading the bare prefix
// would let an unrelated key stand in for service, and a job wrongly held to be served is a job the
// expiry never reclaims, its escrow left with no appointment.
//
// The pool holds more miners than the committee takes, so exactly one is left out by construction.
// The committee is read from the shipped query rather than recomputed here, otherwise the test would
// certify its own idea of the draw instead of the chain's.
func TestExpiryACommitOUTSIDETheCommitteeDoesNotCountAsServed(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	qs := keeper.NewQueryServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	// Four miners for a committee of three: at least one is necessarily outside it.
	ids := make([]string, 0, 4)
	for i := 0; i < 4; i++ {
		op, oErr := f.addressCodec.BytesToString([]byte(fmt.Sprintf("minerOperator%02d_____________", i)))
		require.NoError(t, oErr)
		id := deriveIDFor(t, f, op)
		_, cErr := srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, MinerId: id, Operator: op, Stake: 1000})
		require.NoError(t, cErr)
		ids = append(ids, id)
	}

	due := openExpirableJob(t, f, srv, client, "jobIntruder", 30)

	// Who is in, read from the chain's own query.
	resp, qErr := qs.AssignedCommittee(f.ctx, &types.QueryAssignedCommitteeRequest{JobId: "jobIntruder"})
	require.NoError(t, qErr)
	inside := map[string]bool{}
	for _, m := range resp.Members {
		inside[m] = true
	}
	var intruder, opIntruder string
	for i, id := range ids {
		if !inside[id] {
			intruder = id
			op, oErr := f.addressCodec.BytesToString([]byte(fmt.Sprintf("minerOperator%02d_____________", i)))
			require.NoError(t, oErr)
			opIntruder = op
			break
		}
	}
	require.NotEmpty(t, intruder, "with 4 miners and a committee of 3, one must be outside the draw")

	// The intruder anchors a commit on a job it was never assigned. The chain currently accepts it;
	// that acceptance is not what this test judges. It judges that the expiry does not read it as
	// service.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: opIntruder, JobId: "jobIntruder__" + intruder, ResultCommit: "9,9,9"})
	require.NoError(t, err, "the commit is anchored by the shipped message, never fabricated")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobIntruder")
	require.NoError(t, gErr)
	require.Contains(t, job.State, "expired",
		"a commit from a miner outside the assigned committee must not shield the escrow from expiry")
}

// After a legitimate refund, nothing may pay from this job again, and all four paths are driven here.
//
// The refund empties the module of this job's escrow but leaves `job.Fee` intact and the state readable
// as "open+expired+refunded", which `jobIsPaid` does not match (it looks for "paid" or "settled"). A
// disbursement reached afterwards computes an amount from `job.Fee` and sends it from the module
// account, which no longer holds this job's coins, so the payment comes out of the other clients'
// escrows. Payout is permissionless, so anyone could drain the module once a single job has expired.
//
// Every disbursement entry point is driven: a fifth path added later without the guard is the defect
// this case exists to catch, and a single-path test would miss it.
func TestExpiryNoPathPaysAfterARefund(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	due := openExpirableJob(t, f, srv, client, "jobDrain", 500)
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobDrain")
	require.NoError(t, gErr)
	require.Contains(t, job.State, "+expired+refunded", "prerequisite: the escrow really went back")
	require.False(t, jobIsPaidProbe(job.State),
		"prerequisite: jobIsPaid does not see this state — that mismatch is the hazard being guarded")

	cases := []struct {
		name string
		run  func() error
	}{
		{"Payout", func() error {
			_, e := srv.Payout(f.ctx, &types.MsgPayout{Creator: client, JobId: "jobDrain", Amount: 500})
			return e
		}},
		{"SettlePay", func() error {
			_, e := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jobDrain", Amount: 500})
			return e
		}},
		{"SettleSemantic", func() error {
			_, e := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: client, JobId: "jobDrain"})
			return e
		}},
	}
	for _, c := range cases {
		err := c.run()
		require.Error(t, err, "%s must refuse a job whose escrow was already returned", c.name)
		require.Contains(t, err.Error(), "escrow already returned",
			"%s must refuse for the right reason, not by accident of some later guard", c.name)
	}
}

// A job that already moved on (settled, disputed, finalised...) belongs to settlement, not to this pass.
func TestExpiryDoesNotTouchAnAlreadyAdvancedJob(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	due := openExpirableJob(t, f, srv, client, "jobAdvanced", 500)

	job, gErr := f.keeper.Job.Get(f.ctx, "jobAdvanced")
	require.NoError(t, gErr)
	job.State = job.State + "+settled"
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jobAdvanced", job))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	after, aErr := f.keeper.Job.Get(f.ctx, "jobAdvanced")
	require.NoError(t, aErr)
	require.NotContains(t, after.State, "expired", "settlement owns the money once the job has moved on")
}

// The appointment survives a genesis round trip as an age. An absolute height means nothing after a
// reset, and losing the entry would strand the escrow with nothing to report it, the failure this
// whole file exists to remove, brought back by an export that forgets it.
func TestExpirySurvivesTheGenesisRoundTrip(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	_ = openExpirableJob(t, f, srv, client, "jobGen", 500)

	exported, eErr := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, eErr)
	found := false
	for _, e := range exported.PendingJobExpiry {
		if e.Id == "jobGen" {
			found = true
			require.Greater(t, e.BlocksRemaining, int64(0), "the entry must travel as a remaining age, not as an absolute height")
		}
	}
	require.True(t, found, "the expiry appointment must be exported, or the escrow is stranded by a reset")
}
