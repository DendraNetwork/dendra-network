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

// The re-adjudication committee is anchored, and only the drawn jurors judge again.
//
// The anchored audit jury authenticated the voting set of the verdict tally and stopped there. `AdjudicateDispute`
// still accepted a self-appointed "fresh committee": any registered miner outside the original
// committee could post `<jobId>__redo__<id>` and enter the tally, behind a gate of three identities and
// a majority computed over the stake of the self-selected set alone.
//
// The general property: any function that moves capital on the strength of a vote must name, on-chain,
// who was entitled to vote. Both directions of the abuse are tested here, because this path had two,
// and the second (cashing in) does more damage than the first.

// redoAnchor convenes a re-adjudication committee explicitly (same pattern as `aeAnchor`).
func redoAnchor(f *fixture, t *testing.T, jobId string, members ...string) {
	t.Helper()
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId+"__redo", strings.Join(members, ",")))
}

// adr033Setup is the common fixture: 3 large stakes (potential original committee), a primary, a disputer.
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

// (A) Direction 1, the false slash: three identities at stake=1, not drawn, post divergent
// re-commits against an honest primary.
// Expected: their re-commits are ignored, the electorate is empty, and adjudication refuses to decide
// rather than convict. The primary's stake is intact.
func TestADR033SybilRedoCannotSlashHonest(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_a01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mHonest", types.Miner{MinerId: "mHonest", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__mHonest", types.Commit{ResultCommit: "1,0,0"}))
	// The attacker registers 3 times for almost nothing (the bond is refunded on exit) and "re-runs".
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jAtk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// A committee is drawn, but no sybil belongs to it.
	redoAnchor(f, t, "jAtk", "mJuror1", "mJuror2", "mJuror3")

	require.NoError(t, gvSetJob(f, f.ctx, "jAtk", types.Job{
		JobId: "jAtk", State: "open+paid+optimistic+disputed", MinerId: "mHonest", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jAtk"})
	require.Error(t, err, "no drawn juror answered -> adjudication does not decide (fail closed)")

	m, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Equal(t, adr033Big, m.Stake,
		"identities that were not drawn must never cost an honest miner a single unit of bond")
	job, _ := f.keeper.Job.Get(f.ctx, "jAtk")
	require.NotContains(t, job.State, "resolved", "the job stays open: the audit timeout will decide")
}

// (B) Direction 2, self-vindication, the direction that pays. A cheater that was really slashed sets up
// 3 sybils that copy its original commit. With a self-appointed committee, a strict majority of the
// "fresh stake" (100 % of it its own) returns the slashed amount from the Treasury, plus the bond and
// the disputer reward, paid to itself. That is theft, not a grievance.
// Expected: nothing leaves the Treasury.
func TestADR033SybilRedoCannotVindicateCheater(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_b01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mCheat", types.Miner{MinerId: "mCheat", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jVind__mCheat", types.Commit{ResultCommit: "0,1,0"}))
	for _, id := range []string{"sybil1", "sybil2", "sybil3"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		// they copy the cheater's commit exactly: "the majority agrees with me"
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
	require.Equal(t, adr033Big, m.Stake, "a slash must not be reversed by self-appointed identities")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(5000), pools.Treasury, "the Treasury does not fund self-vindication")
}

// (C) Fail closed: no anchor at all (dispute opened before the anchoring existed, or draw impossible).
// Expected: even with a plausible fresh committee (3 real miners, unanimous), nothing moves.
func TestADR033NoAnchoredRedoCommitteeIsInert(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_c01")

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__mP", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jNo__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	// No redoAnchor.

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

// (D) The legitimate slash still fires. The guard must bound the abuse, not the use: a committee that
// was actually drawn, unanimous against a divergent primary, slashes.
// Without this test, (A)/(B)/(C) would be satisfied by code that does nothing at all.
func TestADR033AnchoredRedoCommitteeStillSlashes(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_d01")

	// Fixture: 3 large stakes form the original committee (selectCommittee is stake-weighted), and 3
	// small ones are outside the origin and eligible for the fresh committee. With only 4 miners, the
	// original draw would swallow the fresh ones, which are excluded from the tally, and the test would
	// fail on an absence of jurors instead of the property it measures.
	for _, id := range []string{"mCheat", "mB", "mC"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__mCheat", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOk__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jOk", "mD", "mE", "mF") // these are the ones that were drawn

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
		"drawn and unanimous committee -> the hard slash lands (the guard bounds the abuse, not the use)")
}

// (F) Liveness: a dispute always has a deadline. The anchored re-adjudication jury makes it possible for no drawn
// juror to answer (pool = N-5, or silent jurors). Without a deadline the job would stay `+disputed`
// forever, contrary to what `audit_sampling.go` guarantees. This proves the timeout decides.
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

// (G) Committee summoned but silent: the bond stays with the disputer and is never orphaned.
//
// Distinct from (H): here an audit committee was anchored, so the disputer had a legitimate reason to
// contest, and the jurors are the ones that did not answer. The failure is the network's, and "never
// bill an infrastructure failure to an honest party" applies to a disputer too. This path must never
// leave the bond in the module account with no accounting entry saying who it belongs to.
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
	// The job was sampled: an audit committee is anchored, and none of its members will vote.
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, "jBond", "mJ1,mJ2,mJ3,mJ4,mJ5"))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jBond"})
	require.NoError(t, err)
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"the bond is escrowed when the dispute opens")

	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	// The property defended here: an honest disputer's bond is never confiscated because the jurors
	// failed. A silent committee makes the deadline defer instead of closing, so the bond stays in
	// escrow (withheld is not lost, as for the primary's held fee) until a real verdict exists. The
	// primary is likewise not billed the job price for a committee's silence.
	// Red if the bond goes to the Treasury (confiscation), the primary's stake moves, the job closes
	// without a verdict, or the loop fails to arm a new deadline.
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"bond still escrowed (neither refunded nor confiscated): the audit is waiting for a verdict")
	job, _ := f.keeper.Job.Get(f.ctx, "jBond")
	require.Equal(t, uint64(1000), job.DisputeBond, "the bond claim stays recorded on the job")
	require.NotContains(t, job.State, "resolved", "silent committee -> defer, not closure")
	m, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, adr033Big, m.Stake,
		"a committee's silence is not billed to the primary: stake intact")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury,
		"nothing in the Treasury: neither the price (no clawback on silence) nor the bond (never confiscated without adjudication)")
	next, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(h+int64(p.AuditResolveTimeout), "jBond"))
	require.True(t, next, "a new deadline is armed: the dispute waits, it is not abandoned")
}

// (H) The free grievance, a risk created by giving human disputes a deadline.
//
// A deadline routes them to `resolveDisputedAudit`, which was designed for sampled jobs. Its tally reads
// `AuditCommittee[jobId]`, written only by VRF sampling: for a job that was never audited it always
// returns 0 voters, so the "payment not verified" branch clawed back the fee of an honest miner that had
// already been paid. Anyone could cancel the payment of any settled job for the price of gas, and the
// primary could not defend itself, since no verdict could ever count.
//
// Expected: dispute not upheld, miner untouched, bond to the Treasury (anti-grief).
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

	require.NoError(t, gvSetMiner(f, f.ctx, "mHonest", types.Miner{MinerId: "mHonest", Stake: adr033Big}))
	// Job settled optimistically, never sampled: no AuditCommittee[jobId] exists.
	require.NoError(t, gvSetJob(f, f.ctx, "jGrief", types.Job{
		JobId: "jGrief", State: "open+paid+optimistic", MinerId: "mHonest", Fee: 100000,
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
		"no clawback: nobody was summoned, so nothing was proven against the primary")
	m, _ := f.keeper.Miner.Get(f.ctx, "mHonest")
	require.Equal(t, adr033Big, m.Stake, "the honest miner's stake does not move")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(1000), pools.Treasury,
		"disputer bond -> Treasury: an unfounded dispute must cost, otherwise grieving is free")
	require.Equal(t, uint64(0), job.DisputeBond, "bond consumed -> neither re-confiscated nor refunded")
	require.Equal(t, int64(4000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"the disputer does not get its bond back (unlike the summoned-but-silent committee case)")
}

// (J-bis) The control, which gives (J) its meaning: the same pool, the same height, with
// `committee_seed_source` at 0 so the guard's own condition is false. The draw then happens and the
// job is marked disputed. One parameter, two opposite outcomes, so the seed is the variable under test
// rather than the jury floor.
func TestADR033WithoutTheSeedGuardTheDrawHappens(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.CommitteeSeedSource = 0 // the guard tests `== 1`: at 0 it does not apply
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, gvSetMiner(f, f.ctx, "nP", types.Miner{MinerId: "nP", Stake: adr033Big}))
	for _, j := range []string{"nJ1", "nJ2", "nJ3", "nJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jSeedOk", types.Job{
		JobId: "jSeedOk", State: "open+paid+optimistic", MinerId: "nP", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jSeedOk")))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, err := f.keeper.Job.Get(f.ctx, "jSeedOk")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed",
		"with the guard inapplicable and a jury available, the audit is drawn -- so (J)'s silence is the guard")
}

// (J) The proposer does not choose the audits. `ProcessProposal` accepts a proposal without injected
// vote extensions; without injection there is no decentralized seed, and the fallback is
// `AppHash(H-1):h`, known to the proposer before it proposes. It could compute the draw off-chain and
// inject only when the outcome suited it. Expected: with no decentralized seed, no draw happens and the
// due jobs are deferred, so omitting the injection moves nothing.
func TestADR033NoDecentralizedSeedMeansNoAuditDraw(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 %: without the guard this job would certainly be drawn
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.CommitteeSeedSource = 1 // VRF armed, but no seed written at this height
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: adr033Big}))
	// A jury that could actually sit, which is what makes this case meaningful. The fixture used to
	// register the primary alone. The draw excludes the miner under audit (audit_committee.go), so the
	// pool held zero candidates, the small-jury floor deferred the audit, and the assertions below held
	// whatever the seed guard did: replacing the guard's condition with `if false` left this case green.
	// It was testing the jury floor, not the seed.
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jSeedGrind", types.Job{
		JobId: "jSeedGrind", State: "open+paid+optimistic", MinerId: "mP", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jSeedGrind")))
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	job, _ := f.keeper.Job.Get(f.ctx, "jSeedGrind")
	require.NotContains(t, job.State, "disputed",
		"at 100 %% sampling the job would be drawn: the guard must prevent it while the seed is guessable")

	// Deferred, not dropped: the guard must not become a way to cancel audits.
	// The stride is read from the code instead of hard-coded: otherwise the test measures the setting
	// ("h+1") rather than the property ("deferred"), and breaks as soon as the stride is tuned for cost
	// reasons.
	moved, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h+keeper.AuditDeferStrideForTest, "jSeedGrind"))
	require.NoError(t, err)
	require.True(t, moved, "the job must be deferred, not abandoned")
	stale, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(h, "jSeedGrind"))
	require.NoError(t, err)
	require.False(t, stale, "the old deadline must be removed (otherwise it duplicates at every block)")
}

// (K) An audit cannot be bought out of. The vector came from the composition of two guards: the draw
// skipped a job already marked `+disputed` yet still removed its deadline, and `DisputeVerdict` does
// not forbid the primary from disputing its own job. A miner therefore disputed its own job during the
// one-block window before the audit, the audit never happened, and the job fell into the "unfounded
// dispute" branch, which releases the held fee.
//
// It gets worse as the miner grows: the bond is a fixed cost and the slash is proportional to stake.
// An anti-cheat guard whose circumvention price does not scale with what it protects can be bought.
func TestADR033DisputingYourOwnJobDoesNotCancelTheAudit(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // 100 %: this job must be audited
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
	require.NoError(t, gvSetMiner(f, f.ctx, "mCheater",
		types.Miner{MinerId: "mCheater", Stake: adr033Big, Operator: minerAddr}))
	// 4 jurors, not 3: the committee excludes the primary, and `deferAuditForSmallJury` makes a pool
	// below the floor defer the audit instead of opening it, on purpose, since an unreachable quorum
	// must never end in the clawback of an honest miner. This test is about anti-evasion by
	// self-dispute, not pool size, so it gets enough jurors to clear the floor; otherwise it would
	// measure that guard rather than the property it defends.
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{MinerId: j, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jEvade", types.Job{
		JobId: "jEvade", State: "open+paid+optimistic", MinerId: "mCheater", Fee: 100000,
	}))
	const h = int64(50)
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h, "jEvade")))

	// The evasion: it disputes its own job before the audit height.
	_, err = srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: minerAddr, JobId: "jEvade"})
	require.NoError(t, err, "nothing forbids disputing one's own job — that is the starting point")

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	// The audit must have happened despite the dispute: an anchored committee proves it.
	_, anchored := f.keeper.AuditCommittee.Get(f.ctx, "jEvade")
	require.NoError(t, anchored,
		"disputing one's own job must not cancel the audit: otherwise the anti-cheat guard costs one bond")

	// The human dispute must not have been overwritten, or its bond would lose its owner.
	job, _ := f.keeper.Job.Get(f.ctx, "jEvade")
	require.Equal(t, minerAddr, job.Disputer,
		"the audit is added to the human dispute, it does not replace it (else the bond loses its owner)")
	require.NotContains(t, job.State[len("open+paid+optimistic"):], "disputed+disputed",
		"the state must not carry the marker twice")
}

// (L) A third party cannot lock an escrow. `SettleJob` settled a job on the signer's word alone:
// anyone could set `+settled`, after which every real path refused with "job already settled", the
// escrow frozen and the miner never paid. The message is restricted to the module authority.
func TestHigh1SettleJobRejectsNonAuthority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	thirdParty, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("some_random_outsider")))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	require.NoError(t, gvSetJob(f, f.ctx, "jLock", types.Job{JobId: "jLock", Fee: 10000, State: "open"}))

	_, err = srv.SettleJob(f.ctx, &types.MsgSettleJob{Creator: thirdParty, JobId: "jLock"})
	require.Error(t, err, "a third party must not be able to settle (and thus freeze) a job")

	job, _ := f.keeper.Job.Get(f.ctx, "jLock")
	require.NotContains(t, job.State, "settled",
		"the escrow must not be locked by an unauthorized call")
}

// (M) Redo committee summoned but silent: the bond is refunded, not confiscated. This is the nominal
// re-adjudication case: a human dispute on a job that was never sampled, a redo committee anchored when the
// dispute opens, and no juror re-commits. Reading the wrong anchor here (`<jobId>` instead of
// `<jobId>__redo`) bills the jurors' failure to the honest disputer.
func TestHigh2SilentRedoCommitteeRefundsBondNotConfiscates(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_high2_01__")))
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
	// the redo committee must have been anchored
	_, redoOK := f.keeper.AuditCommittee.Get(f.ctx, "jMute__redo")
	require.NoError(t, redoOK, "DisputeVerdict must anchor the redo committee")

	// no juror re-commits. Deadline.
	h := sdk.UnwrapSDKContext(f.ctx).BlockHeight() + 5
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)))

	require.Equal(t, int64(5000), f.bank.balOf(sdk.AccAddress(dispBz)).AmountOf("udndr").Int64(),
		"redo committee summoned but silent -> bond refunded (the failure is the network's, not the disputer's)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Treasury,
		"the bond must not go to the Treasury: this is not an unfounded dispute, a committee was summoned")
}

// (N) 3 respondents out of 15 seats do not decide. An absolute gate such as `len(fresh) >= 3`, applied
// whatever the number of drawn seats, lets 20 % of a jury pronounce a hard slash. The quorum is
// relative to ⌈2/3⌉ of the anchored seats.
func TestHigh3RedoQuorumIsRelativeToAnchoredSeats(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_high3_01__")

	// 15 anchored seats, but only 3 divergent re-commits against a primary.
	require.NoError(t, gvSetMiner(f, f.ctx, "mTarget", types.Miner{MinerId: "mTarget", Stake: adr033Big}))
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__mTarget", types.Commit{ResultCommit: "1,0,0"}))
	seats := make([]string, 0, 15)
	for i := 0; i < 15; i++ {
		id := "seat" + string(rune('a'+i))
		seats = append(seats, id)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	redoAnchor(f, t, "jQ", seats...) // 15 anchored seats
	for _, id := range seats[:3] {   // only 3 re-commit: 3 < ⌈2/3 x 15⌉ = 10
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jQ__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jQ", types.Job{
		JobId: "jQ", State: "open+paid+optimistic+disputed", MinerId: "mTarget", Fee: 100000,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jQ"})
	require.Error(t, err, "3 respondents out of 15 seats are below the ⌈2/3⌉ quorum -> adjudication refused")

	m, _ := f.keeper.Miner.Get(f.ctx, "mTarget")
	require.Equal(t, adr033Big, m.Stake, "a minority of seats must not slash")
}

// (E) The draw excludes the parties: the primary does not judge itself, the disputer does not judge its
// own accusation, and the original committee is set aside (independence).
func TestADR033DrawExcludesPartiesAndOrigin(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_e01")

	// The disputer is also a miner: that is the case that matters (it could summon itself).
	for _, id := range []string{"mPrim", "mNeutral1", "mNeutral2", "mNeutral3", "mNeutral4"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, gvSetMiner(f, f.ctx, disp, types.Miner{MinerId: disp, Stake: adr033Big * 100}))

	require.NoError(t, gvSetJob(f, f.ctx, "jDraw", types.Job{
		JobId: "jDraw", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// An unpredictable randomness source is required: without it the draw is refused on purpose,
	// otherwise the accuser, who picks both the job and the broadcast height, could enumerate heights
	// off-chain until the draw seats its accomplices.
	require.NoError(t, f.keeper.SetBlockHash(f.ctx,
		sdk.UnwrapSDKContext(f.ctx).BlockHeight(), []byte{0xde, 0xad, 0xbe, 0xef, 0x01, 0x02}))

	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jDraw"})
	require.NoError(t, err)

	raw, err := f.keeper.AuditCommittee.Get(f.ctx, "jDraw__redo")
	require.NoError(t, err, "DisputeVerdict must anchor the re-adjudication committee when the dispute opens")
	members := strings.Split(raw, ",")
	require.NotEmpty(t, members)
	for _, id := range members {
		require.NotEqual(t, "mPrim", id, "the primary does not judge itself")
		require.NotEqual(t, disp, id, "the disputer does not judge its own accusation (despite 100x the stake)")
	}
}

// (I) Guessable seed, no draw. A fallback seed of the form `"dispute:<height>"` is predictable, while
// the accuser chooses both the job and the height at which it broadcasts its tx. It could enumerate
// heights off-chain until the draw seats its accomplices, then broadcast at the right one. Without an
// unpredictable randomness source, anchoring is refused: an unavailable adjudication (whose timeout is
// non-punitive) is preferable to a jury chosen by the accuser.
func TestADR033NoUnpredictableSeedMeansNoDraw(t *testing.T) {
	f, srv, disp := adr033Setup(t, "disputer_adr033_i01")
	for _, id := range []string{"mPrim", "mA", "mB", "mC", "mD", "mE"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: adr033Big}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, "jSeed", types.Job{
		JobId: "jSeed", State: "open+paid+optimistic", MinerId: "mPrim", Fee: 100000,
	}))
	// No SetBlockHash, and committee_seed_source=0 by default, so no unpredictable seed.
	_, err := srv.DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jSeed"})
	require.NoError(t, err, "the dispute still opens (liveness): it is the draw that is refused")

	_, gErr := f.keeper.AuditCommittee.Get(f.ctx, "jSeed__redo")
	require.Error(t, gErr,
		"no committee may be anchored on a seed the accuser can guess")
}
