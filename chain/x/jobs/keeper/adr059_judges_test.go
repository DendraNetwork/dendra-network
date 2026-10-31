package keeper_test

import (
	"errors"
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Consensus epoch 15: jurors are declared judges that vote. One case per rule, each with
// the twin or the mutation that gives it its meaning, driving the shipped handlers and the shipped
// draws. The fixtures below write miners straight into the collections and do not go through
// gvSetMiner, because that helper declares every miner a judge (adr037_fixtures_test.go) and these
// cases need the undeclared ones.

// a59Miner registers an undeclared miner: record, registration height 0, an operator address derived
// from its id. It returns the operator (bech32).
func a59Miner(t *testing.T, f *fixture, id string, stake uint64) string {
	t.Helper()
	op := ssAddr20(t, f, "a59-op-"+id)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Creator: op, Operator: op, Stake: stake}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 0))
	return op
}

// a59Declare writes the state a `judge = true` declaration at `h` leaves, for the cases whose subject
// is not the handler (the handler has its own case below).
func a59Declare(t *testing.T, f *fixture, h uint64, ids ...string) {
	t.Helper()
	for _, id := range ids {
		require.NoError(t, f.keeper.MinerJudge.Set(f.ctx, id))
		require.NoError(t, f.keeper.MinerJudgeDeclaredHeight.Set(f.ctx, id, h))
		require.NoError(t, f.keeper.MinerMissedDraws.Remove(f.ctx, id))
	}
}

// a59Missed reads the counter with its zero: absent is 0, and only a read error is a failure.
func a59Missed(t *testing.T, f *fixture, id string) uint64 {
	t.Helper()
	n, err := f.keeper.MinerMissedDraws.Get(f.ctx, id)
	if errors.Is(err, collections.ErrNotFound) {
		return 0
	}
	require.NoError(t, err)
	return n
}

func a59RequireNoJudgeState(t *testing.T, f *fixture, id string) {
	t.Helper()
	declared, err := f.keeper.MinerJudge.Has(f.ctx, id)
	require.NoError(t, err)
	require.False(t, declared, "the pool entry must be forgotten")
	for name, has := range map[string]func() (bool, error){
		"declared height": func() (bool, error) { return f.keeper.MinerJudgeDeclaredHeight.Has(f.ctx, id) },
		"last verdict":    func() (bool, error) { return f.keeper.MinerLastVerdictHeight.Has(f.ctx, id) },
		"missed draws":    func() (bool, error) { return f.keeper.MinerMissedDraws.Has(f.ctx, id) },
	} {
		ok, hErr := has()
		require.NoError(t, hErr)
		require.False(t, ok, "%s must be forgotten", name)
	}
}

// Decision 1: the declaration is the operator's, writes the pool entry and the date, and `false`
// forgets the four entries.
func TestADR059_DeclareJudgeIsSignedByTheOperatorAndWritesTheFourEntries(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op := a59Miner(t, f, "mD", 1000)
	stranger := ssAddr20(t, f, "a59-stranger")
	at := func(h int64) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h) }

	_, err := srv.DeclareJudge(at(42), &types.MsgDeclareJudge{Creator: stranger, MinerId: "mD", Judge: true})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "only the key that signs the verdicts seats or unseats the miner")
	_, err = srv.DeclareJudge(at(42), &types.MsgDeclareJudge{Creator: op, MinerId: "nobody", Judge: true})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
	declared, err := f.keeper.MinerJudge.Has(f.ctx, "mD")
	require.NoError(t, err)
	require.False(t, declared, "a refused declaration writes nothing")

	_, err = srv.DeclareJudge(at(42), &types.MsgDeclareJudge{Creator: op, MinerId: "mD", Judge: true})
	require.NoError(t, err)
	declared, err = f.keeper.MinerJudge.Has(f.ctx, "mD")
	require.NoError(t, err)
	require.True(t, declared)
	h, err := f.keeper.MinerJudgeDeclaredHeight.Get(f.ctx, "mD")
	require.NoError(t, err)
	require.Equal(t, uint64(42), h)
	emitted := false
	for _, ev := range sdk.UnwrapSDKContext(f.ctx).EventManager().Events() {
		if ev.Type == "judge_declared" {
			emitted = true
		}
	}
	require.True(t, emitted, "each declaration is a signed transaction with an event, so the count per identity is a reading")

	// The way back (decision 4): a second declaration rewrites the date and empties the counter.
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "mD", 2))
	_, err = srv.DeclareJudge(at(50), &types.MsgDeclareJudge{Creator: op, MinerId: "mD", Judge: true})
	require.NoError(t, err)
	h, err = f.keeper.MinerJudgeDeclaredHeight.Get(f.ctx, "mD")
	require.NoError(t, err)
	require.Equal(t, uint64(50), h)
	require.Equal(t, uint64(0), a59Missed(t, f, "mD"), "declaring again resets the missed draws")

	// Leaving forgets the four entries, as exit and eviction do.
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "mD", 49))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "mD", 1))
	_, err = srv.DeclareJudge(at(51), &types.MsgDeclareJudge{Creator: op, MinerId: "mD", Judge: false})
	require.NoError(t, err)
	a59RequireNoJudgeState(t, f, "mD")
}

// Decision 2: both draws take declared judges and nobody else. The twin (nobody declared) and the
// mutation (drop the filter and six are seated) are the same assertion read twice.
func TestADR059_OnlyADeclaredJudgeIsSeatedByEitherDraw(t *testing.T) {
	f := initFixture(t)
	ids := []string{"j0", "j1", "j2", "j3", "j4", "j5"}
	for _, id := range ids {
		a59Miner(t, f, id, 1000)
	}
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jOnly", 1000))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jOnly", types.Job{JobId: "jOnly", State: "open+paid", Fee: 1000}))
	require.NoError(t, f.keeper.Beacon.Set(f.ctx, "jOnly", types.Beacon{JobId: "jOnly", Seed: "seed-public"}))
	require.NoError(t, f.keeper.BlockHash.Set(f.ctx, 1000, []byte("the-bench-block-hash")))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(1000)

	got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jOnly", "", "")
	require.NoError(t, err)
	require.Empty(t, got, "six vital, present miners of the frozen pool and no declaration: nobody is a juror")
	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(ctx, "jOnly", "", ""))
	_, rErr := f.keeper.AuditCommittee.Get(ctx, keeper.RedoAnchorKeyForTest("jOnly"))
	require.Error(t, rErr, "the re-adjudication draw seats nobody either, and anchors nothing")

	a59Declare(t, f, 1000, "j1", "j3", "j5")
	got, err = f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jOnly", "", "")
	require.NoError(t, err)
	require.ElementsMatch(t, []string{"j1", "j3", "j5"}, got, "the jury is exactly the declared judges, every one of them seated")
	got, err = f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jOnly", "j3", "")
	require.NoError(t, err)
	require.ElementsMatch(t, []string{"j1", "j5"}, got, "the primary is still excluded before anything else")

	// The re-adjudication draw: the same predicate, after the exclusions it already had.
	a59Declare(t, f, 1000, ids...)
	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(ctx, "jOnly", "", ""))
	raw, err := f.keeper.AuditCommittee.Get(ctx, keeper.RedoAnchorKeyForTest("jOnly"))
	require.NoError(t, err)
	orig, err := f.keeper.AssignedCommitteeForTest(ctx, "jOnly", keeper.CommitteeSize)
	require.NoError(t, err)
	redo := splitNonEmpty(raw)
	require.Len(t, redo, len(ids)-len(orig), "every declared judge outside the work committee is seated")
	for _, m := range redo {
		require.False(t, orig[m], "the work committee stays excluded from the redo jury")
	}
}

// Decisions 2, 3 and 4 on the predicate itself: the window on the last answer, the trial on the
// declaration, the governed window, and the cap on missed draws.
func TestADR059_WindowTrialAndMissedDrawCap(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // judge window = juror freshness (259200), cap = the compiled count
	a59Miner(t, f, "jw", 1000)
	const window = uint64(259200)

	// Undeclared: never a candidate, whatever its history (decision 10).
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "jw", 1000))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 1000, p))
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Remove(f.ctx, "jw"))

	// Trial (decision 4): declared at 100 with no answer yet, a candidate for one window.
	a59Declare(t, f, 100, "jw")
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 100+window, p), "the last block of the trial")
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 101+window, p), "one block past it: out until an answer or a new declaration")

	// An answer reopens the window from its own height (decision 2).
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "jw", 500000))
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500000+window, p))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001+window, p))

	// The governed window moves the edge; 0 selects the freshness window, governed or compiled.
	g := p
	g.JudgeVerdictWindowBlocks = 300000
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500000+300000, g))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001+300000, g))
	fr := p
	fr.JurorFreshnessBlocks = 1000
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 501000, fr))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 501001, fr), "at 0 the judge window follows a governed freshness of 1000")

	// The cap (decision 3): at judge_missed_draws_max the judge is out even with a fresh answer.
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "jw", keeper.JudgeMissedDrawsMaxForTest-1))
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001, p))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "jw", keeper.JudgeMissedDrawsMaxForTest))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001, p), "at the compiled cap the judge is not a candidate")
	one := p
	one.JudgeMissedDrawsMax = 1
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "jw", 1))
	require.False(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001, one), "governed to 1: one missed draw excludes")
	require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "jw", 500001, p), "at the compiled cap of %d, one missed draw does not", keeper.JudgeMissedDrawsMaxForTest)
}

// a59Audit opens an optimistic audit on `jobId`: `seats` declared judges anchored on its audit jury,
// a primary, a retained fee owed to a client with no balance, and a resolution deadline at `deadline`.
// It returns the juror ids and their operators, in anchored order.
func a59Audit(t *testing.T, f *fixture, jobId string, seats int, deadline int64) ([]string, []string) {
	t.Helper()
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	a59Miner(t, f, "p59-"+jobId, 1_000_000)
	ids := make([]string, 0, seats)
	ops := make([]string, 0, seats)
	for i := 0; i < seats; i++ {
		id := fmt.Sprintf("s59-%s-%d", jobId, i)
		ids = append(ids, id)
		ops = append(ops, a59Miner(t, f, id, 1000))
	}
	a59Declare(t, f, 1, ids...)
	aeAnchor(f, t, jobId, ids...)
	clientAcc := sdk.AccAddress([]byte("client_adr059_______"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
		JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "p59-" + jobId, Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, 70000))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, jobId, 1))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(deadline, jobId)))
	return ids, ops
}

// Decision 3: the seats that never answered are counted once at each of the four points where an
// audit leaves the disputed state, and never on a deferral.
func TestADR059_MissedDrawsAreCountedOnceAtEachOfTheFourConclusionPoints(t *testing.T) {
	t.Run("a real resolution of resolveDisputedAudit", func(t *testing.T) {
		f := initFixture(t)
		ids, _ := a59Audit(t, f, "jRes", 5, 6)
		for _, id := range ids[:4] { // bar = max(4, ceil(2/3 x 5)) = 4: four free bytes vindicate
			aeVerdict(f, t, "jRes", id, "x")
		}
		require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
		job, err := f.keeper.Job.Get(f.ctx, "jRes")
		require.NoError(t, err)
		require.Contains(t, job.State, "+resolved+vindicated+quorum", "premise: the audit concluded")
		for _, id := range ids[:4] {
			require.Equal(t, uint64(0), a59Missed(t, f, id), "a seat that answered is not counted")
		}
		require.Equal(t, uint64(1), a59Missed(t, f, ids[4]), "the mute seat is counted once")
		_, aErr := f.keeper.AuditCommittee.Get(f.ctx, "jRes")
		require.Error(t, aErr, "the seats are purged after the count, so no second pass can count them again")
	})

	t.Run("a deferral counts nothing", func(t *testing.T) {
		f := initFixture(t)
		ids, _ := a59Audit(t, f, "jDef", 5, 6)
		require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
		job, err := f.keeper.Job.Get(f.ctx, "jDef")
		require.NoError(t, err)
		require.NotContains(t, job.State, "resolved", "premise: below the bar the audit defers")
		for _, id := range ids {
			require.Equal(t, uint64(0), a59Missed(t, f, id), "a deferral is not a conclusion: nobody is counted yet")
		}
	})

	t.Run("AdjudicateDispute", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		disp, _ := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_adr059_adj_")))
		const big = uint64(1_000_000_000_000)
		p := types.DefaultParams()
		p.VerificationMode = 1
		p.HoldBps = 10000
		p.DisputeWindow = 10
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
		aeFundModule(f)
		for _, id := range []string{"mA", "mB", "mC"} {
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: big}))
		}
		for _, id := range []string{"mD", "mE", "mF", "mG", "mH"} {
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		}
		clientAcc := sdk.AccAddress([]byte("client_adr059_adj___"))
		client, _ := f.addressCodec.BytesToString(clientAcc)
		f.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAdj__mA", types.Commit{ResultCommit: "1,0,0"}))
		for _, id := range []string{"mD", "mE", "mF"} {
			require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAdj__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
		}
		redoAnchor(f, t, "jAdj", "mD", "mE", "mF", "mG") // mG is seated and silent
		aeAnchor(f, t, "jAdj", "mH")                     // mH holds the one audit seat, silent
		require.NoError(t, gvSetJob(f, f.ctx, "jAdj", types.Job{
			JobId: "jAdj", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: 100000,
			Disputer: disp, DisputeHeight: 1,
		}))
		_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jAdj"})
		require.NoError(t, err, "premise: three re-commits of four seats conclude the adjudication")
		for _, id := range []string{"mD", "mE", "mF"} {
			require.Equal(t, uint64(0), a59Missed(t, f, id), "a redo seat answers with its re-commit, the key this path tallies")
		}
		require.Equal(t, uint64(0), a59Missed(t, f, "mG"), "a silent seat held only on the redo jury is not billed")
		require.Equal(t, uint64(1), a59Missed(t, f, "mH"), "the silent audit seat is counted once at this conclusion")
	})

	t.Run("unwindStrandedRetention", func(t *testing.T) {
		f := initFixture(t)
		ids, _ := a59Audit(t, f, "jUnw", 3, 999999)
		aeVerdict(f, t, "jUnw", ids[1], "x")
		require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, "jUnw", unwParams(500)), "premise: the retention is unwound")
		require.Equal(t, uint64(1), a59Missed(t, f, ids[0]))
		require.Equal(t, uint64(0), a59Missed(t, f, ids[1]), "the one seat that answered is not counted")
		require.Equal(t, uint64(1), a59Missed(t, f, ids[2]))
	})

	t.Run("ResolveDispute by the authority", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, _ := a59Audit(t, f, "jGov", 2, 999999)
		aeVerdict(f, t, "jGov", ids[0], "0")
		_, err := srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jGov", Upheld: false})
		require.NoError(t, err)
		require.Equal(t, uint64(0), a59Missed(t, f, ids[0]))
		require.Equal(t, uint64(1), a59Missed(t, f, ids[1]))
	})
}

// Decision 3, the other half: an answer on an audit the judge is still anchored on dates the judge and
// clears its counter; an answer after the conclusion is accepted as today and moves nothing.
func TestADR059_AnAnswerResetsBeforeTheConclusionAndNothingAfter(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	ids, ops := a59Audit(t, f, "jReset", 3, 999999)
	for _, id := range ids {
		require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, id, 2))
	}
	redoAnchor(f, t, "jReset", ids[2]) // the third judge sits on the redo jury of the same job
	at := func(h int64) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h) }

	_, err := srv.CreateCommit(at(5), &types.MsgCreateCommit{Creator: ops[0], JobId: "jReset__verdict__" + ids[0], ResultCommit: "x"})
	require.NoError(t, err)
	last, err := f.keeper.MinerLastVerdictHeight.Get(f.ctx, ids[0])
	require.NoError(t, err)
	require.Equal(t, uint64(5), last, "a verdict on an anchored seat is dated")
	require.Equal(t, uint64(0), a59Missed(t, f, ids[0]), "and clears the missed draws")

	_, err = srv.CreateCommit(at(5), &types.MsgCreateCommit{Creator: ops[2], JobId: "jReset__redo__" + ids[2], ResultCommit: "1,2,3"})
	require.NoError(t, err)
	last, err = f.keeper.MinerLastVerdictHeight.Get(f.ctx, ids[2])
	require.NoError(t, err)
	require.Equal(t, uint64(5), last, "a re-commit on an anchored redo seat is that jury's answer and is dated too")
	require.Equal(t, uint64(0), a59Missed(t, f, ids[2]))

	// A verdict from a miner the anchor does not name is accepted as today and dates nothing: the
	// membership is read from the anchor and from nothing else.
	outsider := a59Miner(t, f, "outsider", 1000)
	_, err = srv.CreateCommit(at(5), &types.MsgCreateCommit{Creator: outsider, JobId: "jReset__verdict__outsider", ResultCommit: "x"})
	require.NoError(t, err)
	_, lErr := f.keeper.MinerLastVerdictHeight.Get(f.ctx, "outsider")
	require.Error(t, lErr, "not seated: nothing is dated")

	// The conclusion purges the anchors and counts the seat that stayed mute.
	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jReset", Upheld: false})
	require.NoError(t, err)
	require.Equal(t, uint64(3), a59Missed(t, f, ids[1]), "two before, counted once at the conclusion: three")

	// A late blind vote on a dead audit buys nothing.
	_, err = srv.CreateCommit(at(7), &types.MsgCreateCommit{Creator: ops[1], JobId: "jReset__verdict__" + ids[1], ResultCommit: "x"})
	require.NoError(t, err, "accepted as today: CreateCommit has no guard on a resolved job")
	_, lErr = f.keeper.MinerLastVerdictHeight.Get(f.ctx, ids[1])
	require.Error(t, lErr, "a verdict after the conclusion dates nothing")
	require.Equal(t, uint64(3), a59Missed(t, f, ids[1]), "and clears nothing")
}

// Decision 5: the jury is the smaller of the draw size and the eligible judges; the size and its
// default do not move.
func TestADR059_TheJuryIsTheSmallerOfTheDrawSizeAndTheEligibleJudges(t *testing.T) {
	for _, c := range []struct {
		declared int
		size     uint64
		want     int
	}{
		{7, 0, 7},
		{20, 0, keeper.AuditCommitteeDrawSizeForTest},
		{20, 5, 5},
	} {
		f := initFixture(t)
		p := types.DefaultParams()
		p.AuditCommitteeDrawSize = c.size
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		for i := 0; i < 20; i++ {
			id := fmt.Sprintf("q%02d", i)
			a59Miner(t, f, id, 1000)
			if i < c.declared {
				a59Declare(t, f, 1000, id)
			}
		}
		require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jSize", 1000))
		got, err := f.keeper.DrawAuditCommitteeForTest(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(1000), "s", "jSize", "", "")
		require.NoError(t, err)
		require.Len(t, got, c.want, "%d declared of 20, audit_committee_draw_size=%d", c.declared, c.size)
	}
}

// Decision 4, the way back: a judge at the cap with no open audit has nothing to vote on, and returns
// by declaring again.
func TestADR059_AnExcludedJudgeComesBackByDeclaringAgain(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op := a59Miner(t, f, "jb", 1000)
	a59Declare(t, f, 1, "jb")
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "jb", keeper.JudgeMissedDrawsMaxForTest))
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "jBack", 1000))
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(1000)

	got, err := f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jBack", "", "")
	require.NoError(t, err)
	require.Empty(t, got, "at the cap, and with no anchored audit to answer, the judge is not drawn")

	_, err = srv.DeclareJudge(ctx, &types.MsgDeclareJudge{Creator: op, MinerId: "jb", Judge: true})
	require.NoError(t, err)
	got, err = f.keeper.DrawAuditCommitteeForTest(ctx, "s", "jBack", "", "")
	require.NoError(t, err)
	require.Equal(t, []string{"jb"}, got, "a new declaration reopens the trial window and empties the counter")
}

// Decision 1: exit and eviction clear the four entries, so an id that comes back starts with no judge
// history.
func TestADR059_ExitAndEvictionForgetTheJudgeState(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	aeFundModule(f)
	op := a59Miner(t, f, "jx", 1000)
	a59Declare(t, f, 1, "jx")
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, "jx", 3))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, "jx", 1))
	_, err := srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: op, MinerId: "jx"})
	require.NoError(t, err)
	a59RequireNoJudgeState(t, f, "jx")

	g := initFixture(t)
	aeFundModule(g)
	a59Miner(t, g, "je", 1000) // registered at 0, never committed: evicted past the prune window
	a59Declare(t, g, 1, "je")
	require.NoError(t, g.keeper.MinerLastVerdictHeight.Set(g.ctx, "je", 3))
	require.NoError(t, g.keeper.MinerMissedDraws.Set(g.ctx, "je", 1))
	require.NoError(t, g.keeper.EndBlock(sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(700000)))
	_, mErr := g.keeper.Miner.Get(g.ctx, "je")
	require.Error(t, mErr, "premise: the miner was evicted")
	a59RequireNoJudgeState(t, g, "je")
}

// Decision 1, genesis: the pool and the counts travel; the declaration is re-dated at the import
// anchor and the last answer is not carried.
func TestADR059_TheJuryPoolAndTheMissedDrawsTravelAndTheDatesDoNot(t *testing.T) {
	f := initFixture(t)
	m := registeredMiner(t, 0x31, 1000)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, m.MinerId, m))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, m.MinerId, 0))
	a59Declare(t, f, 7, m.MinerId)
	require.NoError(t, f.keeper.MinerLastVerdictHeight.Set(f.ctx, m.MinerId, 9))
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, m.MinerId, 2))

	exported, err := f.keeper.ExportGenesis(f.ctx)
	require.NoError(t, err)
	require.Equal(t, []string{m.MinerId}, exported.MinerJudge)
	require.Equal(t, []types.Uint64Entry{{Key: m.MinerId, Value: 2}}, exported.MinerMissedDraws)

	g := initFixture(t)
	ctx := sdk.UnwrapSDKContext(g.ctx).WithBlockHeight(500)
	require.NoError(t, g.keeper.InitGenesis(ctx, *exported))
	declared, err := g.keeper.MinerJudge.Has(ctx, m.MinerId)
	require.NoError(t, err)
	require.True(t, declared, "the declaration travels")
	require.Equal(t, uint64(2), a59Missed(t, g, m.MinerId), "the count travels as is")
	h, err := g.keeper.MinerJudgeDeclaredHeight.Get(ctx, m.MinerId)
	require.NoError(t, err)
	require.Equal(t, keeper.FrozenPoolAnchorForTest(500), h, "re-dated at the import anchor, like the registration height")
	_, lErr := g.keeper.MinerLastVerdictHeight.Get(ctx, m.MinerId)
	require.Error(t, lErr, "the last answer is not carried: the reopened trial decides on the new chain")
}

// Decision 3, one definition of an answer: `juryAnswer` is read when the commit lands and when the
// audit concludes, so a key that is no jury's answer moves nothing at either point and a key that is
// one is an answer at both. `CreateCommit` accepts any role key from the operator, which is why the
// pairing of key and seat is written once and measured at both ends here.
func TestADR059_AnAnswerIsTheKeyTheSeatsJuryTalliesAtBothEnds(t *testing.T) {
	at := func(f *fixture, h int64) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h) }
	noDate := func(t *testing.T, f *fixture, id string) {
		t.Helper()
		_, err := f.keeper.MinerLastVerdictHeight.Get(f.ctx, id)
		require.ErrorIs(t, err, collections.ErrNotFound, "%s: nothing is dated", id)
	}

	t.Run("a re-commit from an audit seat is no answer", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, ops := a59Audit(t, f, "jRk", 2, 999999)
		require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[0], 2))
		_, err := srv.CreateCommit(at(f, 5), &types.MsgCreateCommit{Creator: ops[0], JobId: "jRk__redo__" + ids[0], ResultCommit: "1,2,3"})
		require.NoError(t, err, "accepted as today: CreateCommit pairs no key to a seat")
		noDate(t, f, ids[0])
		require.Equal(t, uint64(2), a59Missed(t, f, ids[0]), "no tally reads that key, so the counter is not cleared")
		_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jRk", Upheld: false})
		require.NoError(t, err)
		require.Equal(t, uint64(3), a59Missed(t, f, ids[0]), "mute at the conclusion, as the write side already said")
	})

	t.Run("the redo-verdict form is no answer for a redo seat", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, ops := a59Audit(t, f, "jRv", 1, 999999)
		redoAnchor(f, t, "jRv", ids[0]) // on both juries, so the conclusion reads the seat
		_, err := srv.CreateCommit(at(f, 5), &types.MsgCreateCommit{Creator: ops[0], JobId: "jRv__redo__verdict__" + ids[0], ResultCommit: "x"})
		require.NoError(t, err)
		noDate(t, f, ids[0])
		_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jRv", Upheld: false})
		require.NoError(t, err)
		require.Equal(t, uint64(1), a59Missed(t, f, ids[0]), "no tally reads that form: a missed draw at both ends")
	})

	t.Run("a redo-only verdict is no answer, and a redo-only seat is not billed", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, ops := a59Audit(t, f, "jRw", 1, 999999)
		redoAnchor(f, t, "jRw", ids[0])
		require.NoError(t, f.keeper.AuditCommittee.Remove(f.ctx, "jRw"))
		require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[0], 2))
		_, err := srv.CreateCommit(at(f, 5), &types.MsgCreateCommit{Creator: ops[0], JobId: "jRw__verdict__" + ids[0], ResultCommit: "x"})
		require.NoError(t, err)
		noDate(t, f, ids[0])
		require.Equal(t, uint64(2), a59Missed(t, f, ids[0]))
		_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jRw", Upheld: false})
		require.NoError(t, err)
		require.Equal(t, uint64(2), a59Missed(t, f, ids[0]), "the untallied verdict cleared nothing, and the redo-only seat bills nothing")
	})
}

// Decision 3 counts judges: a seat whose miner left the pool while the audit was open, or never
// declared, is not billed at the conclusion, since `judge = false` forgets the four entries and a count
// on an identity outside the pool would travel with the genesis. A seat whose miner declared after it
// was drawn is the next case's subject.
func TestADR059_ASeatThatIsNotADeclaredJudgeAtTheConclusionIsNotCounted(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	ids, ops := a59Audit(t, f, "jUd", 2, 999999)
	a59Miner(t, f, "s59-jUd-never", 1000) // registered and seated below, never declared
	aeAnchor(f, t, "jUd", ids[0], ids[1], "s59-jUd-never")
	_, err := srv.DeclareJudge(f.ctx, &types.MsgDeclareJudge{Creator: ops[1], MinerId: ids[1], Judge: false})
	require.NoError(t, err)
	a59RequireNoJudgeState(t, f, ids[1])

	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jUd", Upheld: false})
	require.NoError(t, err)
	require.Equal(t, uint64(1), a59Missed(t, f, ids[0]), "the declared, mute seat is counted")
	a59RequireNoJudgeState(t, f, ids[1])
	a59RequireNoJudgeState(t, f, "s59-jUd-never")
}

// Decision 3 bills the seats that summoned a judge. A seat drawn before the declaration its judge holds
// at the conclusion did not: every seat open at the upgrade height (drawn under the epoch 14 rule,
// before any declaration existed), and every seat held before a new declaration (decision 4). The
// proof is the anchoring height `anchorCommittee` writes, strictly below the declaration height; each
// twin below is billed because nothing proves it.
func TestADR059_ASeatDrawnBeforeTheDeclarationItsJudgeHoldsIsNotBilled(t *testing.T) {
	at := func(f *fixture, h int64) sdk.Context { return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h) }
	// openAudit writes a disputed audit with a retention the unwind can refund, its primary, and its
	// jury anchored at `anchoredAt` by the shipped writer.
	openAudit := func(t *testing.T, f *fixture, jobId string, anchoredAt int64, members string) {
		t.Helper()
		a59Miner(t, f, "p-"+jobId, 1_000_000)
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, anchoredAt), jobId, members))
		clientAcc := sdk.AccAddress([]byte("client_adr059_seat__"))
		client, err := f.addressCodec.BytesToString(clientAcc)
		require.NoError(t, err)
		f.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "p-" + jobId, Client: client, Fee: 100000}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, 70000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, jobId, 1))
	}
	resolve := func(t *testing.T, f *fixture, jobId string) {
		t.Helper()
		_, err := keeper.NewMsgServerImpl(f.keeper).ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: jobId, Upheld: false})
		require.NoError(t, err)
	}

	t.Run("the seats open at the upgrade are not billed, a seat drawn under the declaration is", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		p := types.DefaultParams()
		p.VerificationMode = 1
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		opJ := a59Miner(t, f, "uJ", 1000) // declares once the upgrade has run
		a59Miner(t, f, "uN", 1000)        // never declares
		before := []string{"jU1", "jU2", "jU3", "jU4"}
		for _, j := range before {
			openAudit(t, f, j, 16, "uJ,uN")
		}
		_, err := srv.DeclareJudge(at(f, 50000), &types.MsgDeclareJudge{Creator: opJ, MinerId: "uJ", Judge: true})
		require.NoError(t, err)
		openAudit(t, f, "jU5", 50010, "uJ,uN")
		for i, j := range before {
			require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001+int64(i), j, unwParams(500)), "premise: %s is unwound", j)
		}
		require.Equal(t, uint64(0), a59Missed(t, f, "uJ"), "four seats drawn before the declaration, four conclusions: nothing billed")
		require.True(t, f.keeper.EligibleAsJudgeAtForTest(f.ctx, "uJ", 50001, p), "the new judge is still a candidate")
		require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100010, "jU5", unwParams(500)), "premise: jU5 is unwound")
		require.Equal(t, uint64(1), a59Missed(t, f, "uJ"), "the twin: a mute seat drawn under the declaration is billed")
		require.Equal(t, uint64(0), a59Missed(t, f, "uN"), "never declared: never billed")
	})

	t.Run("a seat with no date, a seat dated in the block of the declaration, a judge with no declaration height", func(t *testing.T) {
		f := initFixture(t)
		undated, _ := a59Audit(t, f, "jNo", 1, 999999) // declared at 1, anchored with no date
		tie, _ := a59Audit(t, f, "jTie", 1, 999999)
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 1), "jTie", tie[0]))
		nodecl, _ := a59Audit(t, f, "jNoDecl", 1, 999999)
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 0), "jNoDecl", nodecl[0]))
		require.NoError(t, f.keeper.MinerJudgeDeclaredHeight.Remove(f.ctx, nodecl[0]))
		for _, j := range []string{"jNo", "jTie", "jNoDecl"} {
			resolve(t, f, j)
		}
		require.Equal(t, uint64(1), a59Missed(t, f, undated[0]), "no anchoring date: nothing proves the seat predates the declaration")
		require.Equal(t, uint64(1), a59Missed(t, f, tie[0]), "anchored in the block of the declaration: billed")
		require.Equal(t, uint64(1), a59Missed(t, f, nodecl[0]), "declared with no declaration height: billed")
	})

	t.Run("a new declaration takes the seats drawn before it out of the count", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, ops := a59Audit(t, f, "jAgain", 2, 999999) // both declared at 1
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 10), "jAgain", ids[0]+","+ids[1]))
		for _, id := range ids {
			require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, id, 2))
		}
		_, err := srv.DeclareJudge(at(f, 20), &types.MsgDeclareJudge{Creator: ops[0], MinerId: ids[0], Judge: true})
		require.NoError(t, err)
		resolve(t, f, "jAgain")
		require.Equal(t, uint64(0), a59Missed(t, f, ids[0]), "declared again at 20 after its seat was drawn at 10: not billed")
		require.Equal(t, uint64(3), a59Missed(t, f, ids[1]), "the twin, still under the declaration it was drawn under: billed")
	})

	t.Run("a judge on both juries is billed when one of its seats was drawn under its declaration", func(t *testing.T) {
		f := initFixture(t)
		srv := keeper.NewMsgServerImpl(f.keeper)
		ids, ops := a59Audit(t, f, "jTwo", 2, 999999)
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 10), "jTwo", ids[0]+","+ids[1]))
		for i := range ids {
			_, err := srv.DeclareJudge(at(f, 50), &types.MsgDeclareJudge{Creator: ops[i], MinerId: ids[i], Judge: true})
			require.NoError(t, err)
		}
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 60), "jTwo__redo", ids[0]))
		resolve(t, f, "jTwo")
		require.Equal(t, uint64(1), a59Missed(t, f, ids[0]), "audit seat drawn at 10, redo seat at 60, declared at 50: billed once")
		require.Equal(t, uint64(0), a59Missed(t, f, ids[1]), "its one seat drawn at 10, before the declaration at 50: not billed")
	})

	t.Run("a seat carried across a genesis import: billed after a zero-height import, not above it", func(t *testing.T) {
		f := initFixture(t)
		require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
		judge := registeredMiner(t, 0x41, 1000)
		primary := registeredMiner(t, 0x61, 1_000_000)
		for _, m := range []types.Miner{judge, primary} {
			require.NoError(t, f.keeper.Miner.Set(f.ctx, m.MinerId, m))
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, m.MinerId, 0))
		}
		a59Declare(t, f, 7, judge.MinerId)
		clientAcc := sdk.AccAddress([]byte("client_adr059_gen___"))
		client, err := f.addressCodec.BytesToString(clientAcc)
		require.NoError(t, err)
		require.NoError(t, gvSetJob(f, f.ctx, "jGen", types.Job{JobId: "jGen", State: "open+paid+optimistic+disputed", MinerId: primary.MinerId, Client: client, Fee: 100000}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jGen", 70000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jGen", 1))
		require.NoError(t, f.keeper.AnchorCommitteeForTest(at(f, 40), "jGen", judge.MinerId)) // drawn under the declaration of height 7
		exported, err := f.keeper.ExportGenesis(at(f, 100))
		require.NoError(t, err)

		g := initFixture(t)
		aeFundModule(g)
		g.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, g.keeper.InitGenesis(at(g, 500), *exported))
		require.True(t, g.keeper.UnwindStrandedRetentionForTest(g.ctx, 100000, "jGen", unwParams(500)), "premise: the carried audit is unwound")
		require.Equal(t, uint64(0), a59Missed(t, g, judge.MinerId), "seat re-dated by its age (500-60), declaration at the import anchor (499): not billed")

		z := initFixture(t)
		aeFundModule(z)
		z.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, z.keeper.InitGenesis(at(z, 0), *exported))
		require.True(t, z.keeper.UnwindStrandedRetentionForTest(z.ctx, 100000, "jGen", unwParams(500)), "premise: the carried audit is unwound")
		require.Equal(t, uint64(1), a59Missed(t, z, judge.MinerId), "zero-height import: the seat and the declaration both read 0, nothing proves the order: billed")

		aeFundModule(f)
		f.bank.setBalance(clientAcc, sdk.NewCoins())
		require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100000, "jGen", unwParams(500)), "premise: the same audit is unwound where it was drawn")
		require.Equal(t, uint64(1), a59Missed(t, f, judge.MinerId), "the twin, on the chain that drew it under the declaration: billed")
	})
}

// Decision 3, a judge on both juries of one job: counted once when mute on both, and answered on both
// when it re-committed, the reading `noteJudgeAnswer` made when that commit landed. The third judge is
// walked first under the audit jury, where a verdict-only reading would bill it.
func TestADR059_AJudgeSeatedOnBothJuriesIsCountedOnceAndAnsweredOnce(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	ids, ops := a59Audit(t, f, "jDbl", 3, 999999)
	redoAnchor(f, t, "jDbl", ids[1], ids[2])
	require.NoError(t, f.keeper.MinerMissedDraws.Set(f.ctx, ids[2], 1))
	_, err := srv.CreateCommit(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5), &types.MsgCreateCommit{Creator: ops[2], JobId: "jDbl__redo__" + ids[2], ResultCommit: "1,2,3"})
	require.NoError(t, err)
	require.Equal(t, uint64(0), a59Missed(t, f, ids[2]), "a re-commit from a redo seat is its answer when it lands")

	_, err = srv.ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jDbl", Upheld: false})
	require.NoError(t, err)
	require.Equal(t, uint64(1), a59Missed(t, f, ids[0]), "one seat, mute: one")
	require.Equal(t, uint64(1), a59Missed(t, f, ids[1]), "two seats, mute on both: counted once")
	require.Equal(t, uint64(0), a59Missed(t, f, ids[2]), "two seats, one re-commit: answered, whichever jury walks it first")
}
