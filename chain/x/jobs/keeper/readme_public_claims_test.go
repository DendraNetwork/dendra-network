package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The public README claims, measured or refuted.
//
// The public README states three things to a future reader (an auditor, an exchange, a miner about to
// put capital at risk):
//
//	(A) "The audit committee is drawn and anchored on-chain ... and only its summoned members can vote"
//	(B) "A hard slash requires at least two thirds of the anchored seats to return \"invalid\",
//	     so a minority verdict cannot slash an honest miner"
//	(C) "With no anchored committee, no hard slash fires at all (fail-closed)"
//
// Error strings read out of the adjudication code are not a measurement, and a public README must not
// rest on "probably true". The case to follow to the end is the one (C) names, `seats == 0`:
// ⌈2/3 x 0⌉ = 0, and a null denominator makes a relative bar trivially cleared. A claim whose
// demonstration stops at the edge of the case it names is not demonstrated.
//
// (A) and (C) hold, and the `seats == 0` case with them. (B) is the fragile one: `Params.Validate()`
// accepts `audit_min_quorum = 0` together with `verification_mode = 1`, so under default parameters
// only the relative bar stands between a minority and a hard slash. An absolute fallback such as
// `voters >= 4`, a count sized for a 5-seat committee, is 27 % of the 15 seats the audit draw targets, the
// "minority verdict" the sentence declares impossible, and the default timeout path carries no other
// guard.
//
// Documenting a false claim does not make it true, and neither can a launch script: setting the
// parameter on one operator's own deployments would rest a published security property on an optional
// setting. The property belongs to the code: `auditRelativeBar` is the single source of both outcomes,
// relative in both branches, so the sentence holds for the chain and not for one launch kit.
//
// A neighbouring failure mode in the same decision has its own guard: see
// `TestReadjudicationTieNeitherSlashesNorVindicates`.

// (C) and the `seats == 0` case: a relative bar over a null denominator.
//
// `auditSlashDecision` computes `need = max(audit_min_quorum, ⌈2/3 x seats⌉)`. At `seats == 0` the
// second term is 0, so only the governed floor still holds anything back. This test follows the
// computation to the end instead of trusting the fail-closed check upstream, because a guard that is
// correct only thanks to its caller breaks the day it gets a second caller.
func TestREADMEClaimCZeroSeatsNeverSlash(t *testing.T) {
	for _, quorum := range []uint64{0, 4, 10} {
		t.Run(fmt.Sprintf("audit_min_quorum=%d", quorum), func(t *testing.T) {
			p := types.DefaultParams()
			p.AuditMinQuorum = quorum

			// Empty electorate: what `auditVerdictTally` returns when nothing is anchored.
			require.False(t, keeper.AuditSlashDecisionForTest(p, false, 0, 0, 0),
				"no seats and no voters: ⌈2/3 x 0⌉ = 0 must never be enough for a hard slash")
			// Even if a future caller passed `cheated=true` by mistake, the floor holds.
			require.False(t, keeper.AuditSlashDecisionForTest(p, true, 0, 0, 0),
				"the participation floor must hold on its own when the denominator is null")
			require.False(t, keeper.AuditVindicateDecisionForTest(p, true, 0, 0, 0),
				"symmetry: zero seats must not vindicate either, or the grief has a dual")

			// What makes all of this safe: the effective floor is never zero.
			require.Greater(t, keeper.EffectiveSlashFloorForTest(p), 0,
				"a null participation floor would turn \"nobody voted\" into a decision")
		})
	}
}

// (C) end to end: nothing anchored (neither verdicts nor redo commits), yet real registered miners
// re-commit in disagreement. Adjudication must refuse, and the primary's stake must be intact to the
// last unit.
//
// This differs from (A) below: there a committee is drawn and the voters are not part of it. Here
// nothing at all is anchored, the "job older than the mechanism, or a path outside optimistic mode"
// case that (C) describes.
func TestREADMEClaimCNoAnchorNoHardSlash(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_readme_c001")

	require.NoError(t, gvSetMiner(f, f.ctx, "mPrim", types.Miner{MinerId: "mPrim", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNoAnchor__mPrim", types.Commit{ResultCommit: "1,0,0"}))
	// Ten registered miners with real stake, re-committing in disagreement: everything a slash needs,
	// except the authorisation to vote.
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("r%02d", i)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1_000_000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNoAnchor__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// No aeAnchor, no redoAnchor: that absence is the subject of the test.

	require.NoError(t, gvSetJob(f, f.ctx, "jNoAnchor", types.Job{
		JobId: "jNoAnchor", State: "open+paid+optimistic+disputed", MinerId: "mPrim", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jNoAnchor"})
	require.Error(t, err, "with no anchored committee, adjudication must decide nothing (fail-closed)")

	m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
	require.Equal(t, adr033Big, m.Stake, "no hard slash may fire without an anchored committee")
	j, _ := f.keeper.Job.Get(f.ctx, "jNoAnchor")
	require.NotContains(t, j.State, "+resolved", "a refusal must not consume the dispute")
}

// (B) The two-thirds bar, and what it depends on.
//
// Two-sided, against the real committee size (read from the code, not restated here): a verdict at
// ⌈2/3⌉ - 1 seats does not slash, at ⌈2/3⌉ seats it does. That is the README sentence, measured.
func TestREADMEClaimBTwoThirdsOfAnchoredSeats(t *testing.T) {
	p := types.DefaultParams()
	p.AuditMinQuorum = 4 // the value the public launch script requires

	seats := keeper.AuditCommitteeDrawSizeForTest
	needed := (2*seats + 2) / 3
	require.Equal(t, 10, needed, "⌈2/3 x 15⌉ = 10 — if the draw size changes, this number must change with it")

	require.False(t, keeper.AuditSlashDecisionForTest(p, true, seats, needed-1, seats),
		"9 seats out of 15 (60 %) is a minority in the sense of the claim: it must not slash")
	require.True(t, keeper.AuditSlashDecisionForTest(p, true, seats, needed, seats),
		"10 seats out of 15 is two thirds: the slash must be able to bite, or the guard is dead")
	// The second lock, independent of the first: seats alone are not enough without the stake majority.
	require.False(t, keeper.AuditSlashDecisionForTest(p, false, seats, seats, seats),
		"15 \"invalid\" seats without a stake majority must not slash: two locks, not one")
	// Symmetry: vindication clears the same bar, so the two outcomes cannot drift apart.
	require.False(t, keeper.AuditVindicateDecisionForTest(p, true, seats, needed-1, seats),
		"vindicating at 9/15 would reopen the asymmetry between slash and vindication")
	require.True(t, keeper.AuditVindicateDecisionForTest(p, true, seats, needed, seats))
}

// What claim (B) requires, and what this test measures.
//
// The relative bar ⌈2/3 x anchored seats⌉ has to exist in both branches, not only under
// `audit_min_quorum > 0`. At the default (`audit_min_quorum = 0`, which `Params.Validate()` accepts
// together with `verification_mode = 1`), an absolute fallback of `voters >= 4`, a count sized for a
// 5-seat committee, is 27 % of the 15 seats the audit draw targets, and the audit timeout path calls
// `auditSlashDecision` with no other guard: four jurors would hard-slash an honest miner carrying
// eleven "valid" votes, on the default path.
//
// `auditRelativeBar` is the single source of both outcomes, hence relative in both branches. This test
// measures independence: same 4 voters, same 15-seat committee, same refusal, armed or at the default.
// The README sentence does not depend on an optional setting, the only form in which a security
// property can be published.
func TestREADMEClaimBNoLongerDependsOnTheLaunchParameter(t *testing.T) {
	seats := keeper.AuditCommitteeDrawSizeForTest

	defaultParams := types.DefaultParams()
	require.Zero(t, defaultParams.AuditMinQuorum, "premise: the default is the fallback floor")
	require.False(t, keeper.AuditSlashDecisionForTest(defaultParams, true, 4, 4, seats),
		"4 voters over 15 seats is 27 %: at the default too, that minority must not slash")
	require.False(t, keeper.AuditVindicateDecisionForTest(defaultParams, true, 4, 4, seats),
		"dual of the grief: 27 % must not vindicate a cheater either")

	armed := types.DefaultParams()
	armed.AuditMinQuorum = 4 // what the launch gate imposes
	require.Equal(t,
		keeper.AuditSlashDecisionForTest(defaultParams, true, 10, 10, seats),
		keeper.AuditSlashDecisionForTest(armed, true, 10, 10, seats),
		"at two thirds, armed and default must decide alike: the bar does not depend on the setting")
	require.True(t, keeper.AuditSlashDecisionForTest(defaultParams, true, 10, 10, seats),
		"...and it must bite at 10/15, otherwise the guard is dead")

	// The 5-seat committee is where absolute and relative coincide (⌈2/3 x 5⌉ = 4): the relative bar
	// must decide identically there, since both routes yield the same number.
	require.True(t, keeper.AuditSlashDecisionForTest(defaultParams, true, 4, 4, keeper.AuditLegacyFloorBasis),
		"at 5 seats, 4 \"invalid\" votes remain a near-unanimity: behaviour unchanged")

	// Parameter validation refuses to arm optimistic mode below 4 when the quorum is governed.
	noQuorum := types.DefaultParams()
	noQuorum.VerificationMode = 1
	noQuorum.AuditMinQuorum = 2
	require.Error(t, noQuorum.Validate(),
		"verification_mode=1 with a governed quorum below 4 must be refused by the chain itself")
}

// (B) end to end, on the default path: the audit timeout, which calls `auditSlashDecision` with no
// other guard. The function-level test above proves the rule; this one proves it is wired where it
// decides 80 % of a miner's bond, under the parameters a chain carries without being armed.
//
// Two-sided: 4 "invalid" over 15 seats (27 %) defers and nothing moves; 10 over 15 (two thirds) and
// the hard slash fires. Without the second case, a plain "never slash" posing as a guard would pass.
func TestREADMEClaimBAtTheDefaultTimeout(t *testing.T) {
	setup := func(t *testing.T, jobId string, nInvalid int) (*fixture, types.Params) {
		f := initFixture(t)
		p := types.DefaultParams() // deliberately not armed: that is the point
		p.VerificationMode = 1
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		aeReg(f, t, "mP", aeStake)

		seatIDs := []string{}
		for i := 0; i < keeper.AuditCommitteeDrawSizeForTest; i++ { // 15 anchored seats
			id := fmt.Sprintf("j%02d", i)
			seatIDs = append(seatIDs, id)
			aeReg(f, t, id, 1_000_000)
		}
		aeAnchor(f, t, jobId, seatIDs...)
		for i := 0; i < nInvalid; i++ { // only these answer, all with "invalid"
			aeVerdict(f, t, jobId, seatIDs[i], "0")
		}

		clientAcc := sdk.AccAddress([]byte("client_readme_b_" + jobId))
		client, err := f.addressCodec.BytesToString(clientAcc)
		require.NoError(t, err)
		f.bank.setBalance(clientAcc, sdk.NewCoins())
		require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
			JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
		}))
		require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), jobId)))
		require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
		return f, p
	}

	t.Run("4 of 15 (27 percent) -> deferred, no slash", func(t *testing.T) {
		f, _ := setup(t, "jMin", 4)
		m, _ := f.keeper.Miner.Get(f.ctx, "mP")
		require.Equal(t, aeStake, m.Stake,
			"4 jurors out of 15 is 27 % at the default: that minority must not hard-slash an honest miner")
		j, _ := f.keeper.Job.Get(f.ctx, "jMin")
		require.Empty(t, j.SlashRecords)
		require.NotContains(t, j.State, "clawed", "below the bar the audit is deferred, not decided")
	})

	t.Run("10 of 15 (two thirds) -> hard slash", func(t *testing.T) {
		f, p := setup(t, "jTwoThirds", 10)
		m, _ := f.keeper.Miner.Get(f.ctx, "mP")
		require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, m.Stake,
			"at the two-thirds bar the slash must fire, otherwise the guard is dead")
		j, _ := f.keeper.Job.Get(f.ctx, "jTwoThirds")
		require.Contains(t, j.State, "clawed")
		require.Len(t, j.SlashRecords, 1, "the slash is recorded, therefore restitutable")
	})
}

// (A) Only summoned members weigh, on the permissionless path.
//
// The anchored-jury tests prove the barrier inside the verdict tally and inside the re-commit tally. Neither
// measures what the public claim states, which is about the outcome rather than a tally: a
// non-summoned miner must not weigh on the result. So it is checked at the far end: same votes, same
// job, a single difference (the anchored list), and two opposite outcomes.
func TestREADMEClaimAOnlySummonedJurorsWeigh(t *testing.T) {
	setup := func(t *testing.T, jobId, seed string, summon bool) (*fixture, error) {
		f, srv, disp := adr033Setup(t, seed)
		require.NoError(t, gvSetMiner(f, f.ctx, "mPrim", types.Miner{MinerId: "mPrim", Stake: adr033Big}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__mPrim", types.Commit{ResultCommit: "1,0,0"}))
		ids := []string{}
		for i := 0; i < 12; i++ {
			id := fmt.Sprintf("v%02d", i)
			ids = append(ids, id)
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1_000_000}))
			require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
		}
		if summon {
			redoAnchor(f, t, jobId, ids...)
		} else {
			// A committee is anchored, but made of twelve other identities: the voters are real,
			// registered and unanimous, and not summoned.
			redoAnchor(f, t, jobId, "x00", "x01", "x02", "x03", "x04", "x05", "x06", "x07", "x08", "x09", "x10", "x11")
		}
		require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
			JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "mPrim", Fee: 100000,
			Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
		}))
		require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
		_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: jobId})
		return f, err
	}

	// (i) Summoned: the twelve votes count, so the diverging primary is slashed. The guard can fire.
	fYes, errYes := setup(t, "jSummoned", "disputer_readme_a001", true)
	require.NoError(t, errYes, "twelve summoned and unanimous jurors must be able to decide")
	mYes, _ := fYes.keeper.Miner.Get(fYes.ctx, "mPrim")
	require.Less(t, mYes.Stake, adr033Big, "otherwise this test measures nothing: the guard would be a disguised refusal")

	// (ii) Not summoned: same votes, same unanimity, no influence, refusal, stake intact.
	fNo, errNo := setup(t, "jNotSummoned", "disputer_readme_a002", false)
	require.Error(t, errNo, "non-summoned miners must not produce a decision")
	mNo, _ := fNo.keeper.Miner.Get(fNo.ctx, "mPrim")
	require.Equal(t, adr033Big, mNo.Stake,
		"\"only its summoned members can vote\": a non-summoned miner costs an honest one nothing")
}

// The tie case found while checking the claims.
//
// The fallback path of `AdjudicateDispute`, taken when the verdicts do not reach their floor, must
// decide cheating strictly, on `agree*2 < totalRedoStake`, and leave an exact tie undecided. Written
// `<=`, the rule becomes "the primary is slashed as soon as agreement does not exceed half", and a
// 50/50 split costs an honest miner a hard slash of `SlashLeakBps`.
//
// The `<=` form is a defect rather than a setting, for three reasons:
//
//   - the rule is stated as disagreement with the majority of fresh stake, and a tie is not a
//     majority, so code and documentation would state different rules;
//   - its twin `auditVerdictTally` decides strictly on both sides and leaves a tie undecided, and two
//     tables of the same shape must not follow two different rules;
//   - the exact split is reachable: the redo quorum is satisfied by respondents of equal stake
//     (`min_stake`), so five against five over ten seats produces it. Holding half the fresh stake,
//     not a majority, would then cost an honest miner 80 % of its bond, on the permissionless path,
//     with no governance involved.
//
// The test is three-sided: majority disagreement slashes (the guard fires), a tie refuses and no funds
// move, majority agreement does not slash (the guard stays silent). The tie is the discriminating
// case; the other two keep the function from being neutralised to satisfy it.
func TestReadjudicationTieNeitherSlashesNorVindicates(t *testing.T) {
	// Ten anchored seats give a quorum of ⌈2/3 x 10⌉ = 7; ten respondents of equal stake clear it.
	const equalStake = uint64(1_000_000)

	play := func(t *testing.T, jobId, seed string, nAgree int) (*fixture, error) {
		f, srv, disp := adr033Setup(t, seed)
		// This setup cannot keep the respondents off the committee by outbidding them: the assignment
		// ceiling removes that premise. Four large stakes do not take the three seats just because
		// `adr033Big` dwarfs `equalStake`; since `capAssignmentWeights` saturates the draw weight at a
		// multiple of `min_stake`, outbidding buys nothing and the respondents can win seats.
		// The premise is asserted below, so a setup that loses it fails there.
		//
		// The premise is held through the mechanism that decides who is eligible, the frozen
		// pool: the respondents register after the job's freeze, so they are outside the original draw
		// by construction rather than by wealth. Nothing the test measures changes (the split among redo
		// respondents, and what the guard does with it), and it no longer rests on a concentration the
		// protocol now refuses.
		for _, id := range []string{"mPrim", "gA", "gB", "gC"} {
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
		}
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__mPrim", types.Commit{ResultCommit: "1,0,0"}))

		ids := []string{}
		for i := 0; i < 10; i++ {
			id := fmt.Sprintf("e%02d", i)
			ids = append(ids, id)
			// Registered one block after the freeze: `minerInFrozenPool` asks `registered <= freeze`.
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, gvFreezeH+1))
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: equalStake}))
			vec := "0,1,0" // disagreeing: cosine 0, below the 7000 bps threshold
			if i < nAgree {
				vec = "1,0,0" // agreeing: cosine 1
			}
			require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__redo__"+id, types.Commit{ResultCommit: vec}))
		}
		redoAnchor(f, t, jobId, ids...)
		// No verdict committee is anchored, so `verdictCanSlash` is false and the fallback path is
		// the one under test.

		require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
			JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "mPrim", Fee: 100000,
			Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
		}))
		require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
		f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

		// The premise is checked, not assumed. If the original draw caught a respondent, the split
		// would no longer be 5/5 and the test would pass while measuring something else.
		orig, oErr := f.keeper.AssignedCommitteeForTest(f.ctx, jobId, keeper.CommitteeSize)
		require.NoError(t, oErr)
		for _, id := range ids {
			require.False(t, orig[id], "premise broken: %s sits on the original committee, so the split is no longer exact", id)
		}

		_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: jobId})
		return f, err
	}

	t.Run("majority disagreement 4-6 -> slash (the guard bites)", func(t *testing.T) {
		f, err := play(t, "jMaj", "disputer_readme_e001", 4)
		require.NoError(t, err)
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Less(t, m.Stake, adr033Big, "6 against 4 is a strict majority: the slash must fire")
	})

	t.Run("tie 5-5 -> refusal, no funds move", func(t *testing.T) {
		f, err := play(t, "jTie", "disputer_readme_e002", 5)
		require.Error(t, err, "an exact split of the fresh stake is not a majority disagreement")
		require.Contains(t, err.Error(), "split evenly",
			"the refusal must name the tie, otherwise it reads as an unrelated failure")
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Equal(t, adr033Big, m.Stake,
			"holding half the fresh stake must not cost an honest miner 80 % of its bond")
		j, _ := f.keeper.Job.Get(f.ctx, "jTie")
		require.NotContains(t, j.State, "+resolved", "the audit must stay open for the timeout")
	})

	t.Run("majority agreement 6-4 -> no slash (the guard stays silent)", func(t *testing.T) {
		f, err := play(t, "jAcc", "disputer_readme_e003", 6)
		require.NoError(t, err)
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Equal(t, adr033Big, m.Stake, "6 agreements out of 10 vindicate: no slash")
	})
}
