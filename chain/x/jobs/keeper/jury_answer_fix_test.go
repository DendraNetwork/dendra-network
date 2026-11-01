package keeper_test

import (
	"cosmossdk.io/collections"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestRedoOnlyVerdictDoesNotCountAsAnswer(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	ids, ops := a59Audit(t, f, "jRedoVerdictFix", 1, 999999)
	redoAnchor(f, t, "jRedoVerdictFix", ids[0])
	require.NoError(t, f.keeper.AuditCommittee.Remove(f.ctx, "jRedoVerdictFix"))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[0], 2))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
	_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: ops[0], JobId: "jRedoVerdictFix__verdict__" + ids[0], ResultCommit: "1"})
	require.NoError(t, err)
	_, err = f.keeper.MinerLastVerdictHeight.Get(ctx, ids[0])
	require.ErrorIs(t, err, collections.ErrNotFound)
	require.Equal(t, uint64(2), a59Missed(t, f, ids[0]))
	_, err = srv.ResolveDispute(ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jRedoVerdictFix", Upheld: false})
	require.NoError(t, err)
	require.Equal(t, uint64(2), a59Missed(t, f, ids[0]), "a seat held only on the redo jury is not billed")
}

// The two tests below seat the judge on both juries: a seat held only on the redo jury is never billed
// (judge.go::recordMissedDraws), so only a judge that also holds an audit seat shows how the conclusion
// reads its re-commit.

func TestRedoAnswerRequiresValidVector(t *testing.T) {
	for _, tc := range []struct {
		name, vector string
		valid        bool
	}{{"malformed", "not-a-vector", false}, {"empty", "", false}, {"valid", "1,2,3", true}} {
		t.Run(tc.name, func(t *testing.T) {
			f := initFixture(t)
			srv := keeper.NewMsgServerImpl(f.keeper)
			job := "jRedoVectorFix" + tc.name
			ids, ops := a59Audit(t, f, job, 1, 999999)
			redoAnchor(f, t, job, ids[0])
			require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[0], 2))
			ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
			_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: ops[0], JobId: job + "__redo__" + ids[0], ResultCommit: tc.vector})
			require.NoError(t, err)
			date, err := f.keeper.MinerLastVerdictHeight.Get(ctx, ids[0])
			if tc.valid {
				require.NoError(t, err)
				require.Equal(t, uint64(5), date)
				require.Zero(t, a59Missed(t, f, ids[0]))
			} else {
				require.ErrorIs(t, err, collections.ErrNotFound)
				require.Equal(t, uint64(2), a59Missed(t, f, ids[0]))
			}
			_, err = srv.ResolveDispute(ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: job, Upheld: false})
			require.NoError(t, err)
			if tc.valid {
				require.Zero(t, a59Missed(t, f, ids[0]))
			} else {
				require.Equal(t, uint64(3), a59Missed(t, f, ids[0]))
			}
		})
	}
}

// A redo juror that also wrote a contested commit on the job (`<jobId>__<mid>`) has its re-commit left
// out of the adjudication tally (msg_server_adjudicate.go, AdjudicateDispute: `authors[mid]`), so the
// re-commit is no answer either (judge.go::juryAnswer). Read when the commit lands and again at the
// conclusion: written before the re-commit, the contested commit keeps the re-commit from being dated;
// written after it, the dated answer is billed at the conclusion, where the tally leaves it out.
func TestRedoReCommitFromAContestedAuthorIsNoAnswer(t *testing.T) {
	for _, tc := range []struct {
		name        string
		authorFirst bool
		dated       bool
		missed      uint64
	}{
		{"contested commit before the re-commit", true, false, 3},
		{"contested commit after the re-commit", false, true, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := initFixture(t)
			srv := keeper.NewMsgServerImpl(f.keeper)
			job := "jRedoAuthor"
			ids, ops := a59Audit(t, f, job, 1, 999999)
			redoAnchor(f, t, job, ids[0])
			require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[0], 2))
			ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5)
			contested := func() {
				_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: ops[0], JobId: job + "__" + ids[0], ResultCommit: "1,2,3"})
				require.NoError(t, err)
			}
			if tc.authorFirst {
				contested()
			}
			_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: ops[0], JobId: job + "__redo__" + ids[0], ResultCommit: "1,2,3"})
			require.NoError(t, err)
			date, err := f.keeper.MinerLastVerdictHeight.Get(ctx, ids[0])
			if tc.dated {
				require.NoError(t, err)
				require.Equal(t, uint64(5), date)
				require.Zero(t, a59Missed(t, f, ids[0]))
			} else {
				require.ErrorIs(t, err, collections.ErrNotFound)
				require.Equal(t, uint64(2), a59Missed(t, f, ids[0]))
			}
			if !tc.authorFirst {
				contested()
			}
			_, err = srv.ResolveDispute(ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: job, Upheld: false})
			require.NoError(t, err)
			require.Equal(t, tc.missed, a59Missed(t, f, ids[0]))
		})
	}
}
