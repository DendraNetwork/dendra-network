package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-028 — ANTI-EVASION. The optimistic primary is PAID first; at the audit deadline it NEVER keeps
// an unverified payment. At the timeout (EndBlock): cheating at the relative bar -> HARD slash +
// clawback; valid at the SAME bar -> vindicated; otherwise -> DEFERRAL (nothing moves, a new deadline
// is set — neither "paid without verification" nor "billing the outage to the honest party").
// A SILENT primary is caught as a cheating quorum (the judge worker posts "0" when a reveal is missing).
// The SYMMETRIC bar prevents two judges or sybils from slashing an honest primary OR vindicating a
// cheater, and their grievance now costs the honest party nothing either. AuditLegacyFloorBasis=5
// (DECOUPLED from CommitteeSize=3) -> floor = ceil(5/2)+1 = 4.

const aeStake = uint64(1_000_000_000_000)

func aeCtx6(f *fixture) sdk.Context {
	return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(6).WithHeaderInfo(header.Info{Height: 6, AppHash: []byte("ah6")})
}

func aeFundModule(f *fixture) {
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1_000_000_000_000)))
}

func aeReg(f *fixture, t *testing.T, id string, stake uint64) {
	require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: stake}))
}

func aeVerdict(f *fixture, t *testing.T, jobId, judge, v string) {
	require.NoError(t, f.keeper.Commit.Set(f.ctx, jobId+"__verdict__"+judge, types.Commit{ResultCommit: v}))
}

// aeAnchor — ADR-032: ANCHORS the audit committee entitled to vote on `jobId`.
//
// Without that anchor the tally FAILS CLOSED (no hard slash is possible). This is deliberate: before
// ADR-032 any registered miner could vote, and 4 identities at `min_stake` were enough to slash 80 %
// of an HONEST miner's stake. A test that wants to observe a legitimate slash must therefore convene
// its judges explicitly, exactly as the chain does at the draw.
func aeAnchor(f *fixture, t *testing.T, jobId string, members ...string) {
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId, strings.Join(members, ",")))
}

// (1) CHEATING AT THE FLOOR (4 CONVENED judges voting "0") -> 80 % HARD SLASH + clawback. This is the
// fate of a silent or garbage primary (the committee posts "0"). Triggered by the TIMEOUT, with no
// call to AdjudicateDispute. (floor = 4 at N=5)
//
// A form of this test that registers 4 judges at `stake=1` WITHOUT convening them and REQUIRES them to
// slash the primary by 80 % describes, line for line, the sybil attack ADR-032 closes — and it stays
// green on every run: it does not test the security property, it certifies the flaw. What the test
// must guarantee is that a legitimate slash still fires WHEN THE ANCHORED COMMITTEE VOTES; the refusal
// of non-convened voters is covered by the attack test.
func TestADR028CheatQuorumHardSlashAtTimeout(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)                            // 4 judges -> participation floor reached (AuditLegacyFloorBasis=5 -> floor 4)
	aeAnchor(f, t, "jC", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: they are CONVENED -> their verdicts count
	aeVerdict(f, t, "jC", "mJ1", "0")
	aeVerdict(f, t, "jC", "mJ2", "0")
	aeVerdict(f, t, "jC", "mJ3", "0")
	aeVerdict(f, t, "jC", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028_cheat0"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins()) // start from zero to measure the exact refund
	require.NoError(t, gvSetJob(f, f.ctx, "jC", types.Job{
		JobId: "jC", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jC")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jC")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved")
	require.Contains(t, job.State, "clawed")
	require.Len(t, job.SlashRecords, 1, "slash recorded (restitutable)")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "HARD slash of 80 %")
	require.Equal(t, int64(100000), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client refunded the job price")
}

// (2) NO QUORUM (no committee, zero verdicts) -> DEFERRAL, and never a clawback. The silence of a
// committee is NOT a verdict, so it costs the primary NOTHING: this is the path that used to take back
// the payment of honest miners for an outage they did not cause. What would make this test RED: a
// stake that moves (the clawback came back), a resolution without a verdict, or a deadline consumed
// without a retry (the job would become unreachable, a silent freeze).
func TestADR028NoQuorumReportsNeverBillsThePrimary(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	clientAcc := sdk.AccAddress([]byte("client_adr028_noquor"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jN", types.Job{
		JobId: "jN", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jN")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jN")
	require.NotContains(t, job.State, "resolved", "no verdict -> NOTHING is closed (deferral)")
	require.NotContains(t, job.State, "clawed", "the clawback-on-silence path is gone")
	require.Empty(t, job.SlashRecords, "no trace of a sanction: no audit took place")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "stake INTACT: a network outage is never billed to the honest party")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded: nothing was judged, nothing moves")
	// The deferral loop is ALIVE: the old deadline is consumed and a new one is set (otherwise the job
	// freezes silently). The timeout is NOT governed here (default 0), so the fallback deferral stride
	// must apply: the loop must NEVER depend on a parameter to stay alive.
	old, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(int64(6), "jN"))
	require.False(t, old, "today's deadline consumed")
	next, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(int64(6)+keeper.AuditDeferStrideForTest, "jN"))
	require.True(t, next, "new deadline set (fallback stride): the audit WAITS, it does not give up")
}

// (3) VALID QUORUM (4 judges voting "1" AT the vindication floor) -> vindicated, NO slash and no
// clawback, stake intact. Vindication requires the SAME floor as the slash (symmetry): 4 judges at N=5.
func TestADR028ValidQuorumVindicated(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)                            // 4 judges voting "1" -> vindication floor reached (symmetry)
	aeAnchor(f, t, "jV", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: judges CONVENED (without an anchor the tally fails closed)
	aeVerdict(f, t, "jV", "mJ1", "1")
	aeVerdict(f, t, "jV", "mJ2", "1")
	aeVerdict(f, t, "jV", "mJ3", "1")
	aeVerdict(f, t, "jV", "mJ4", "1")
	require.NoError(t, gvSetJob(f, f.ctx, "jV", types.Job{
		JobId: "jV", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jV")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jV")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed", "valid verdict -> no clawback")
	require.Len(t, job.SlashRecords, 0)
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "honest primary: stake intact")
}

// (4) TWO-SYBIL GRIEVANCE, BELOW THE FLOOR: two judges voting "0" (< floor 4) trigger NEITHER a hard
// slash NOR anything else — DEFERRAL, with the stake STRICTLY intact. A "majority of those present" is
// not a quorum, and the grievance no longer costs the honest primary even the job price.
// RED if: the stake moves by a single udndr, or anything closes on two votes.
func TestADR028BelowFloorNoHardSlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jG", "mS1", "mS2") // ADR-032: judges CONVENED (without an anchor the tally fails closed)
	aeVerdict(f, t, "jG", "mS1", "0")  // two sybils vote invalid, but below the participation floor (4)
	aeVerdict(f, t, "jG", "mS2", "0")
	require.NoError(t, gvSetJob(f, f.ctx, "jG", types.Job{
		JobId: "jG", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jG")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "2 judges below the floor -> DEFERRAL: stake INTACT (not even the job price is taken)")
	job, _ := f.keeper.Job.Get(f.ctx, "jG")
	require.NotContains(t, job.State, "resolved", "nothing closes on two votes")
}

// (4b) THE DUAL OF THE GRIEVANCE: two sybils vote "1" to VINDICATE a primary, but below the floor ->
// NO vindication, and — the essential point — a sub-quorum NEVER RELEASES the escrow. This is the one
// place where "no sanction" could turn into a gift to a cheater: if it silently became "payment
// finalized", two sybils would be enough to convert "audited" into "paid without verification", a free
// way to dodge the audit. RED if: the held fee leaves the module, the operator is paid, or the job
// carries `vindicated`.
func TestADR028BelowFloorNoVindication(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000 // full hold (production setting): this is the held fee two sybils would try to release
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_f1clos_dual"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jF", "mS1", "mS2") // ADR-032: judges CONVENED (without an anchor the tally fails closed)
	aeVerdict(f, t, "jF", "mS1", "1")  // two sybils vote VALID to vindicate, but below the participation floor (4)
	aeVerdict(f, t, "jF", "mS2", "1")
	require.NoError(t, gvSetJob(f, f.ctx, "jF", types.Job{
		JobId: "jF", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jF", uint64(80000))) // the held fee being coveted
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jF")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jF")
	require.NotContains(t, job.State, "vindicated", "two sybils below the floor do NOT vindicate")
	require.NotContains(t, job.State, "resolved", "and nothing closes: DEFERRAL")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jF")
	require.NoError(t, hErr, "the held fee is STILL in the module: a sub-quorum NEVER releases the escrow")
	require.Equal(t, uint64(80000), held)
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "the operator was NOT paid: dodging the audit is not free")
}

// (5) Non-optimistic mode (human dispute): behaviour UNCHANGED (innocent by default, no clawback).
func TestADR028NonOptimisticUnchanged(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	aeReg(f, t, "mP", aeStake)
	require.NoError(t, gvSetJob(f, f.ctx, "jH", types.Job{
		JobId: "jH", State: "open+disputed", MinerId: "mP", // no "optimistic" marker -> human dispute
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jH")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jH")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed", "non-optimistic dispute -> innocent by default, unchanged")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "no slash on an unanswered human dispute")
}

// (6) Params.Validate() — cross bounds in optimistic mode (a slash must be able to complete).
func TestADR028ParamsValidateCrossChecks(t *testing.T) {
	base := types.DefaultParams()
	base.VerificationMode = 1
	base.AuditSampleBps = 1000
	base.DisputeWindow = 5
	base.AuditResolveTimeout = 10
	// Optimistic mode also requires a non-zero dispute bond (challenging must not be free) and an
	// unpredictable seed source (here the deferred committee reveal).
	base.DisputeBond = 1000
	base.CommitteeRevealDelay = 1
	require.NoError(t, base.Validate(), "coherent mode-1 configuration")

	b1 := base
	b1.AuditResolveTimeout = 5 // == window
	require.Error(t, b1.Validate(), "timeout <= dispute_window must be rejected")

	b2 := base
	b2.AuditSampleBps = 0
	require.Error(t, b2.Validate(), "audit_sample_bps=0 rejected in mode 1")

	b3 := base
	b3.DisputeWindow = 0
	require.Error(t, b3.Validate(), "dispute_window=0 rejected in mode 1")

	off := types.DefaultParams() // mode 0: no cross constraint (dormant)
	off.VerificationMode = 0
	off.AuditResolveTimeout = 0
	off.DisputeWindow = 0
	require.NoError(t, off.Validate(), "mode 0 unchanged")
}

// (7) TOTAL CONSERVATION ON DEFERRAL — at full hold, a sub-quorum does not move A SINGLE udndr: held
// fee, deferred burn, protocol cut in Pools, Demand, bond and client balance are all bit-for-bit
// identical after the deadline. The COMPOSITION of the refund is defended on the SLASH path (test 7b
// below), the only one that still refunds. RED if a single one of the six items moves.
func TestADR028FeeHoldFullyRetainedOnSubQuorum(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	cut := fee * p.ProtocolFeeBps / 10000            // 15000
	burn := fee * p.FeeBurnBps / 10000               // 5000
	minerNet := fee - cut - burn                     // 80000
	validators := cut * p.ValidatorRewardBps / 10000 // 7500
	team := cut * p.TeamFeeBps / 10000               // 3000
	treasury := cut - validators - team              // 4500

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Demand: treasury + team}))
	aeReg(f, t, "mS1", 1)
	aeReg(f, t, "mS2", 1)
	aeAnchor(f, t, "jHold", "mS1", "mS2")
	aeVerdict(f, t, "jHold", "mS1", "0")
	aeVerdict(f, t, "jHold", "mS2", "0")
	clientAcc := sdk.AccAddress([]byte("client_adr028_feehld"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jHold", types.Job{
		JobId: "jHold", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jHold", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jHold", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jHold")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "bond INTACT")
	require.Equal(t, treasury+team, mP.Demand, "Demand INTACT: nothing was judged, nothing is paid back")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded (the audit is still waiting)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, validators+team+treasury, pools.Validators+pools.Team+pools.Treasury, "the protocol cut is still in Pools")
	heldF, e1 := f.keeper.HeldFee.Get(f.ctx, "jHold")
	require.NoError(t, e1, "held fee STILL held")
	require.Equal(t, minerNet, heldF)
	heldB, e2 := f.keeper.HeldBurn.Get(f.ctx, "jHold")
	require.NoError(t, e2, "deferred burn STILL held (neither burned nor returned)")
	require.Equal(t, burn, heldB)
}

// (7b) COMPOSITION OF THE REFUND, DEFENDED ON THE ONLY PATH THAT STILL REFUNDS — cheating at the
// quorum (4 convened judges voting "0"). The client is refunded from the WHOLE retained fee (held fee
// + the cut paid back from Pools + the deferred burn RETURNED, never burned on an undelivered job),
// the job's Demand is reversed, and the hard slash applies on top. If the composition broke (client
// served from the bond, burn burned anyway, cut left in Pools), this test goes red.
func TestADR028FeeHoldRefundCompositionOnCheat(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	cut := fee * p.ProtocolFeeBps / 10000
	burn := fee * p.FeeBurnBps / 10000
	minerNet := fee - cut - burn
	validators := cut * p.ValidatorRewardBps / 10000
	team := cut * p.TeamFeeBps / 10000
	treasury := cut - validators - team

	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Demand: treasury + team}))
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jCheat", j, "0")
	}
	aeAnchor(f, t, "jCheat", "mJ1", "mJ2", "mJ3", "mJ4") // quorum (4) reached, unanimously "invalid"
	clientAcc := sdk.AccAddress([]byte("client_adr028_compo0"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jCheat", types.Job{
		JobId: "jCheat", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jCheat", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jCheat", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jCheat")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jCheat")
	require.Contains(t, job.State, "clawed+quorum", "cheating at the quorum -> the audit was CONDUCTED")
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(),
		"client refunded the WHOLE fee: held fee + cut paid back + burn RETURNED (never burned on an undelivered job)")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, uint64(0), mP.Demand, "the job's Demand is reversed")
	require.Less(t, mP.Stake, aeStake, "and the hard slash applies on top (cheater)")
	_, e1 := f.keeper.HeldFee.Get(f.ctx, "jCheat")
	_, e2 := f.keeper.HeldBurn.Get(f.ctx, "jCheat")
	require.Error(t, e1, "held fee consumed (paid to the client)")
	require.Error(t, e2, "deferred burn consumed (RETURNED to the client)")
	pools, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, uint64(0), pools.Validators+pools.Team, "the cut is paid back from Pools to the client (treasury may carry the slash residue)")
}

// (8) FEE HOLD — VINDICATED: the held fee is RELEASED to the primary's operator (the optimistic
// payment is confirmed).
func TestADR028FeeHoldReleasedOnVindication(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	opAcc := sdk.AccAddress([]byte("operator_adr028hold0"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)
	aeAnchor(f, t, "jRel", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: judges CONVENED (without an anchor the tally fails closed)
	aeVerdict(f, t, "jRel", "mJ1", "1")
	aeVerdict(f, t, "jRel", "mJ2", "1")
	aeVerdict(f, t, "jRel", "mJ3", "1")
	aeVerdict(f, t, "jRel", "mJ4", "1")
	require.NoError(t, gvSetJob(f, f.ctx, "jRel", types.Job{
		JobId: "jRel", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jRel", uint64(80000))) // held minerNet
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jRel", uint64(5000))) // DEFERRED burn (burned at finality)
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jRel")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, _ := f.keeper.Job.Get(f.ctx, "jRel")
	require.Contains(t, job.State, "resolved")
	require.NotContains(t, job.State, "clawed")
	require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "held minerNet RELEASED to the operator (the 5000 burn is BURNED at finality, NOT paid out)")
	_, hErr := f.keeper.HeldFee.Get(f.ctx, "jRel")
	require.Error(t, hErr, "held fee consumed")
	_, bErr := f.keeper.HeldBurn.Get(f.ctx, "jRel")
	require.Error(t, bErr, "deferred burn burned at finality (HeldBurn removed)")
}

// (9) SUCCESSFUL APPEAL: the primary is silent at the first timeout (no quorum) -> DEFERRED (no
// clawback, the held fee is KEPT) plus a second deadline; a LATE reveal -> the fresh committee votes
// VALID -> vindicated at the appeal deadline (held fee released to the honest, offline primary, burn
// burned). This is the protection of the honest miner that went offline.
func TestADR028AppealLateRevealVindicated(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.AuditSampleBps = 1000
	p.DisputeWindow = 3
	p.AuditResolveTimeout = 10
	p.AppealWindow = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	burn := fee * p.FeeBurnBps / 10000                  // 5000
	minerNet := fee - fee*p.ProtocolFeeBps/10000 - burn // 80000
	opAcc := sdk.AccAddress([]byte("operator_appeal_vind"))
	op, _ := f.addressCodec.BytesToString(opAcc)
	f.bank.setBalance(opAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Operator: op, Stake: aeStake}))
	require.NoError(t, gvSetJob(f, f.ctx, "jAp", types.Job{
		JobId: "jAp", State: "open+paid+optimistic+disputed", MinerId: "mP", Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jAp", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jAp", burn))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jAp")))

	// First timeout (h=6): no verdict -> DEFERRED (job unchanged, held fee kept, nothing paid out)
	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))
	job, _ := f.keeper.Job.Get(f.ctx, "jAp")
	require.NotContains(t, job.State, "resolved", "first timeout -> DEFERRED, NOT resolved")
	require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "nothing paid out at the first timeout")
	_, stillHeld := f.keeper.HeldFee.Get(f.ctx, "jAp")
	require.NoError(t, stillHeld, "held fee STILL there after deferral")

	// Late reveal: the fresh committee votes VALID (4 judges = the floor at AuditLegacyFloorBasis=5)
	aeAnchor(f, t, "jAp", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: judges CONVENED (without an anchor the tally fails closed)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
		aeReg(f, t, j, 1)
		aeVerdict(f, t, "jAp", j, "1")
	}
	// Second deadline (h=11=6+appeal_window): appeal -> re-tally VALID -> vindicated
	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("ah11")})
	require.NoError(t, f.keeper.EndBlock(ctx11))
	job2, _ := f.keeper.Job.Get(f.ctx, "jAp")
	require.Contains(t, job2.State, "resolved", "appeal deadline -> resolved")
	require.NotContains(t, job2.State, "clawed", "a vindicated late reveal -> NO clawback")
	require.Equal(t, int64(minerNet), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "successful appeal -> held fee released to the offline-but-honest primary")
	_, e := f.keeper.HeldBurn.Get(f.ctx, "jAp")
	require.Error(t, e, "burn burned at finality")
}

// (10) EXPIRED APPEAL: the primary stays silent, so at the appeal deadline there is no "final
// clawback" — deferral, exactly as at the first timeout. The appeal must be NEITHER an exit toward
// release (dodging) NOR a guillotine toward clawback (billing the outage): it only gives a late reveal
// more time, and when it expires the audit keeps waiting.
// RED if: the expired appeal closes the job, refunds the client, pays the operator, or leaves the job
// without a new deadline (a silent freeze).
func TestADR028AppealExpiryClawback(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.AuditSampleBps = 1000
	p.DisputeWindow = 3
	p.AuditResolveTimeout = 10
	p.AppealWindow = 5
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	const fee = uint64(100000)
	cut := fee * p.ProtocolFeeBps / 10000
	burn := fee * p.FeeBurnBps / 10000
	minerNet := fee - cut - burn
	validators := cut * p.ValidatorRewardBps / 10000
	team := cut * p.TeamFeeBps / 10000
	treasury := cut - validators - team
	clientAcc := sdk.AccAddress([]byte("client_appeal_expiry"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetMiner(f, f.ctx, "mP", types.Miner{MinerId: "mP", Stake: aeStake, Demand: treasury + team}))
	require.NoError(t, gvSetJob(f, f.ctx, "jEx", types.Job{
		JobId: "jEx", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: fee,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jEx", minerNet))
	require.NoError(t, f.keeper.HeldBurn.Set(f.ctx, "jEx", burn))
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Validators: validators, Team: team, Treasury: treasury}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jEx")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f))) // first timeout -> deferred (appeal window)
	jobD, _ := f.keeper.Job.Get(f.ctx, "jEx")
	require.NotContains(t, jobD.State, "resolved", "deferred")

	ctx11 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11).WithHeaderInfo(header.Info{Height: 11, AppHash: []byte("ah11")})
	require.NoError(t, f.keeper.EndBlock(ctx11)) // appeal expired -> deferral (no final clawback)
	job2, _ := f.keeper.Job.Get(f.ctx, "jEx")
	require.NotContains(t, job2.State, "resolved", "appeal expired without a verdict -> nothing closes")
	require.NotContains(t, job2.State, "clawed", "the final guillotine is gone: a network outage is not billed")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded (the audit is still waiting)")
	heldF, hErr := f.keeper.HeldFee.Get(f.ctx, "jEx")
	require.NoError(t, hErr, "held fee STILL held (neither released nor refunded)")
	require.Equal(t, minerNet, heldF)
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "bond INTACT")
	// The loop is alive: the appeal expiry set a new audit deadline (h=11 + timeout=10 -> 21).
	next, _ := f.keeper.PendingAuditResolve.Has(f.ctx, collections.Join(int64(21), "jEx"))
	require.True(t, next, "new deadline set after the expired appeal: the audit waits, it does not give up")
}
