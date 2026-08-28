package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE ANCHORED JURY IS THE ONLY LIST ALLOWED TO VOTE, AND UNTIL 2026-08-11 NOTHING COULD READ IT.
//
// What these cases pin is NOT "the query answers". It is the DISTINCTION the query exists to keep:
// «no jury was drawn» and «the jury is empty» are different facts, and a reader that receives `[]`
// for both cannot tell a deferred audit from a seated one. That confusion is not hypothetical — the
// neighbouring `assigned-committee` (the WORK assignment, primary first) was read as the jury and a
// 3-member list was declared unable to reach quorum, while the anchored jury had 12 members.
//
// ⛔ A case that only asserted `len(members) == 2` on the happy path would stay green if the handler
// started returning an empty list on a missing anchor. The refusal cases below are the ones that
// bite; the happy path is the witness that keeps them honest.

func TestAuditCommitteeReturnsTheAnchoredJuryAndItsSeatingHeight(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	require.NoError(t, gvSetJob(f, f.ctx, "jSeated", types.Job{JobId: "jSeated", State: "open+paid+optimistic+disputed"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jSeated", "mA,mB,mC"))
	require.NoError(t, f.keeper.CommitteeSince.Set(f.ctx, "jSeated", 4242))

	resp, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jSeated"})
	require.NoError(t, err)
	require.Equal(t, []string{"mA", "mB", "mC"}, resp.Members)
	require.Equal(t, uint64(4242), resp.AnchoredHeight,
		"the seating height is the anchor's twin: without it, a juror held three blocks and one held a month look alike")
}

func TestAuditCommitteeRefusesAMissingAnchorInsteadOfReturningAnEmptyJury(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	// The job EXISTS and its audit was never opened — the `deferred_no_jury` situation, which is the
	// state of most jobs on the live network.
	require.NoError(t, gvSetJob(f, f.ctx, "jNoJury", types.Job{JobId: "jNoJury", State: "open"}))

	resp, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jNoJury"})
	require.Error(t, err, "an absent anchor must REFUSE: an empty list would be read as a seated but empty jury")
	require.Nil(t, resp)
	require.Equal(t, codes.NotFound, status.Code(err))
}

func TestAuditCommitteeRefusesAnUnknownJob(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	_, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jAbsent"})
	require.Error(t, err)
	require.Equal(t, codes.NotFound, status.Code(err))
}

func TestAuditCommitteeRefusesAnEmptyJobId(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	_, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "   "})
	require.Error(t, err)
	require.Equal(t, codes.InvalidArgument, status.Code(err))
}

// THE REDO JURY LIVES UNDER A DISTINCT KEY (ADR-033), AND THE TWO MUST NOT BE CONFUSED.
//
// Both are anchored, both are juries, and a reader that returned the audit jury for `--redo` would
// let a re-adjudication be audited against the committee it was opened to replace. The case sets
// DIFFERENT members on the two keys precisely so that reading the wrong one cannot pass.
func TestAuditCommitteeReadsTheRedoJuryUnderItsOwnKey(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	require.NoError(t, gvSetJob(f, f.ctx, "jRedo", types.Job{JobId: "jRedo", State: "open+paid+optimistic+disputed"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jRedo", "mFirst,mSecond"))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, keeper.RedoAnchorKeyForTest("jRedo"), "mFresh"))

	first, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jRedo"})
	require.NoError(t, err)
	require.Equal(t, []string{"mFirst", "mSecond"}, first.Members)

	redo, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jRedo", Redo: true})
	require.NoError(t, err)
	require.Equal(t, []string{"mFresh"}, redo.Members,
		"--redo must read the re-adjudication anchor, not the committee it replaces")
}

// AN ANCHOR WITHOUT ITS TWIN IS OLD STATE, NOT A BROKEN READ.
//
// Refusing the whole answer because the seating height is missing would hide the members the caller
// actually asked for — the failure mode of a guard that is stricter than the question.
func TestAuditCommitteeStillAnswersWhenTheSeatingHeightIsAbsent(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)
	require.NoError(t, gvSetJob(f, f.ctx, "jOld", types.Job{JobId: "jOld", State: "open"}))
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jOld", "mLegacy"))

	resp, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jOld"})
	require.NoError(t, err)
	require.Equal(t, []string{"mLegacy"}, resp.Members)
	require.Equal(t, uint64(0), resp.AnchoredHeight)
}
