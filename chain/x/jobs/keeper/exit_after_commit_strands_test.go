package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A COMMITTEE MEMBER MAY LEAVE THE REGISTRY AFTER ANSWERING, AND THE CLIENT'S ESCROW HAS NO EXIT LEFT.
//
// `hasOpenObligation` is the guard that holds a miner back while it still owes something. It walks
// `HeldFee` (holding only the job's own `MinerId`), the `AuditCommittee` seats, and disputed jobs. It
// reads NEITHER `Commit` NOR `assignedCommittee`: a member of the WORK committee that has already
// answered is held by no clause at all.
//
// MEASURED, with the module funded so the exit is not refused for want of a bond refund — the first
// fixture failed exactly there, on its own scaffolding rather than on the rule:
//
//	committee [sc sb sa], all three committed
//	sb unregisters AFTER committing            -> ACCEPTED
//	committee re-derived                        -> [sc sa]
//	sb's commit is still in the store           -> true
//	SettlePay                                   -> "incomplete committee -> settlement refused"
//	job                                         -> still "open", fee still held
//
// ⛔ THIS IS NOT THE FROZEN-WEIGHT DEFECT, and the distinction decides whether it needs its own fix.
// The draw's SOURCE is `k.Miner.Walk` — the live registry. Freezing the ordering weight pins the sort
// KEY; a row that has been deleted is not walked at all, so the member leaves the committee under any
// weighting rule. Of the three shapes the weight fix could take, two leave this mechanism whole.
//
// ⚠️ AND IT NEEDS NO ADVERSARY. `pruneInactiveMiners` evicts a miner that simply goes quiet, through
// the same guard, with the same result.
//
// ⚠️ WHAT IS READ RATHER THAN MEASURED, and marked as such: `expireUnservedJob` returns the escrow only
// for a job whose state is exactly "open" AND that no assigned miner served. The remaining commits make
// `served` true, so it reschedules instead of refunding — the code path is read here, not driven.
//
// ⛔ PINNED, NOT FIXED, because the obvious fix reopens a documented defect. Holding back any miner that
// has committed on an unsettled job is exactly the trap `miner_vitality.go` describes: an identity whose
// key is lost could then NEVER leave, and nothing bounds how long a job stays unsettled. A retention
// must be bounded by a condition on the JOB (settled, resolved, escrow returned), not by the miner's
// goodwill — and `expireUnservedJob` must stay able to unblock. That is a design decision.

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
		// Fee 0 ON PURPOSE. Since ADR-045 entry 9, `SettlePay` REFUSES a job that still holds the escrow
		// `OpenJob` took: it pays from the CLIENT's wallet and would write "+settled", which is terminal
		// and would then forbid any release of that escrow. The subject of this file is committee
		// COMPLETENESS, not the escrow, so the job opens without one and the protocol path keeps its own.
		JobId: jobID, State: "open", Fee: 0, Client: client,
	}))
	for _, id := range []string{"ea", "eb", "ec"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: "CANON"}))
	}
	return ops, client
}

// (a) THE DEFECT: the exit is accepted, and the settlement it breaks was already possible.
func TestAMemberThatAnsweredCannotLeaveWhileTheJobIsLive(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	ops, client := eacSetup(t, f, "jEAC")

	avant, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEAC", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, avant, keeper.CommitteeSize, "premise: a full committee, and all of it answered")

	partant := avant[1]
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[partant], MinerId: partant})
	require.Error(t, err,
		"a member that has ANSWERED on a live job is held: the committee is re-derived from the registry, so its departure would make the job unsettleable")

	apres, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEAC", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, apres, keeper.CommitteeSize, "the committee is intact, which is the property the hold buys")

	// And the settlement the hold protects actually completes.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jEAC", Amount: 3000})
	require.NoError(t, err, "with its committee whole, the job settles")
}

// THE RELEASE, and it is what makes the clause a HOLD rather than a life sentence. The bound is the
// JOB's own terminal state -- nothing further is ever asked of the miner, which is the trap clause (b)
// had to be relaxed to avoid: an identity whose key is lost must still be able to leave.
func TestTheHoldIsReleasedByTheJOBNeverByTheMiner(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	ops, client := eacSetup(t, f, "jEACr")

	membres, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEACr", keeper.CommitteeSize)
	require.NoError(t, err)
	partant := membres[1]

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[partant], MinerId: partant})
	require.Error(t, err, "held while the job is live")

	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jEACr", Amount: 3000})
	require.NoError(t, err)

	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: ops[partant], MinerId: partant})
	require.NoError(t, err, "settled: the job owes nothing to a derivable committee any more, so the hold lifts")
}

// (b) THE CONTROL, and the reason (a) means anything: the SAME fixture without the departure settles.
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

// (c) NO ADVERSARY IS NEEDED. Eviction goes through the same guard, so a member that simply stops
// answering is removed with the same consequence — and that is the ordinary life of a network.
// ⛔ THIS TEST USED TO BYPASS THE GUARD IT CLAIMED TO MEASURE. It called `Miner.Remove` directly and
// asserted that the escrow stranded -- which is true, and says nothing about eviction: the shipped
// prune path goes through `hasOpenObligation` (miner_vitality.go), exactly like `DeleteMiner`. A
// fabricated end state cannot tell you whether the door that leads to it is open. It now drives the
// real path, and what it asserts is that the door is SHUT.
func TestThePrunePathHoldsAMemberThatAnswered(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	_, _ = eacSetup(t, f, "jEACc")

	avant, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jEACc", keeper.CommitteeSize)
	require.NoError(t, err)
	garde := avant[1]

	obligated, oErr := f.keeper.HasOpenObligationForTest(f.ctx, garde)
	require.NoError(t, oErr)
	require.True(t, obligated,
		"the predicate BOTH exit paths consult must hold a member that answered on a live job -- eviction is not a second door")
}
