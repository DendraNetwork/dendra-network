package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-028: three governable params (silence_slash_bps / appeal_window / audit_min_quorum), all dormant
// by default (0). These tests prove (a) that the 0/0/0 default reproduces the existing behaviour
// exactly; (b) that audit_min_quorum>0 raises the participation floor, so a tally below it does not
// slash; (c) that silence_slash_bps>0 adds a stake penalty on top of the no-quorum clawback.
//
// It reuses the helpers of adr028_antievasion_test.go (initFixture, aeReg, aeVerdict, aeFundModule,
// aeCtx6, aeStake). AuditLegacyFloorBasis=5 is independent of CommitteeSize=3, so the fallback floor is
// ceil(5/2)+1 = 4.

// (V2-1) Dormant default (silence=0, appeal=0, quorum=0) means unchanged behaviour.
//   - Validate() in mode 0: no new constraint, the existing end-to-end path is untouched.
//   - At timeout: 4 jurors voting "0" (the fallback floor via effectiveSlashFloor) cause a hard slash
//     of 80 %. No silence penalty (silence_slash_bps=0).
func TestADR028V2DefaultDormantMatchesV1(t *testing.T) {
	// (a) Dormant Validate: mode 0 with the new fields at 0 must pass.
	off := types.DefaultParams()
	require.Equal(t, uint64(0), off.SilenceSlashBps, "dormant default")
	require.Equal(t, uint64(0), off.AppealWindow, "dormant default")
	require.Equal(t, uint64(0), off.AuditMinQuorum, "dormant default")
	require.NoError(t, off.Validate(), "default (dormant) params must validate")

	// (b) Timeout with default params (minimal mode 1, the three new fields untouched): floor = 4.
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)                            // 4 jurors = fallback floor reached via effectiveSlashFloor (audit_min_quorum=0)
	aeAnchor(f, t, "jD", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: jurors summoned (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jD", "mJ1", "0")
	aeVerdict(f, t, "jD", "mJ2", "0")
	aeVerdict(f, t, "jD", "mJ3", "0")
	aeVerdict(f, t, "jD", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028v2_dflt"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jD", types.Job{
		JobId: "jD", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jD")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jD")
	require.NoError(t, err)
	require.Contains(t, job.State, "clawed")
	// ADR-034: an audit that actually took place. Four jurors voted "0": the quorum is reached and the
	// clawback attests to a verification. It must be marked `+quorum` and not `+noquorum` (an audit
	// that expired for lack of votes), which is counted separately: "at least N audits closed by
	// quorum" cannot be measured while both outcomes carry the same state.
	// The assertion is on `clawed+quorum` rather than `quorum`, because "noquorum" contains "quorum",
	// so a naive pattern would pass in both cases.
	require.Contains(t, job.State, "clawed+quorum", "quorum reached -> audit conducted")
	require.NotContains(t, job.State, "noquorum", "this is not an audit that expired for lack of votes")
	require.Len(t, job.SlashRecords, 1, "default: a single SlashRecord (hard slash), no silence penalty")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "hard slash of 80 %")
}

// (V2-2) audit_min_quorum=5 raises the floor: 4 jurors voting "0", enough to hard-slash at the
// fallback floor of 4, no longer trigger the hard slash, since 5 are required. This shows that the
// param takes effect, the converse of the case where 4 jurors were enough. silence_slash_bps stays 0,
// so no additional penalty applies.
func TestADR028V2MinQuorumRaisesFloor(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1: Validate requires sample>0 and audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.AuditMinQuorum = 5       // above the 4 jurors present: floor not reached, so no hard slash
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "audit_min_quorum=5 is valid in mode 1 (integer count, no upper bound)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)                            // 4 jurors voting "0": enough at the fallback floor of 4, not enough here (quorum 5)
	aeAnchor(f, t, "jQ", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: jurors summoned (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jQ", "mJ1", "0")
	aeVerdict(f, t, "jQ", "mJ2", "0")
	aeVerdict(f, t, "jQ", "mJ3", "0")
	aeVerdict(f, t, "jQ", "mJ4", "0")

	clientAcc := sdk.AccAddress([]byte("client_adr028v2_quor"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jQ", types.Job{
		JobId: "jQ", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jQ")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// The defended property: a raised governed quorum prevents the hard slash at 4 votes, and the stake
	// stays intact: below the bar no fallback costs anything, the audit is simply deferred. This test
	// fails if 4 "0" votes under a quorum of 5 remove a single udndr, whether by a returning hard slash or
	// by a resurrected clawback.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "raised quorum -> 4 \"0\" votes cost nothing (deferred, neither slash nor clawback)")
	job, _ := f.keeper.Job.Get(f.ctx, "jQ")
	require.NotContains(t, job.State, "resolved", "below the governed bar, nothing closes")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client not refunded: the audit is still pending")
}

// (V2-3) silence_slash_bps=2000: no quorum with a mute signal (at least one "0" verdict posted, below
// the floor of 4) would mean a clawback of the price plus a dedicated stake penalty of 20 % of the
// remaining stake. The penalty requires the signal, the ADR-028 "0" posted against a missing or suspect
// reveal. See V2-3bis for the pure-abstention case, which costs the price only.
func TestADR028V2SilenceSlashAddsPenalty(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1: Validate requires sample>0 and audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.SilenceSlashBps = 2000   // 20 %: dedicated silence penalty on top of the price clawback
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "silence_slash_bps=2000 is valid in mode 1 (<=10000)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)             // one juror present...
	aeAnchor(f, t, "jS", "mJ1")       // ADR-032: jurors summoned (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jS", "mJ1", "0") // ...posts "0" (ADR-028 mute signal): 1 < floor of 4, so no quorum
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_siln"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jS", types.Job{
		JobId: "jS", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jS")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// The silence penalty is unreachable, and that is the property under test.
	// It lived in clawbackPayment, on the "no-quorum -> clawback" path that deferral replaced: an
	// isolated "0" below the bar is no longer an exploitable signal, it is an audit that did not happen.
	// A real mute primary draws ceil(2/3) of "0" votes and lands on the slash branch, covered elsewhere.
	// This test keeps silence_slash_bps armed (2000) and checks that it does not apply: the param is
	// inert, not removed, so anyone wiring clawbackPayment again turns this test red immediately.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "one \"0\" below the bar with silence_slash armed -> stake intact: the penalty is unreachable")
	job, _ := f.keeper.Job.Get(f.ctx, "jS")
	require.Empty(t, job.SlashRecords, "no sanction recorded: nothing was judged")
	require.NotContains(t, job.State, "resolved", "deferred: nothing closes")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client not refunded: the audit is still pending")
}

// (V2-3bis) No quorum from pure abstentions (no verdict posted at all: uncertain jurors who abstain,
// or absent ones): the primary does not keep an unverified payment, but its bond is not penalised,
// because no mute signal exists. Penalising every no-quorum punished the honest miner for the juror's
// uncertainty: -20 % of bond compounded per incident with zero hard slashes. A real mute primary draws
// "0" votes instead, which is the V2-3 case.
func TestADR028V2SilenceSlashRequiresMuteSignal(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.SilenceSlashBps = 2000   // armed, but no verdict will be posted
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake) // no juror posts: pure abstentions, so no quorum and no signal
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_abst"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jA", types.Job{
		JobId: "jA", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jA")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Pure abstentions now cost nothing. Clawing back the price alone still punished the honest miner
	// for the juror's uncertainty: -20 % of bond compounded per incident, plus publicly displayed
	// clawbacks against honest operators. Deferral closes the whole class: no verdict, no movement.
	// This test fails if a no-quorum of abstentions costs the primary a single udndr.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "pure abstentions -> stake intact (neither price nor penalty: deferred)")
	job, _ := f.keeper.Job.Get(f.ctx, "jA")
	require.Empty(t, job.SlashRecords, "no sanction recorded")
	require.NotContains(t, job.State, "resolved", "nothing closes without a verdict")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client not refunded: the audit is still pending")
}

// (V2-4) Pro-honest veto at N=5: the count decides, not the stake majority. Five jurors vote: 3 "0"
// (invalid) and 2 "1" (valid), all at equal stake, so the stake majority says "invalid" (3 > 2), and
// with audit_min_quorum=0 that would hard-slash. With audit_min_quorum=4 (near-unanimity, ceil(2N/3) at
// N=5), four "invalid" verdicts are required, and a count of 3 < 4 yields no hard slash. The asymmetry
// favours the accused: an honest miner misjudged by a minority (3 of 5) is not slashed, where a stake
// majority would have condemned it.
func TestADR028V2VetoBlocksStakeMajoritySlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.AuditMinQuorum = 4       // veto: an "invalid" supermajority of the anchored seats is required for a hard slash (near-unanimity at this committee size)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4", "mJ5"} {
		aeReg(f, t, j, 1) // equal stake, so 3 "invalid" is a stake majority
	}
	aeAnchor(f, t, "jVeto", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032: jurors summoned (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jVeto", "mJ1", "0")
	aeVerdict(f, t, "jVeto", "mJ2", "0")
	aeVerdict(f, t, "jVeto", "mJ3", "0") // 3 invalid (stake majority) but count 3 < quorum 4
	aeVerdict(f, t, "jVeto", "mJ4", "1")
	aeVerdict(f, t, "jVeto", "mJ5", "1") // 2 valid

	clientAcc := sdk.AccAddress([]byte("client_adr028_veto00"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jVeto", types.Job{
		JobId: "jVeto", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jVeto")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// The veto defends one thing: a minority by count (3 of 5) never slashes, even when it is the stake
	// majority. It also costs nothing: 3 "0" and 2 "1" produce neither a slash (count < 4) nor a
	// vindication (2 < 4), so the job is deferred in full. This test fails if a stake majority once again
	// suffices to remove anything.
	job, _ := f.keeper.Job.Get(f.ctx, "jVeto")
	require.NotContains(t, job.State, "clawed", "no clawback below the bar")
	require.NotContains(t, job.State, "resolved", "3 of 5 is a verdict in neither direction -> deferred")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "veto: 3 of 5 invalid (stake majority) removes nothing — neither 80 % nor the price")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client not refunded: the audit is still pending")
}

// (V2-5) Veto at N=5, where near-unanimity decides: 4 "0" (invalid) and 1 "1" (valid) out of 5. The
// invalid count of 4 reaches the quorum of 4, so the primary is hard-slashed at 80 %. A single "valid"
// dissenter does not save a cheater rejected by near-unanimity (ceil(2N/3)=4): the veto protects the
// honest miner (V2-4) without letting the clear cheater escape.
func TestADR028V2VetoSlashesAtQuasiUnanimity(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.AuditMinQuorum = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4", "mJ5"} {
		aeReg(f, t, j, 1)
	}
	aeAnchor(f, t, "jUna", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032: jurors summoned (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jUna", "mJ1", "0")
	aeVerdict(f, t, "jUna", "mJ2", "0")
	aeVerdict(f, t, "jUna", "mJ3", "0")
	aeVerdict(f, t, "jUna", "mJ4", "0") // 4 invalid = near-unanimity (>= quorum 4)
	aeVerdict(f, t, "jUna", "mJ5", "1") // 1 valid dissenter: does not block the slash

	clientAcc := sdk.AccAddress([]byte("client_adr028_veto11"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jUna", types.Job{
		JobId: "jUna", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jUna")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "veto: an invalid supermajority (near-unanimity) -> hard slash of 80 %, the valid dissenter does not save the cheater")
}
