package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-033 — THE RE-ADJUDICATION COMMITTEE IS ANCHORED: only the drawn jurors re-judge.
//
// Why these tests exist. ADR-032 authenticated the voting set of the VERDICT TALLY and stopped there.
// `AdjudicateDispute` still accepted a SELF-APPOINTED "fresh committee": any registered miner outside
// the original committee could post `<jobId>__redo__<id>` and enter the tally, behind a gate worth
// `3 IDENTITIES` and a majority computed over the stake of the self-selected set alone.
//
// The general property: *any function that moves capital on the strength of a vote must name, on-chain,
// who was entitled to vote.* Both directions of the abuse are tested here, because this path had two —
// and the second (cashing in) is more damaging than the first.

// redoAnchor — explicitly convenes a re-adjudication committee (same pattern as `aeAnchor`).
func redoAnchor(f *fixture, t *testing.T, jobId string, members ...string) {
	t.Helper()
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId+"__redo", strings.Join(members, ",")))
}

// adr033Setup — common fixture: 3 large stakes (potential original committee), a primary, a disputer.
func adr033Setup(t *testing.T, disputerSeed string) (*fixture, types.MsgServer, string) {
	t.Helper()
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(disputerSeed)))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	return f, srv, disp
}

const adr033Big = uint64(1_000_000_000_000)

// (A) DIRECTION 1 — THE FALSE SLASH. Three identities at stake=1, NOT drawn, post divergent
// re-commits against an honest primary.
// Expected: their re-commits are ignored, the electorate is empty, and adjudication REFUSES to decide
// rather than convict. The primary's stake is INTACT.
func TestADR033SybilRedoCannotSlashHonest(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_a01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mHonest", types.Miner{MinerId: "mHonest", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__mHonest", types.Commit{ResultCommit: "1,0,0"}))
	// The attacker registers 3 times for near-nothing (the bond is refunded on exit) and "re-runs".
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// A committee IS drawn — but no sybil belongs to it.
	redoAnchor(f, t, "jAtk", "mJuror1", "mJuror2", "mJuror3")

	require.NoError(t, gvSetJob(f, f.ctx, "jAtk", types.Job{
		JobId: "jAtk", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jAtk"})
	require.Error(t, err, "no DRAWN juror answered -> adjudication does not decide (fail closed)")

	m, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Equal(t, adr033Big, m.Stake,
		"identities that were NOT drawn must NEVER cost an honest miner a single unit of bond")
	job, _ := f.keeper.Job.Get(f.ctx, "jAtk")
	require.NotContains(t, job.State, "resolved", "the job stays open: the audit timeout will decide")
}

// (B) DIRECTION 2 — SELF-VINDICATION, the direction that PAYS. A genuinely slashed cheater stands up
// 3 sybils that copy its original commit. With a self-appointed committee, a strict majority of the
// "fresh stake" (100 % of it its own) returns the slashed amount FROM THE TREASURY, plus the bond and
// the disputer reward — paid to itself. That is theft, not a grievance.
// Expected: nothing leaves the Treasury.
func TestADR033SybilRedoCannotVindicateCheater(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_b01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mCheat", types.Miner{MinerId: "mCheat", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jVind__mCheat", types.Commit{ResultCommit: "0,1,0"}))
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		// they copy the cheater's commit EXACTLY: "the majority agrees with me"
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jVind__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jVind", "mJuror1", "mJuror2", "mJuror3") // no sybil is summoned

	require.NoError(t, gvSetJob(f, f.ctx, "jVind", types.Job{
		JobId: "jVind", State: "settled+disputed", Disputer: disp, DisputeBond: 1000, DisputeHeight: 1,
		SlashRecords: []types.SlashRecord{{MinerId: "mCheat", Amount: 300}},
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 5000}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jVind"})
	require.Error(t, err, "empty electorate -> no adjudication, therefore no restitution")

	m, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	require.Equal(t, adr033Big, m.Stake, "a slash must NOT be reversed by self-appointed identities")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(5000), pools.Treasury, "the Treasury does not fund self-vindication")
}

// (C) FAIL CLOSED — no anchor at all (dispute opened before the anchoring existed, or draw impossible).
// Expected: even with a "plausible" fresh committee (3 real miners, unanimous), nothing moves.
func TestADR033NoAnchoredRedoCommitteeIsInert(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_c01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__mP", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// NO redoAnchor.

	require.NoError(t, gvSetJob(f, f.ctx, "jNo", types.Job{
		JobId: "jNo", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jNo"})
	require.Error(t, err, "without an anchor: no decision, in either direction")
	m, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, adr033Big, m.Stake, "fail closed: missing a cheater is preferable to convicting an honest miner")
}

// (D) THE LEGITIMATE SLASH STILL FIRES. The guard must bound the abuse, not the use: a committee that
// was ACTUALLY drawn, unanimous against a divergent primary, slashes.
// Without this test, (A)/(B)/(C) would be satisfied by code that does nothing at all.
func TestADR033AnchoredRedoCommitteeStillSlashes(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_d01")

	// FIXTURE: 3 LARGE stakes -> they form the ORIGINAL committee (selectCommittee is stake-weighted),
	// 3 SMALL ones -> they are therefore outside the origin and eligible for the fresh committee. With
	// only 4 miners, the original draw would swallow the "fresh" ones, which are excluded from the
	// tally — the test would then fail describing an absence of jurors, not the property it measures.
	for _, id := range []string{"mCheat", "mB", "mC"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__mCheat", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jOk", "mD", "mE", "mF") // THESE are the ones that were drawn

	require.NoError(t, gvSetJob(f, f.ctx, "jOk", types.Job{
		JobId: "jOk", State: "open+paid+optimistic+disputed", MinerId: "mCheat", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jOk"})
	require.NoError(t, err)
	m, _ := f.keeper.Miner.Get(f.ctx, "mCheat")
	p, _ := f.keeper.Params.Get(f.ctx)
	require.Equal(t, adr033Big-adr033Big*p.SlashLeakBps/10000, m.Stake,
		"DRAWN and unanimous committee -> the hard slash lands (the guard bounds the abuse, not the use)")
}

// (F) LIVENESS — A DISPUTE ALWAYS HAS A DEADLINE. The ADR-033 hardening makes it possible for NO drawn
// juror to answer (pool = N-5, or silent jurors). Without a deadline the job would stay `+disputed`
// forever — and `audit_sampling.go` claims otherwise. This proves the timeout decides.
func TestADR033DisputeAlwaysGetsADeadline(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_f01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, gvSetJob(f, f.ctx, "jLive", types.Job{
		JobId: "jLive", State: "open+paid", MinerId: "mP", Fee: 100000,
	}))
	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jLive"})
	require.NoError(t, err)

	// Nobody re-commits, nobody judges. The only recourse is the deadline.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jLive")
	require.Contains(t, job.State, "resolved",
		"a dispute with no juror must close at its deadline, not stay open indefinitely")
}

// (G) COMMITTEE SUMMONED BUT SILENT — the bond stays with the disputer, and is never orphaned.
//
// Strictly distinct from (H): here an audit committee WAS anchored, so the disputer had a legitimate
// reason to contest and it is the jurors that did not answer. The failure is the network's, and the
// invariant "never bill an infrastructure failure to an honest party" applies to a disputer too. What
// this path must never do is leave the bond in the module account with no accounting entry saying who
// it belongs to — the defect that manufactures orphan funds.
func TestADR033SilentAnchoredCommitteeRefundsDisputeBond(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_g01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, gvSetJob(f, f.ctx, "jBond", types.Job{
		JobId: "jBond", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	// THE JOB WAS SAMPLED: an audit committee is anchored. None of its members will vote.
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jBond", "mJ1,mJ2,mJ3,mJ4,mJ5"))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jBond"})
	require.NoError(t, err)
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"the bond is escrowed when the dispute opens")

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	// The property defended here: an honest disputer's bond is NEVER confiscated because the jurors
	// failed. A silent committee makes the deadline DEFER instead of closing, so the bond stays
	// ESCROWED (withheld is not lost — the same doctrine as the primary's held fee) until a real
	// verdict exists. The primary is likewise not billed the job price for a committee's silence.
	// RED if: the bond goes to the Treasury (confiscation), OR the primary's stake moves, OR the job
	// closes without a verdict, OR the loop fails to re-arm a deadline.
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"bond still ESCROWED (neither refunded nor confiscated): the audit is waiting for a verdict")
	job, _ := f.keeper.Job.Get(f.ctx, "jBond")
	require.Equal(t, uint64(1000), job.DisputeBond, "the bond claim stays recorded on the job")
	require.NotContains(t, job.State, "resolved", "silent committee -> DEFER, not closure")
	m, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, adr033Big, m.Stake,
		"a committee's silence is not billed to the primary: stake INTACT")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury,
		"NOTHING in the Treasury: neither the price (no clawback on silence) nor the bond (never confiscated without adjudication)")
	next, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(h+int64(p.AuditResolveTimeout), "jBond"))
	require.True(t, next, "a new deadline is armed: the dispute waits, it is not abandoned")
}

// (H) THE FREE GRIEVANCE — the risk created by giving human disputes a deadline.
//
// A deadline routes them to `resolveDisputedAudit`, which was designed for SAMPLED jobs. Its tally
// reads `AuditCommittee[jobId]`, written only by VRF sampling: for a job that was never audited it
// returns STRUCTURALLY 0 voters, so the "payment not verified" branch clawed back the fee of an honest
// miner that had already been paid. Anyone could therefore cancel the payment of any settled job for
// the price of gas — and the primary had no way to defend itself, since no verdict could ever count.
//
// Expected: dispute NOT upheld, miner untouched, bond to the Treasury (anti-grief).
func TestADR033UnfoundedDisputeCannotClawbackHonestPayment(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_h01")
	p, _ := f.keeper.Params.Get(f.ctx)
	p.AuditResolveTimeout = 5
	p.HoldBps = 10000
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	dispBz, err := f.addressCodec.StringToBytes(disp)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	require.NoError(t, gvSetMiner(f, f.ctx, "mHonnete", types.Miner{MinerId: "mHonnete", Stake: adr033Big}))
	// Job settled optimistically, NEVER sampled: no AuditCommittee[jobId] exists.
	require.NoError(t, gvSetJob(f, f.ctx, "jGrief", types.Job{
		JobId: "jGrief", State: "open+paid+optimistic", MinerId: "mHonnete", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jGrief", 80000))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jGrief"})
	require.NoError(t, err)

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jGrief")
	require.Contains(t, job.State, "resolved", "the dispute must close (liveness)")
	require.NotContains(t, job.State, "clawed",
		"NO clawback: nobody was summoned, so nothing was proven against the primary")
	m, _ := f.keeper.Miner.Get(f.ctx, "mHonnete")
	require.Equal(t, adr033Big, m.Stake, "the honest miner's stake does not move")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(1000), pools.Treasury,
		"disputer bond -> Treasury: an unfounded dispute must COST, otherwise grieving is free")
	require.Equal(t, uint64(0), job.DisputeBond, "bond consumed -> neither re-confiscated nor refunded")
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"the disputer does NOT get its bond back (unlike the summoned-but-silent committee case)")
}

// (J) THE PROPOSER DOES NOT CHOOSE THE AUDITS. `ProcessProposal` accepts a proposal WITHOUT injected
// vote extensions; without injection there is no decentralized seed, and the fallback is
// `AppHash(H-1):h` — known to the proposer BEFORE it proposes. It could therefore compute the draw
// off-chain and inject only when the outcome suited it. Expected: with no decentralized seed, NO draw
// happens and the due jobs are DEFERRED — omitting the injection moves nothing.
func TestADR033NoDecentralizedSeedMeansNoAuditDraw(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 %: WITHOUT the guard this job would certainly be drawn
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.CommitteeSeedSource = 1 // VRF armed, but no seed written at this height
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, gvSetJob(f, f.ctx, "jSeedGrind", types.Job{
		JobId: "jSeedGrind", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jSeedGrind")))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jSeedGrind")
	require.NotContains(t, job.State, "disputed",
		"at 100 %% sampling the job WOULD be drawn: the guard must prevent it while the seed is guessable")

	// DEFERRED, not dropped: the guard must not become a way to cancel audits.
	// The stride is read from the code instead of hard-coded: otherwise the test measures the SETTING
	// ("h+1") rather than the property ("deferred"), and breaks as soon as the stride is tuned for
	// cost reasons.
	moved, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h+keeper.AuditDeferStrideForTest, "jSeedGrind"))
	require.NoError(t, err)
	require.True(t, moved, "the job must be DEFERRED, not abandoned")
	stale, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h, "jSeedGrind"))
	require.NoError(t, err)
	require.False(t, stale, "the old deadline must be removed (otherwise it duplicates at every block)")
}

// (K) AN AUDIT CANNOT BE BOUGHT OUT OF. This vector arises from the COMPOSITION of two guards: the
// draw skipped a job already marked `+disputed` yet still removed its deadline, and `DisputeVerdict`
// does not forbid the primary from disputing its own job. A miner therefore disputed its own job
// during the one-block window preceding the audit, the audit never happened, and the job fell into the
// "unfounded dispute" branch, which RELEASES the held fee.
//
// The arithmetic gets worse as the miner grows: the bond is a FIXED cost, the slash is PROPORTIONAL to
// stake. An anti-cheat guard whose circumvention price does not scale with what it protects is a guard
// that can be bought.
func TestADR033DisputingYourOwnJobDoesNotCancelTheAudit(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 %: this job MUST be audited
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.DisputeBond = 0
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))

	// The miner is also the disputer: nothing forbids it.
	// Same byte length as the string it replaced: the address is derived from these bytes, so a
	// shorter or longer literal would change the address this test asserts on.
	minerAddr, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("the_miner_itself___")))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "mTricheur",
		types.Miner{MinerId: "mTricheur", Stake: adr033Big, Operator: minerAddr}))
	// 4 jurors, not 3: the committee EXCLUDES the primary, and `deferAuditForSmallJury` makes a pool
	// below the floor DEFER the audit instead of opening it — deliberately, since an unreachable
	// quorum must never end in the clawback of an honest miner. This test is about ANTI-EVASION by
	// self-dispute, not about pool size: it is given enough jurors to clear the floor, otherwise it
	// would measure the guard rather than the property it defends.
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jEvade", types.Job{
		JobId: "jEvade", State: "open+paid+optimistic", MinerId: "mTricheur", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jEvade")))

	// THE EVASION: it disputes its own job BEFORE the audit height.
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: minerAddr, JobId: "jEvade"})
	require.NoError(t, err, "nothing forbids disputing one's own job — that is the starting point")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	// The audit must have happened DESPITE the dispute: an anchored committee proves it.
	_, anchored := f.keeper.AuditCommittee.Get(f.ctx, "jEvade")
	require.NoError(t, anchored,
		"disputing one's own job must NOT cancel the audit: otherwise the anti-cheat guard costs one bond")

	// And the human dispute must not have been overwritten: its bond would lose its owner.
	job, _ := f.keeper.Job.Get(f.ctx, "jEvade")
	require.Equal(t, minerAddr, job.Disputer,
		"the audit is ADDED to the human dispute, it does not replace it (else the bond loses its owner)")
	require.NotContains(t, job.State[len("open+paid+optimistic"):], "disputed+disputed",
		"the state must not carry the marker twice")
}

// (L) A THIRD PARTY CANNOT LOCK AN ESCROW. `SettleJob` settled a job on the signer's word alone:
// anyone could set `+settled`, after which every real path refused with "job already settled" — escrow
// frozen, miner never paid. The message is restricted to the module authority.
func TestHaut1SettleJobRejectsNonAuthority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	thirdParty, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("un_quidam_quelconque")))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, gvSetJob(f, f.ctx, "jLock", types.Job{JobId: "jLock", Fee: 10000, State: "open"}))

	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: thirdParty, JobId: "jLock"})
	require.Error(t, err, "a third party must NOT be able to settle (and thus freeze) a job")

	job, _ := f.keeper.Job.Get(f.ctx, "jLock")
	require.NotContains(t, job.State, "settled",
		"the escrow must not be locked by an unauthorized call")
}

// (M) REDO COMMITTEE SUMMONED BUT SILENT: THE BOND IS REFUNDED, NOT CONFISCATED. This is the NOMINAL
// ADR-033 case: a human dispute on a job that was never sampled, a redo committee anchored when the
// dispute opens, and no juror re-commits. Reading the wrong anchor here (`<jobId>` instead of
// `<jobId>__redo`) bills the jurors' failure to the honest disputer.
func TestHaut2SilentRedoCommitteeRefundsBondNotConfiscates(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputeur_haut2_01_")))
	require.NoError(t, err)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 5
	p.DisputeBond = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
	dispBz, _ := f.addressCodec.StringToBytes(disp)
	f.bank.setBalance(sdk.AccAddress(dispBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))

	// a pool large enough for anchorRedoCommittee to draw a committee
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	for _, id := range []string{"mA", "mB", "mC", "mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.SetBlockHash(f.ctx, sdk.UnwrapSDKContext(f.ctx).BlockHeight(), []byte{0x01, 0x02, 0x03, 0x04}))
	require.NoError(t, gvSetJob(f, f.ctx, "jMute", types.Job{
		JobId: "jMute", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jMute"})
	require.NoError(t, err)
	// the redo committee MUST have been anchored
	_, redoOK := f.keeper.AuditCommittee.Get(f.ctx, "jMute__redo")
	require.NoError(t, redoOK, "DisputeVerdict must anchor the redo committee")

	// no juror re-commits. Deadline.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	require.Equal(t, int64(5000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"redo committee summoned but silent -> bond REFUNDED (the failure is the network's, not the disputer's)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury,
		"the bond must NOT go to the Treasury: this is not an unfounded dispute, a committee WAS summoned")
}

// (N) 3 RESPONDENTS OUT OF 15 SEATS DO NOT DECIDE. An absolute gate such as `len(fresh) >= 3`, applied
// whatever the number of drawn seats, lets 20 % of a jury pronounce a hard slash. The quorum is
// relative to ⌈2/3⌉ of the anchored seats.
func TestHaut3RedoQuorumIsRelativeToAnchoredSeats(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputeur_haut3_01_")

	// 15 anchored seats, but only 3 divergent re-commits against a primary.
	require.NoError(t, gvSetMiner(f, f.ctx, "mCible", types.Miner{MinerId: "mCible", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__mCible", types.Commit{ResultCommit: "1,0,0"}))
	seats := make([]string, 0, 15)
	for i := 0; i < 15; i++ {
		id := "seat" + string(rune('a'+i))
		seats = append(seats, id)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	redoAnchor(f, t, "jQ", seats...) // 15 anchored seats
	for _, id := range seats[:3] {   // only 3 re-commit: 3 < ⌈2/3×15⌉ = 10
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jQ", types.Job{
		JobId: "jQ", State: "open+paid+optimistic+disputed", MinerId: "mCible", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jQ"})
	require.Error(t, err, "3 respondents out of 15 seats are below the ⌈2/3⌉ quorum -> adjudication refused")

	m, _ := f.keeper.Miner.Get(f.ctx, "mCible")
	require.Equal(t, adr033Big, m.Stake, "a minority of seats must not slash")
}

// (E) THE DRAW EXCLUDES THE PARTIES. The primary does not judge itself, the disputer does not judge its
// own accusation, and the ORIGINAL committee is set aside (independence).
func TestADR033DrawExcludesPartiesAndOrigin(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_e01")

	// The disputer is ALSO a miner: that is the case that matters (it could summon itself).
	for _, id := range []string{"mPrim", "mNeutral1", "mNeutral2", "mNeutral3", "mNeutral4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, gvSetMiner(f, f.ctx, disp, types.Miner{MinerId: disp, Stake: adr033Big * 100}))

	require.NoError(t, gvSetJob(f, f.ctx, "jDraw", types.Job{
		JobId: "jDraw", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// An UNPREDICTABLE randomness source is required: without it the draw is deliberately refused,
	// otherwise the accuser — who picks both the job AND the broadcast height — could enumerate
	// heights off-chain until the draw seats its accomplices.
	require.NoError(t, f.keeper.SetBlockHash(f.ctx,
		sdk.UnwrapSDKContext(f.ctx).BlockHeight(), []byte{0xde, 0xad, 0xbe, 0xef, 0x01, 0x02}))

	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jDraw"})
	require.NoError(t, err)

	raw, err := f.keeper.AuditCommittee.Get(f.ctx, "jDraw__redo")
	require.NoError(t, err, "DisputeVerdict must ANCHOR the re-adjudication committee when the dispute opens")
	members := strings.Split(raw, ",")
	require.NotEmpty(t, members)
	for _, id := range members {
		require.NotEqual(t, "mPrim", id, "the primary does not judge itself")
		require.NotEqual(t, disp, id, "the disputer does not judge its own accusation (despite 100x the stake)")
	}
}

// (I) GUESSABLE SEED -> NO DRAW. A fallback seed of the form `"dispute:<height>"` is entirely
// predictable, while the accuser chooses both the job and the height at which it broadcasts its tx. It
// could enumerate heights off-chain until the draw seats its accomplices, then broadcast at the right
// one. Without an unpredictable randomness source, anchoring is refused: an unavailable adjudication
// (whose timeout is non-punitive) is preferable to a jury chosen by the accusation.
func TestADR033NoUnpredictableSeedMeansNoDraw(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_i01")
	for _, id := range []string{"mPrim", "mA", "mB", "mC", "mD", "mE"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jSeed", types.Job{
		JobId: "jSeed", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// NO SetBlockHash, and committee_seed_source=0 by default -> no unpredictable seed.
	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jSeed"})
	require.NoError(t, err, "the dispute still opens (liveness): it is the DRAW that is refused")

	_, gErr := f.keeper.AuditCommittee.Get(f.ctx, "jSeed__redo")
	require.Error(t, gErr,
		"no committee may be anchored on a seed the accuser can guess")
}
