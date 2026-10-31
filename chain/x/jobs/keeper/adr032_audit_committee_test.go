package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The audit committee is anchored, and only summoned members vote.
//
// An earlier test performed the attack literally (four jurors at stake=1, never summoned, slashing a
// primary by 80 %) and required it to pass, certifying the flaw instead of measuring security. The
// property is tested here in both directions: the attack fails, and the legitimate slash still fires.

// (A) The attack: 4 identities at min_stake, not summoned, voting "0" against an honest miner.
// Expected: their verdicts are ignored, so no hard slash and the primary's stake is intact.
func TestADR032SybilNonMembersCannotSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mHonest", aeStake)
	// The attacker registers four times for almost nothing (the bond is refunded on exit) and votes "0".
	for _, s := range []string{"sybil1", "sybil2", "sybil3", "sybil4"} {
		aeReg(f, t, s, 1)
		aeVerdict(f, t, "jAtk", s, "0")
	}
	// A committee is anchored, but no sybil belongs to it: they were not drawn.
	aeAnchor(f, t, "jAtk", "mJuror1", "mJuror2", "mJuror3", "mJuror4")

	require.NoError(t, gvSetJob(f, f.ctx, "jAtk", types.Job{
		JobId: "jAtk", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jAtk")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// What is at stake is the authentication of the voting body: sybils that were not summoned do not
	// count, so their "quorum" does not exist. An empty tally defers, so four sybils voting "0" cost the
	// honest miner nothing at all, not even the job price. This test is red if non-summoned voters move
	// the stake by a single udndr, in either direction.
	mH, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Equal(t, aeStake, mH.Stake,
		"non-summoned voters trigger nothing: neither a hard slash nor even the loss of the job price")
	job, _ := f.keeper.Job.Get(f.ctx, "jAtk")
	require.Empty(t, job.SlashRecords, "no sanction recorded: nobody authorised voted")
	require.NotContains(t, job.State, "resolved", "empty tally -> deferred, nothing is closed")
}

// (B) Fail closed: a job with no anchored committee (jobs older than the mechanism, paths outside
// optimistic mode) never gets a hard slash. The asymmetry is deliberate: missing a cheater is bounded
// and stays -EV, whereas slashing an honest miner cannot be taken back.
func TestADR032NoAnchoredCommitteeNeverHardSlashes(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jNoAnchor", j, "0")
	}
	// No aeAnchor: no anchored list exists for this job.

	require.NoError(t, gvSetJob(f, f.ctx, "jNoAnchor", types.Job{
		JobId: "jNoAnchor", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jNoAnchor")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Fully fail closed: with no anchored committee the tally is empty, so the audit is deferred.
	// Charging the absence of an anchor to the primary would turn an operational gap into a sanction.
	// This test is red if the missing anchor costs or earns anything at all.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "with no anchored committee: no movement, neither hard slash nor price")
	job, _ := f.keeper.Job.Get(f.ctx, "jNoAnchor")
	require.Empty(t, job.SlashRecords, "nothing recorded: nothing was judged")
	require.NotContains(t, job.State, "resolved", "deferred: the audit waits for a real electorate")
}

// (C) Non-regression with a mixed committee: the summoned members vote "0" at quorum while sybils vote
// "1" to dilute them. Expected: sybils are ignored in both directions, so the legitimate slash fires.
// The restriction does more than protect the honest miner: it authenticates the voting set.
func TestADR032NonMembersCannotDiluteLegitimateSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mCheat", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jMix", j, "0")
	}
	// Ten large-stake sybils vote "1": without the restriction they would flip the stake majority.
	for _, s := range []string{"x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10"} {
		aeReg(f, t, s, aeStake)
		aeVerdict(f, t, "jMix", s, "1")
	}
	aeAnchor(f, t, "jMix", "mJ1", "mJ2", "mJ3", "mJ4")

	require.NoError(t, gvSetJob(f, f.ctx, "jMix", types.Job{
		JobId: "jMix", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jMix")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mC, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mC.Stake,
		"the legitimate slash must fire: non-summoned voters dilute nothing")
}

// (D) The veto is a near-unanimity again, the calibration defect of the 15-seat draw, fixed.
//
// The veto meant "4 out of a 5-seat committee", 80 %, a near-unanimity, so that an honest miner judged
// invalid by a minority is never slashed. The drawn committee was raised to 15 seats for liveness
// while keeping the absolute quorum at 4, which is 27 %: four "invalid" verdicts then slashed even with
// eleven jurors voting "valid". The threshold had not moved; its denominator had changed underneath it.
// This test witnesses that case.
func TestADR032MinorityOfAnchoredCommitteeCannotSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	all := []string{}
	for i := 0; i < 15; i++ {
		id := "j" + string(rune('a'+i))
		aeReg(f, t, id, 1_000_000)
		all = append(all, id)
	}
	aeAnchor(f, t, "jMin", all...) // 15 anchored seats
	// Four summoned jurors return "invalid", eleven return "valid": a minority accuses.
	for _, j := range all[:4] {
		aeVerdict(f, t, "jMin", j, "0")
	}
	for _, j := range all[4:] {
		aeVerdict(f, t, "jMin", j, "1")
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jMin", types.Job{
		JobId: "jMin", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jMin")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	hardSlashLeaves := aeStake - aeStake*p.SlashLeakBps/10000
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Greater(t, mP.Stake, hardSlashLeaves,
		"4 accusers out of 15 is 27 %: a minority must never trigger the hard slash (⌈2/3⌉ is required)")
}

// (E) Non-regression: a near-unanimity still slashes. Without this test, the case above could be
// satisfied by a fix that simply freezes the slash, a silent regression.
func TestADR032SupermajorityOfAnchoredCommitteeStillSlashes(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mCheat", aeStake)
	all := []string{}
	for i := 0; i < 15; i++ {
		id := "k" + string(rune('a'+i))
		aeReg(f, t, id, 1_000_000)
		all = append(all, id)
	}
	aeAnchor(f, t, "jSup", all...)
	// 10 out of 15 reaches ⌈2/3⌉, and they hold the majority of voting stake: both locks open.
	for _, j := range all[:10] {
		aeVerdict(f, t, "jSup", j, "0")
	}
	for _, j := range all[10:] {
		aeVerdict(f, t, "jSup", j, "1")
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jSup", types.Job{
		JobId: "jSup", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jSup")))
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mC, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mC.Stake,
		"⌈2/3⌉ of the anchored seats plus a stake majority: the legitimate slash must still fire")
}

// (F) Draw determinism: the same (seed, jobId, eligible set) yields the same committee in the same
// order. Two validators drawing different lists would anchor different state and the chain would
// fork. The draw is replayed through the keeper, and the miners' insertion order must change nothing.
func TestADR032DrawIsDeterministic(t *testing.T) {
	ids := []string{"mA", "mB", "mC", "mD", "mE", "mF", "mG", "mH"}

	f1 := initFixture(t)
	for _, id := range ids {
		aeReg(f1, t, id, 1_000_000)
	}
	// These tests draw against a bare jobId, with no `Job` written. The frozen pool requires
	// an anchor, so it is set to the maximum height, which keeps these tests measuring the determinism
	// of the ordering rather than the freeze.
	require.NoError(t, f1.keeper.JobPoolFreezeHeight.Set(f1.ctx, "job-1", ^uint64(0)))
	got1, err := f1.keeper.DrawAuditCommitteeForTest(f1.ctx, "seed-xyz", "job-1", "mA", "")
	require.NoError(t, err)

	// Same set, inserted in reverse order.
	f2 := initFixture(t)
	for i := len(ids) - 1; i >= 0; i-- {
		aeReg(f2, t, ids[i], 1_000_000)
	}
	require.NoError(t, f2.keeper.JobPoolFreezeHeight.Set(f2.ctx, "job-1", ^uint64(0)))
	got2, err := f2.keeper.DrawAuditCommitteeForTest(f2.ctx, "seed-xyz", "job-1", "mA", "")
	require.NoError(t, err)

	require.Equal(t, got1, got2, "the draw must not depend on the store's iteration order")
	require.NotContains(t, got1, "mA", "the primary never sits on the committee that judges it")

	// A different seed must produce a different draw, otherwise the draw is not one.
	got3, err := f1.keeper.DrawAuditCommitteeForTest(f1.ctx, "seed-OTHER", "job-1", "mA", "")
	require.NoError(t, err)
	require.NotEqual(t, got1, got3, "changing the seed must change the committee")
}

// (G) Stake weighting, the core of the defence: multiplying into identities at min_stake must not buy
// seats. Against a few large stakes, a swarm of dust must stay a minority.
func TestADR032DrawIsStakeWeightedNotIdentityCount(t *testing.T) {
	f := initFixture(t)
	// Three serious miners.
	for _, id := range []string{"big1", "big2", "big3"} {
		aeReg(f, t, id, 1_000_000_000_000)
	}
	// Sixty dust identities: the attack by numbers.
	for i := 0; i < 60; i++ {
		aeReg(f, t, "dust"+string(rune('a'+i%26))+string(rune('0'+i/26)), 1)
	}
	require.NoError(t, f.keeper.JobPoolFreezeHeight.Set(f.ctx, "job-w", ^uint64(0)))
	members, err := f.keeper.DrawAuditCommitteeForTest(f.ctx, "seed-w", "job-w", "none", "")
	require.NoError(t, err)
	require.NotEmpty(t, members)

	bigs := 0
	for _, m := range members {
		if m == "big1" || m == "big2" || m == "big3" {
			bigs++
		}
	}
	require.Equal(t, 3, bigs,
		"all three large stakes must be drawn despite 60 dust identities: a seat is bought with capital, not with headcount")
}
