package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The anchored jury is the only list allowed to vote, and this query is how it is read.
//
// These cases pin the distinction the query exists to keep: "no jury was drawn" and "the jury is empty"
// are different facts, and a reader that receives `[]` for both cannot tell a deferred audit from a
// seated one. The neighbouring `assigned-committee` query (the work assignment, primary first) has
// already been mistaken for the jury.
//
// A case that only asserted `len(members) == 2` on the happy path would stay green if the handler
// started returning an empty list on a missing anchor. The refusal cases below are the ones that bite;
// the happy path keeps them honest.

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
	// The job exists and its audit was never opened: the `deferred_no_jury` situation.
	require.NoError(t, gvSetJob(f, f.ctx, "jNoJury", types.Job{JobId: "jNoJury", State: "open"}))

	resp, err := qs.AuditCommittee(f.ctx, &types.QueryAuditCommitteeRequest{JobId: "jNoJury"})
	require.Error(t, err, "an absent anchor must refuse: an empty list would be read as a seated but empty jury")
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

// The redo jury lives under a distinct key, and the two must not be confused.
//
// Both are anchored juries, and a reader that returned the audit jury for `--redo` would let a
// re-adjudication be audited against the committee it was opened to replace. The case sets different
// members on the two keys so that reading the wrong one cannot pass.
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

// An anchor without its seating height is old state, and the read still answers.
//
// Refusing the whole answer because the seating height is missing would hide the members the caller
// asked for: a guard stricter than the question.
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
