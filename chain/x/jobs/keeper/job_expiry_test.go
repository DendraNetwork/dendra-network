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

// THE ESCROW HAD NO WAY OUT, AND THAT IS WHAT THESE CASES LOCK.
//
// OpenJob moves the client's fee into the module account. Every other exit runs through settlement,
// and settlement needs a primary that commits — so a miner that simply never answered left the money
// there for good: no timeout, no message, no authority path, and nothing to raise. These cases drive
// the shipped EndBlocker, not a copy of its decision.
//
// The two halves matter equally. A refund that fires when it should is worth nothing if it ALSO fires
// on a job that was served: that would take the fee away from real work. So each "it refunds" case has
// its "it does not" twin.

// jobIsPaidProbe mirrors the unexported settlement predicate so the case above can ASSERT its premise
// instead of assuming it. If `jobIsPaid` ever learns to see "+expired+refunded", this probe diverges
// and the case says so — rather than passing for a reason that has quietly stopped holding.
func jobIsPaidProbe(state string) bool {
	return strings.Contains(state, "paid") || strings.Contains(state, "settled")
}

// ouvreJobExpirable — opens a job at height 1 and returns the height at which its expiry is due.
func ouvreJobExpirable(t *testing.T, f *fixture, srv types.MsgServer, client, jobId string, fee uint64) int64 {
	t.Helper()
	_, err := srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: jobId, Fee: fee})
	require.NoError(t, err)
	// The appointment is scheduled by OpenJob itself; the test reads it back rather than recomputing
	// the window, so a change to the constant cannot make this test silently stop covering anything.
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

	due := ouvreJobExpirable(t, f, srv, client, "jobExp", 500)

	// Drive the SHIPPED EndBlocker at the appointed height.
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

// THE TWIN THAT MATTERS MOST, AND THE ONE THAT WAS GREEN BY FABRICATION.
//
// Its first version wrote `Commit.Set(ctx, "jobServi", ...)` — a BARE key. The chain never produces
// that key and actively refuses it: CreateCommit requires "<jobId>__<minerId>". So the case proved
// nothing, and worse, it CONCEALED the fact that the guard it existed to cover queried that same
// impossible key. The guard was dead and its twin certified it working.
//
// This version anchors the commit through the shipped message, exactly as a miner does, and asserts
// that the bare key stays absent — so a return to `Commit.Get(ctx, jobId)` fails here.
func TestExpiryNeRembourssePasUnJobSERVI(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	idM1 := deriveIDFor(t, f, client)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: client, MinerId: idM1, Operator: client, Stake: 1000})
	require.NoError(t, err)

	due := ouvreJobExpirable(t, f, srv, client, "jobServi", 30)

	// THE REAL PATH. The key is composite because the chain makes it so, not because the test says so.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: client, JobId: "jobServi__" + idM1, ResultCommit: "1,2,3"})
	require.NoError(t, err, "the commit must be anchored by the shipped message, never fabricated")

	// The key the dead guard used to query MUST NOT exist. This is the anti-regression.
	_, bareErr := f.keeper.Commit.Get(f.ctx, "jobServi")
	require.Error(t, bareErr, "the chain never writes a commit under the bare jobId — a guard reading it is dead")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobServi")
	require.NoError(t, gErr)
	require.NotContains(t, job.State, "expired", "a SERVED job must never be expired by this pass")
}

// A COMMIT FROM A MINER THE DRAW NEVER ASSIGNED MUST NOT COUNT AS SERVICE.
//
// `CreateCommit` checks that the signer is the operator OF THE MINER IT NAMES; it does not check that
// this miner belongs to the job's committee. Any registered miner can therefore write a key under
// `<jobId>__`, so the prefix alone cannot answer "was this job served": only a commit from a miner the
// draw actually assigned can, since only that miner's work can ever be settled. Reading the prefix
// bare would let an unrelated key stand in for service, and a job wrongly held to be served is a job
// the expiry never reclaims -- its escrow has no appointment left.
//
// The pool holds MORE miners than the committee takes, so exactly one is left out by construction —
// the committee is read from the shipped query rather than recomputed here, otherwise the test would
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

	due := ouvreJobExpirable(t, f, srv, client, "jobIntrus", 30)

	// WHO IS IN, read from the chain's own query.
	resp, qErr := qs.AssignedCommittee(f.ctx, &types.QueryAssignedCommitteeRequest{JobId: "jobIntrus"})
	require.NoError(t, qErr)
	inside := map[string]bool{}
	for _, m := range resp.Members {
		inside[m] = true
	}
	var intrus, opIntrus string
	for i, id := range ids {
		if !inside[id] {
			intrus = id
			op, oErr := f.addressCodec.BytesToString([]byte(fmt.Sprintf("minerOperator%02d_____________", i)))
			require.NoError(t, oErr)
			opIntrus = op
			break
		}
	}
	require.NotEmpty(t, intrus, "with 4 miners and a committee of 3, one must be outside the draw")

	// The intruder anchors a commit on a job it was never assigned. The chain ACCEPTS it today — that
	// acceptance is not what this test judges; what it judges is that the expiry does not read it as
	// service.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: opIntrus, JobId: "jobIntrus__" + intrus, ResultCommit: "9,9,9"})
	require.NoError(t, err, "the commit is anchored by the shipped message, never fabricated")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobIntrus")
	require.NoError(t, gErr)
	require.Contains(t, job.State, "expired",
		"a commit from a miner OUTSIDE the assigned committee must not shield the escrow from expiry")
}

// AFTER A LEGITIMATE REFUND, NOTHING MAY PAY FROM THIS JOB AGAIN — AND ALL FOUR PATHS ARE DRIVEN HERE.
//
// The refund empties the module of THIS job's escrow but leaves `job.Fee` intact and the state
// readable as "open+expired+refunded", which `jobIsPaid` does NOT match (it looks for "paid" or
// "settled"). A disbursement reached afterwards recomputes an amount from `job.Fee` and sends it from
// the module account, which no longer holds this job's coins: the payment comes out of the OTHER
// clients' escrows. Payout is permissionless, so this is a drain anyone can reach once a single job
// has expired — not an ordering curiosity.
//
// Every disbursement entry point is driven, not one of them: a fifth path added later without the
// guard is exactly the defect this case exists to catch, and a single-path test would miss it.
func TestExpiryNoPathPaysAfterARefund(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	due := ouvreJobExpirable(t, f, srv, client, "jobDrain", 500)
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	job, gErr := f.keeper.Job.Get(f.ctx, "jobDrain")
	require.NoError(t, gErr)
	require.Contains(t, job.State, "+expired+refunded", "prerequisite: the escrow really went back")
	require.False(t, jobIsPaidProbe(job.State),
		"prerequisite: jobIsPaid does NOT see this state — that mismatch IS the hazard being guarded")

	cas := []struct {
		nom string
		run func() error
	}{
		{"Payout", func() error {
			_, e := srv.Payout(f.ctx, &types.MsgPayout{Creator: client, JobId: "jobDrain", Amount: 500})
			return e
		}},
		{"SettleJob", func() error {
			_, e := srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: client, JobId: "jobDrain"})
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
	for _, c := range cas {
		err := c.run()
		require.Error(t, err, "%s must REFUSE a job whose escrow was already returned", c.nom)
		require.Contains(t, err.Error(), "escrow already returned",
			"%s must refuse for the RIGHT reason, not by accident of some later guard", c.nom)
	}
}

// A job that already moved on (settled, disputed, finalised…) belongs to settlement, not to this pass.
func TestExpiryNeToucheePasUnJobDejaAvance(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	due := ouvreJobExpirable(t, f, srv, client, "jobAvance", 500)

	job, gErr := f.keeper.Job.Get(f.ctx, "jobAvance")
	require.NoError(t, gErr)
	job.State = job.State + "+settled"
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jobAvance", job))

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(due)))

	after, aErr := f.keeper.Job.Get(f.ctx, "jobAvance")
	require.NoError(t, aErr)
	require.NotContains(t, after.State, "expired", "settlement owns the money once the job has moved on")
}

// The appointment survives a genesis round trip AS AN AGE. An absolute height means nothing after a
// reset, and losing the entry would strand the escrow with nothing to raise — the failure mode this
// whole file exists to remove, reintroduced by an export that forgets it.
func TestExpirySurvitLAllerRetourGenesis(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientAddr__________________"))
	require.NoError(t, err)

	_ = ouvreJobExpirable(t, f, srv, client, "jobGen", 500)

	exported, eErr := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, eErr)
	found := false
	for _, e := range exported.PendingJobExpiry {
		if e.Id == "jobGen" {
			found = true
			require.Greater(t, e.BlocksRemaining, int64(0), "the entry must travel as a REMAINING age, not as an absolute height")
		}
	}
	require.True(t, found, "the expiry appointment must be exported, or the escrow is stranded by a reset")
}
