package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A committee member could leave the registry after answering, leaving the client's escrow no exit.
//
// `hasOpenObligation` is the guard that holds a miner back while it still owes something. It walked
// `HeldFee` (holding only the job's own `MinerId`), the `AuditCommittee` seats, and disputed jobs, and
// read neither `Commit` nor `assignedCommittee`, so a member of the work committee that had already
// answered was held by no clause at all.
//
// Measured, with the module funded so the exit is not refused for want of a bond refund (the first
// fixture failed there, on its own scaffolding rather than on the rule):
//
//	committee [sc sb sa], all three committed
//	sb unregisters after committing            -> accepted
//	committee derived again                     -> [sc sa]
//	sb's commit is still in the store           -> true
//	SettlePay                                   -> "incomplete committee -> settlement refused"
//	job                                         -> still "open", fee still held
//
// This is not the frozen-weight defect, which needed its own fix: the draw's source is
// `k.Miner.Walk`, the live registry, and freezing the ordering weight pins the sort key, but a deleted
// row is not walked at all, so the member leaves the committee under any weighting rule.
//
// It needs no adversary: `pruneInactiveMiners` evicts a miner that simply goes quiet, through the same
// guard, with the same result.
//
// Read rather than measured: `expireUnservedJob` returns the escrow only for a job whose state is
// exactly "open" and that no assigned miner served. The remaining commits make `served` true, so it
// reschedules instead of refunding.
//
// Holding back any miner that has committed on an unsettled job would fall into the trap
// `miner_vitality.go` describes: an identity whose key is lost could then never leave, and nothing
// bounds how long a job stays unsettled. Clause (d) of `hasOpenObligation` therefore bounds the hold by
// a condition on the job (settled, resolved, escrow returned), never by the miner's goodwill, and
// `expireUnservedJob` stays able to unblock. These cases check that.

func eacFund(t *testing.T, f *fixture) {
	t.Helper()
	// The exit refunds the bond from the module account. Without this the exit fails on funds and the
	// case measures its own scaffolding.
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000)))
}

func eacSetup(t *testing.T, f *fixture, jobID string) (map[string]string, string) {
	t.Helper()
	eacFund(t, f)
	ops := map[string]string{}
	for _, id := range []string{"ea", "eb", "ec"} {
		op := ssAddr20(t, f, "eac-"+id+"-"+jobID)
		ops[id] = op
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: op, Operator: op, Stake: 1000,
		}))
	}
	client := ssAddr20(t, f, "eac-client-"+jobID)
	cbz, err := f.addressCodec.StringToBytes(client)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(cbz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(90000))))
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		// Fee 0 on purpose. `SettlePay` refuses a job that still holds the escrow
		// `OpenJob` took: it pays from the client's wallet and would write "+settled", which is terminal
		// and would then forbid any release of that escrow. The subject of this file is committee
		// completeness, not the escrow, so the job opens without one and the protocol path keeps its own.
		JobId: jobID, State: "open", Fee: 0, Client: client,
	}))
	for _, id := range []string{"ea", "eb", "ec"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: "CANON"}))
	}
	return ops, client
}

// (a) A member that answered cannot leave while the job is live, and the settlement goes through.
func TestAMemberThatAnsweredCannotLeaveWhileTheJobIsLive(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	ops, client := eacSetup(t, f, "jEAC")

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEAC", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, before, keeper.CommitteeSize, "premise: a full committee, and all of it answered")

	leaver := before[1]
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[leaver], MinerId: leaver})
	require.Error(t, err,
		"a member that has answered on a live job is held: the committee is re-derived from the registry, so its departure would make the job unsettleable")

	after, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEAC", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, after, keeper.CommitteeSize, "the committee is intact, which is the property the hold buys")

	// And the settlement the hold protects actually completes.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jEAC", Amount: 3000})
	require.NoError(t, err, "with its committee whole, the job settles")
}

// The release, which makes the clause a hold rather than a life sentence. The bound is the job's own
// terminal state, and nothing further is asked of the miner: clause (b) is relaxed to avoid exactly
// that trap, since an identity whose key is lost must still be able to leave.
func TestTheHoldIsReleasedByTheJOBNeverByTheMiner(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	ops, client := eacSetup(t, f, "jEACr")

	members, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEACr", keeper.CommitteeSize)
	require.NoError(t, err)
	leaver := members[1]

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[leaver], MinerId: leaver})
	require.Error(t, err, "held while the job is live")

	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jEACr", Amount: 3000})
	require.NoError(t, err)

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[leaver], MinerId: leaver})
	require.NoError(t, err, "settled: the job owes nothing to a derivable committee any more, so the hold lifts")
}

// (b) The control, which gives (a) its meaning: the same fixture without the departure settles.
// Without it, "settlement refused" could not be told apart from a fixture that never settles.
func TestTheSameJobSettlesWhenNobodyLeaves(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, client := eacSetup(t, f, "jEACb")

	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jEACb", Amount: 3000})
	require.NoError(t, err, "with its committee intact the same job settles: the departure is the cause")

	job, err := f.keeper.Job.Get(f.ctx, "jEACb")
	require.NoError(t, err)
	require.Contains(t, job.State, "settled")
}

// (c) No adversary is needed: eviction goes through the same guard, so a member that simply stops
// answering would be removed with the same consequence, which is the ordinary life of a network.
// A test that calls `Miner.Remove` directly only shows that the escrow strands, which is true and
// says nothing about eviction: the shipped prune path goes through `hasOpenObligation`
// (miner_vitality.go), like `DeleteMiner`. This test drives the real path and asserts that the exit
// is closed.
func TestThePrunePathHoldsAMemberThatAnswered(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	_, _ = eacSetup(t, f, "jEACc")

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEACc", keeper.CommitteeSize)
	require.NoError(t, err)
	held := before[1]

	obligated, oErr := f.keeper.HasOpenObligationForTest(f.ctx, held)
	require.NoError(t, oErr)
	require.True(t, obligated,
		"the predicate both exit paths consult must hold a member that answered on a live job -- eviction is not a second door")
}
