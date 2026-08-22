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

// ═══ THE PUBLIC README CLAIMS, MEASURED OR REFUTED ═══════════════════════════════════════════════
//
// The public README states three things to a future reader — an auditor, an exchange, a miner about
// to put capital at risk:
//
//	(A) "The audit committee is drawn and anchored on-chain ... and only its summoned members can vote"
//	(B) "A hard slash requires at least two thirds of the anchored seats to return \"invalid\",
//	     so a minority verdict cannot slash an honest miner"
//	(C) "With no anchored committee, no hard slash fires at all (fail-closed)"
//
// Why this file exists. Error strings read out of the adjudication code are not a measurement, and
// "probably true" has no place in a public README when the decision to publish depends on its being
// true. The case that has to be followed to the end is the one (C) names, `seats == 0`: ⌈2/3 x 0⌉ = 0,
// and a null denominator makes a relative bar trivially cleared. A public claim whose demonstration
// stops at the edge of the case it names is not demonstrated.
//
// What the measurement holds to. (A) and (C) hold, and the `seats == 0` case with them. (B) is the
// fragile one: `Params.Validate()` ACCEPTS `audit_min_quorum = 0` together with
// `verification_mode = 1`, so under DEFAULT parameters nothing but the relative bar stands between a
// minority and a hard slash. An ABSOLUTE fallback such as `voters >= 4` — a count sized for a 5-seat
// committee — is 27 % of the 15 seats ADR-032 draws, precisely the "minority verdict" the sentence
// declares impossible, and the default timeout path carries no other guard.
//
// A public claim is not made true by documenting its falsehood, and no launch script can make it
// true either: keeping the README exact by setting the parameter on one operator's own deployments
// rests a published security property on an optional setting. The property belongs to the code —
// `auditRelativeBar` is the SINGLE SOURCE of both outcomes, relative in BOTH branches — so the
// sentence holds for the chain, not for one launch kit.
//
// A neighbouring failure mode in the same decision carries its own guard: see
// `TestReadjudicationEgaliteNeSlashePasNiNeVindique`.

// ─── (C) and the `seats == 0` case: a relative bar over a NULL denominator ───────────────────────
//
// `auditSlashDecision` computes `need = max(audit_min_quorum, ⌈2/3 x seats⌉)`. At `seats == 0` the
// second term is 0, so ONLY the governed floor still holds anything back. This test follows the
// computation to the end instead of trusting the fail-closed check upstream, because a guard that is
// correct only thanks to ITS CALLER breaks the day it acquires a second caller.
func TestREADMEClaimCZeroSiegeNeSlashePas(t *testing.T) {
	for _, quorum := range []uint64{0, 4, 10} {
		t.Run(fmt.Sprintf("audit_min_quorum=%d", quorum), func(t *testing.T) {
			p := types.DefaultParams()
			p.AuditMinQuorum = quorum

			// EMPTY electorate — what `auditVerdictTally` returns when nothing is anchored.
			require.False(t, keeper.AuditSlashDecisionForTest(p, false, 0, 0, 0),
				"no seats and no voters: ⌈2/3 x 0⌉ = 0 must NEVER be enough for a hard slash")
			// ...and even if a future caller passed `cheated=true` by mistake, the floor holds.
			require.False(t, keeper.AuditSlashDecisionForTest(p, true, 0, 0, 0),
				"the participation floor must hold on its own when the denominator is null")
			require.False(t, keeper.AuditVindicateDecisionForTest(p, true, 0, 0, 0),
				"symmetry: zero seats must not vindicate either, or the grief has a dual")

			// The property that makes all of this safe: the effective floor is NEVER zero.
			require.Greater(t, keeper.EffectiveSlashFloorForTest(p), 0,
				"a null participation floor would turn \"nobody voted\" into a decision")
		})
	}
}

// (C) end to end: NOTHING anchored (neither verdicts nor redo commits), yet real registered miners
// re-commit in disagreement. Adjudication must REFUSE, and the primary's stake must be intact to the
// last unit.
//
// This differs from (A) below: there a committee IS drawn and the voters are not part of it. Here
// nothing at all is anchored — the "job predating the mechanism, or a path outside optimistic mode"
// case that (C) describes.
func TestREADMEClaimCNoAnchorNoHardSlash(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_readme_c001")

	require.NoError(t, gvSetMiner(f, f.ctx, "mPrim", types.Miner{MinerId: "mPrim", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNoAnchor__mPrim", types.Commit{ResultCommit: "1,0,0"}))
	// Ten REGISTERED miners with real stake, re-committing in disagreement: everything a slash needs,
	// except the authorisation to vote.
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("r%02d", i)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1_000_000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNoAnchor__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// No aeAnchor, no redoAnchor. That absence is the whole subject of the test.

	require.NoError(t, gvSetJob(f, f.ctx, "jNoAnchor", types.Job{
		JobId: "jNoAnchor", State: "open+paid+optimistic+disputed", MinerId: "mPrim", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jNoAnchor"})
	require.Error(t, err, "with no anchored committee, adjudication must decide NOTHING (fail-closed)")

	m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
	require.Equal(t, adr033Big, m.Stake, "no hard slash may fire without an anchored committee")
	j, _ := f.keeper.Job.Get(f.ctx, "jNoAnchor")
	require.NotContains(t, j.State, "+resolved", "a refusal must not consume the dispute")
}

// ─── (B) the two-thirds bar, and WHAT IT DEPENDS ON ──────────────────────────────────────────────
//
// Two-sided, against the REAL committee size (read from the code, not restated here): a verdict at
// ⌈2/3⌉ - 1 seats does not slash, at ⌈2/3⌉ seats it does. That is the README sentence, measured.
func TestREADMEClaimBTwoThirdsOfAnchoredSeats(t *testing.T) {
	p := types.DefaultParams()
	p.AuditMinQuorum = 4 // the value the public launch script requires

	seats := keeper.AuditCommitteeDrawSizeForTest
	besoin := (2*seats + 2) / 3
	require.Equal(t, 10, besoin, "⌈2/3 x 15⌉ = 10 — if the draw size changes, this number must change with it")

	require.False(t, keeper.AuditSlashDecisionForTest(p, true, seats, besoin-1, seats),
		"9 seats out of 15 (60 %) is a MINORITY in the sense of the claim: it must not slash")
	require.True(t, keeper.AuditSlashDecisionForTest(p, true, seats, besoin, seats),
		"10 seats out of 15 is two thirds: the slash must be able to bite, or the guard is dead")
	// The second lock, independent of the first: seats alone are NOT enough without the stake majority.
	require.False(t, keeper.AuditSlashDecisionForTest(p, false, seats, seats, seats),
		"15 \"invalid\" seats without a STAKE majority must not slash: two locks, not one")
	// Symmetry: vindication clears the SAME bar, so the two outcomes cannot drift apart.
	require.False(t, keeper.AuditVindicateDecisionForTest(p, true, seats, besoin-1, seats),
		"vindicating at 9/15 would reopen the asymmetry between slash and vindication")
	require.True(t, keeper.AuditVindicateDecisionForTest(p, true, seats, besoin, seats))
}

// What sourcing claim (B) demands, and what this test measures.
//
// The relative bar ⌈2/3 x anchored seats⌉ has to exist in BOTH branches, not only under
// `audit_min_quorum > 0`. At the DEFAULT (`audit_min_quorum = 0`, which `Params.Validate()` accepts
// together with `verification_mode = 1`), an ABSOLUTE fallback of `voters >= 4` — a count sized for a
// 5-seat committee — is 27 % of the 15 seats ADR-032 draws, and the audit timeout path calls
// `auditSlashDecision` with no other guard: four jurors would hard-slash an honest miner carrying
// eleven "valid" votes, on the DEFAULT path.
//
// A public claim is not made true by documenting its falsehood; the property lives in the code.
// `auditRelativeBar` is the SINGLE SOURCE of both outcomes, hence relative in BOTH branches. This test
// measures INDEPENDENCE: same 4 voters, same 15-seat committee, SAME refusal, armed or at the default.
// The README sentence does not depend on an optional setting, which is the only form in which a
// security property can be published.
func TestREADMEClaimBNeDependPlusDuParametreDeLancement(t *testing.T) {
	seats := keeper.AuditCommitteeDrawSizeForTest

	defaut := types.DefaultParams()
	require.Zero(t, defaut.AuditMinQuorum, "premise: the default IS the fallback floor")
	require.False(t, keeper.AuditSlashDecisionForTest(defaut, true, 4, 4, seats),
		"4 voters over 15 seats is 27 %: at the DEFAULT too, that minority must not slash")
	require.False(t, keeper.AuditVindicateDecisionForTest(defaut, true, 4, 4, seats),
		"dual of the grief: 27 % must not vindicate a cheater either")

	arme := types.DefaultParams()
	arme.AuditMinQuorum = 4 // what the launch gate imposes
	require.Equal(t,
		keeper.AuditSlashDecisionForTest(defaut, true, 10, 10, seats),
		keeper.AuditSlashDecisionForTest(arme, true, 10, 10, seats),
		"at two thirds, armed and default must DECIDE ALIKE: the bar does not depend on the setting")
	require.True(t, keeper.AuditSlashDecisionForTest(defaut, true, 10, 10, seats),
		"...and it must bite at 10/15, otherwise the guard is dead")

	// The 5-seat committee is the case where absolute and relative coincide (⌈2/3 x 5⌉ = 4): the
	// relative bar must decide identically there, since both routes yield the same number.
	require.True(t, keeper.AuditSlashDecisionForTest(defaut, true, 4, 4, keeper.AuditLegacyFloorBasis),
		"at 5 seats, 4 \"invalid\" votes remain a near-unanimity: behaviour unchanged")

	// And parameter validation refuses to arm optimistic mode below 4 when the quorum IS governed.
	sansQuorum := types.DefaultParams()
	sansQuorum.VerificationMode = 1
	sansQuorum.AuditMinQuorum = 2
	require.Error(t, sansQuorum.Validate(),
		"verification_mode=1 with a governed quorum below 4 must be REFUSED by the chain itself")
}

// (B) END TO END, ON THE DEFAULT PATH — the audit TIMEOUT, which calls `auditSlashDecision` with no
// other guard. The function-level test above proves the rule; this one proves it is WIRED where it
// decides 80 % of a miner's bond, under the parameters a chain carries WITHOUT being armed.
//
// Two-sided: 4 "invalid" over 15 seats (27 %) defers and nothing moves; 10 over 15 (two thirds) and
// the hard slash fires. Without the second case, a plain "never slash" dressed up as a guard would
// pass this test.
func TestREADMEClaimBAuTimeoutParDefaut(t *testing.T) {
	monter := func(t *testing.T, jobId string, nInvalides int) (*fixture, types.Params) {
		f := initFixture(t)
		p := types.DefaultParams() // deliberately NOT armed: that is the whole point
		p.VerificationMode = 1
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		aeReg(f, t, "mP", aeStake)

		sieges := []string{}
		for i := 0; i < keeper.AuditCommitteeDrawSizeForTest; i++ { // 15 ANCHORED seats
			id := fmt.Sprintf("j%02d", i)
			sieges = append(sieges, id)
			aeReg(f, t, id, 1_000_000)
		}
		aeAnchor(f, t, jobId, sieges...)
		for i := 0; i < nInvalides; i++ { // only these answer, all with "invalid"
			aeVerdict(f, t, jobId, sieges[i], "0")
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

	t.Run("4 of 15 (27 percent) -> DEFERRED, no slash", func(t *testing.T) {
		f, _ := monter(t, "jMin", 4)
		m, _ := f.keeper.Miner.Get(f.ctx, "mP")
		require.Equal(t, aeStake, m.Stake,
			"4 jurors out of 15 is 27 % at the DEFAULT: that minority must NOT hard-slash an honest miner")
		j, _ := f.keeper.Job.Get(f.ctx, "jMin")
		require.Empty(t, j.SlashRecords)
		require.NotContains(t, j.State, "clawed", "below the bar the audit is DEFERRED, not decided")
	})

	t.Run("10 of 15 (two thirds) -> HARD SLASH", func(t *testing.T) {
		f, p := monter(t, "jDeuxTiers", 10)
		m, _ := f.keeper.Miner.Get(f.ctx, "mP")
		require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, m.Stake,
			"at the two-thirds bar the slash MUST fire, otherwise the guard is dead")
		j, _ := f.keeper.Job.Get(f.ctx, "jDeuxTiers")
		require.Contains(t, j.State, "clawed")
		require.Len(t, j.SlashRecords, 1, "the slash is recorded, therefore restitutable")
	})
}

// ─── (A) only summoned members weigh, on the PERMISSIONLESS path ─────────────────────────────────
//
// ADR-032 proves the barrier inside the verdict tally, ADR-033 inside the re-commit tally. Neither
// measures what the public claim actually states: the claim is not about a tally, it is about the
// OUTCOME — a non-summoned miner must not weigh on the result. So it is checked at the far end: same
// votes, same job, one single difference (the anchored list), and two opposite outcomes.
func TestREADMEClaimASeulsLesConvoquesPesent(t *testing.T) {
	monter := func(t *testing.T, jobId, seed string, convoquer bool) (*fixture, error) {
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
		if convoquer {
			redoAnchor(f, t, jobId, ids...)
		} else {
			// A committee IS anchored, but made of twelve OTHER identities: the voters are real,
			// registered and unanimous — and not summoned.
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

	// (i) summoned: the twelve votes COUNT, so the diverging primary is slashed. The guard can bite.
	fOui, errOui := monter(t, "jConv", "disputer_readme_a001", true)
	require.NoError(t, errOui, "twelve SUMMONED and unanimous jurors must be able to decide")
	mOui, _ := fOui.keeper.Miner.Get(fOui.ctx, "mPrim")
	require.Less(t, mOui.Stake, adr033Big, "otherwise this test measures nothing: the guard would be a disguised refusal")

	// (ii) NOT summoned: same votes, same unanimity -> no influence, refusal, stake intact.
	fNon, errNon := monter(t, "jNonConv", "disputer_readme_a002", false)
	require.Error(t, errNon, "non-summoned miners must not produce a decision")
	mNon, _ := fNon.keeper.Miner.Get(fNon.ctx, "mPrim")
	require.Equal(t, adr033Big, mNon.Stake,
		"\"only its summoned members can vote\": a non-summoned miner costs an honest one nothing")
}

// ─── The defect found while sourcing the claims ──────────────────────────────────────────────────
//
// The fallback path of `AdjudicateDispute` — taken when the VERDICTS do not reach their floor — must
// decide cheating STRICTLY, on `agree*2 < totalRedoStake`, and leave an exact tie undecided. Written
// `<=`, the rule becomes "the primary is slashed as soon as agreement does not exceed HALF", and a
// 50/50 split costs an honest miner a hard slash of `SlashLeakBps`.
//
// Three reasons the `<=` form is a defect rather than a setting:
//
//   - the rule is stated as disagreement with the MAJORITY of fresh stake, and a tie is not a
//     majority: code and documentation would not state the same rule;
//   - its twin `auditVerdictTally` decides STRICTLY on both sides and leaves a tie undecided — two
//     tables of the same shape must not follow two different rules;
//   - the exact split is REACHABLE: the redo quorum is satisfied by respondents of EQUAL stake
//     (`min_stake`), so five against five over ten seats produces it. Holding HALF the fresh stake and
//     not a majority would then cost an honest miner 80 % of its bond, on the PERMISSIONLESS path,
//     with no governance involved.
//
// The test is three-sided: majority disagreement slashes (the guard bites), a tie refuses and no
// funds move, majority agreement does not slash (the guard stays silent). The tie is the
// discriminating case; the two others forbid satisfying it by neutralising the function.
func TestReadjudicationEgaliteNeSlashePasNiNeVindique(t *testing.T) {
	// Ten anchored seats -> quorum ⌈2/3 x 10⌉ = 7; ten respondents of EQUAL stake clear it.
	const stakeEgal = uint64(1_000_000)

	jouer := func(t *testing.T, jobId, seed string, dAccord int) (*fixture, error) {
		f, srv, disp := adr033Setup(t, seed)
		// ⛔ THIS SETUP KEPT THE RESPONDENTS OFF THE COMMITTEE BY OUTBIDDING THEM, AND THAT PREMISE
		// DIED WITH THE ASSIGNMENT CEILING. Four "large stakes" were expected to take the three seats
		// because `adr033Big` dwarfed `stakeEgal`; since `capAssignmentWeights` saturates the draw
		// weight at a multiple of `min_stake`, outbidding buys nothing and the respondents started
		// winning seats. The bench SAID SO instead of measuring the wrong thing — the premise is
		// asserted below, and that assertion is what caught this.
		//
		// The premise is restored through the mechanism that exists to decide WHO IS ELIGIBLE, the
		// frozen pool: the respondents register AFTER the job's freeze, so they are outside the
		// original draw by construction rather than by wealth. Nothing the test measures changes —
		// the split among redo respondents, and what the guard does with it — and it no longer rests
		// on a concentration the protocol now refuses.
		for _, id := range []string{"mPrim", "gA", "gB", "gC"} {
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
		}
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__mPrim", types.Commit{ResultCommit: "1,0,0"}))

		ids := []string{}
		for i := 0; i < 10; i++ {
			id := fmt.Sprintf("e%02d", i)
			ids = append(ids, id)
			// Registered one block AFTER the freeze: `minerInFrozenPool` asks `registered <= freeze`.
			require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, gvFreezeH+1))
			require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: stakeEgal}))
			vec := "0,1,0" // disagreeing: cosine 0, below the 7000 bps threshold
			if i < dAccord {
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

		// The premise is VERIFIED, not assumed. If the original draw caught a respondent, the split
		// would no longer be 5/5 and the test would "pass" while measuring something else.
		orig, oErr := f.keeper.AssignedCommitteeForTest(f.ctx, jobId, keeper.CommitteeSize)
		require.NoError(t, oErr)
		for _, id := range ids {
			require.False(t, orig[id], "premise broken: %s sits on the original committee, so the split is no longer exact", id)
		}

		_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: jobId})
		return f, err
	}

	t.Run("majority disagreement 4-6 -> SLASH (the guard bites)", func(t *testing.T) {
		f, err := jouer(t, "jMaj", "disputer_readme_e001", 4)
		require.NoError(t, err)
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Less(t, m.Stake, adr033Big, "6 against 4 is a strict majority: the slash must fire")
	})

	t.Run("TIE 5-5 -> REFUSAL, no funds move", func(t *testing.T) {
		f, err := jouer(t, "jEgal", "disputer_readme_e002", 5)
		require.Error(t, err, "an EXACT split of the fresh stake is not a majority disagreement")
		require.Contains(t, err.Error(), "split evenly",
			"the refusal must NAME the tie, otherwise it reads as an unrelated failure")
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Equal(t, adr033Big, m.Stake,
			"holding HALF the fresh stake must not cost an honest miner 80 % of its bond")
		j, _ := f.keeper.Job.Get(f.ctx, "jEgal")
		require.NotContains(t, j.State, "+resolved", "the audit must stay OPEN for the timeout")
	})

	t.Run("majority agreement 6-4 -> no slash (the guard stays silent)", func(t *testing.T) {
		f, err := jouer(t, "jAcc", "disputer_readme_e003", 6)
		require.NoError(t, err)
		m, _ := f.keeper.Miner.Get(f.ctx, "mPrim")
		require.Equal(t, adr033Big, m.Stake, "6 agreements out of 10 vindicate: no slash")
	})
}
