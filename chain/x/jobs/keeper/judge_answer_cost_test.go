package keeper_test

import (
	"strconv"
	"strings"
	"testing"

	"cosmossdk.io/collections"
	storetypes "cosmossdk.io/store/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// costLargestJury is the largest `audit_committee_draw_size` that Params.Validate accepts, the size of
// the largest redo jury `anchorRedoCommittee` can draw. It is read from the function that refuses the
// posted value, so the test follows the bound if it moves.
func costLargestJury(t *testing.T) int {
	t.Helper()
	p := types.DefaultParams()
	largest := 0
	for v := uint64(1); v <= 4096; v++ {
		p.AuditCommitteeDrawSize = v
		if p.Validate() == nil {
			largest = int(v)
		}
	}
	require.Positive(t, largest, "premise: some draw size is valid")
	return largest
}

// costLongestVector is the longest result the chain parses: the most dimensions, each at the largest
// magnitude, so every commit the old walk decoded was as large as a re-commit can be.
func costLongestVector() string {
	parts := make([]string, keeper.EmbedMaxDimForTest)
	for i := range parts {
		parts[i] = strconv.Itoa(-keeper.EmbedMaxMagForTest)
	}
	return strings.Join(parts, ",")
}

// costJury seats `seats` declared judges on both juries of one disputed job, has each post a re-commit
// of the longest vector, and returns the gas of each re-commit, in order, and the gas of the count the
// conclusion runs. Every re-commit is an answer at both ends, the case where `juryAnswer` reads the
// contested authors.
func costJury(t *testing.T, seats int) ([]uint64, uint64) {
	t.Helper()
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	const job = "jCost"
	ids, ops := a59Audit(t, f, job, seats, 999999)
	redoAnchor(f, t, job, ids...)
	vec := costLongestVector()
	gas := make([]uint64, 0, seats)
	for i, id := range ids {
		ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithGasMeter(storetypes.NewInfiniteGasMeter())
		_, err := srv.CreateCommit(ctx, &types.MsgCreateCommit{Creator: ops[i], JobId: job + "__redo__" + id, ResultCommit: vec})
		require.NoError(t, err)
		gas = append(gas, ctx.GasMeter().GasConsumed())
		last, err := f.keeper.MinerLastVerdictHeight.Get(f.ctx, id)
		require.NoError(t, err)
		require.Equal(t, uint64(5), last, "a counted re-commit is the seat's answer when it lands")
	}
	ctx := sdk.UnwrapSDKContext(f.ctx).WithGasMeter(storetypes.NewInfiniteGasMeter())
	f.keeper.RecordMissedDrawsForTest(ctx, job)
	for _, id := range ids {
		require.Zero(t, a59Missed(t, f, id), "every seat answered, and the conclusion reads it so")
	}
	return gas, ctx.GasMeter().GasConsumed()
}

// The answer of a redo seat costs the same at any jury size (judge.go::juryAnswer reads the contested
// authors with one point read). A walk of the job's commits made the last re-commit of a full jury
// decode every earlier one, and the conclusion decode them all once per seat.
func TestJuryAnswerCostIsFlatAtTheLargestJury(t *testing.T) {
	seats := costLargestJury(t)
	gas, conclusion := costJury(t, seats)
	_, single := costJury(t, 1)
	oneRead := storetypes.KVGasConfig().ReadCostPerByte * uint64(len(costLongestVector()))
	t.Logf("seats=%d first re-commit=%d last re-commit=%d conclusion=%d conclusion of one seat=%d one commit read>=%d",
		seats, gas[0], gas[seats-1], conclusion, single, oneRead)
	require.Less(t, gas[seats-1], gas[0]+oneRead,
		"the last re-commit of the jury reads no commit more than the first")
	require.LessOrEqual(t, conclusion, uint64(seats)*single*5/4,
		"the count at the conclusion costs no more per seat at %d seats than at one", seats)
}

// The point read answers what the walk answers, on keys chosen to sit at its edges: role keys under
// the same prefix, an empty suffix, a suffix that holds the separator, a neighbouring job whose id
// extends this one, and a key that only shares the first characters.
func TestContestedAuthorMatchesTheWalkedAuthors(t *testing.T) {
	f := initFixture(t)
	for _, key := range []string{
		"j__a", "j__b", "j__redo__a", "j__redo__c", "j__verdict__d", "j__redo__verdict__e",
		"j__", "j__x__g", "j10__f", "j_h", "k__a",
	} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, key, types.Commit{JobId: key, ResultCommit: "1"}))
	}
	authors, err := f.keeper.CommitAuthorsOfForTest(f.ctx, "j")
	require.NoError(t, err)
	require.Equal(t, map[string]bool{"a": true, "b": true, "x__g": true}, authors, "premise: what the walk returns")
	for _, mid := range []string{
		"a", "b", "c", "d", "e", "f", "g", "h", "x__g", "", "redo__a", "redo__c", "verdict__d",
		"redo__verdict__e", "x", "unknown",
	} {
		got, err := f.keeper.ContestedAuthorForTest(f.ctx, "j", mid)
		require.NoError(t, err)
		require.Equal(t, authors[mid], got, "mid %q", mid)
	}
}

// A seat held only on the re-adjudication jury is never billed (judge.go::recordMissedDraws): the kit
// has no redo path, so a silent redo seat says nothing about its judge. A silent audit seat is billed,
// and a judge silent on both juries is billed once.
func TestRedoOnlySeatIsNeverBilledAsAMissedDraw(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	const job = "jRedoOnlyBill"
	ids, _ := a59Audit(t, f, job, 2, 999999)
	redoOnly := "r59-" + job
	a59Miner(t, f, redoOnly, 1000)
	a59Declare(t, f, 1, redoOnly)
	redoAnchor(f, t, job, ids[1], redoOnly)
	_, err := srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: job, Upheld: false})
	require.NoError(t, err)
	require.Equal(t, uint64(1), a59Missed(t, f, ids[0]), "a silent audit seat is billed")
	require.Equal(t, uint64(1), a59Missed(t, f, ids[1]), "a judge silent on both juries is billed once")
	require.Equal(t, uint64(0), a59Missed(t, f, redoOnly), "a silent seat held only on the redo jury is not billed")
}

// An answer that cannot be read says nothing about the seat, so the count skips it
// (judge.go::recordMissedDraws, the error of seatAnswered), while the twin seat with no commit is billed.
func TestMissedDrawsSkipASeatWhoseAnswerCannotBeRead(t *testing.T) {
	f := initFixture(t)
	const job = "jUnreadable"
	ids, _ := a59Audit(t, f, job, 2, 999999)
	key := job + "__verdict__" + ids[0]
	require.NoError(t, f.keeper.WriteRawCommitForTest(f.ctx, key, []byte{0xff}))
	_, err := f.keeper.Commit.Get(f.ctx, key)
	require.Error(t, err)
	require.NotErrorIs(t, err, collections.ErrNotFound, "premise: the commit is stored and cannot be decoded")
	f.keeper.RecordMissedDrawsForTest(f.ctx, job)
	require.Equal(t, uint64(0), a59Missed(t, f, ids[0]), "an unreadable answer is not a missed draw")
	require.Equal(t, uint64(1), a59Missed(t, f, ids[1]), "the twin with no commit is billed")
}
