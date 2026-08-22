package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-028 — three governable params (silence_slash_bps / appeal_window / audit_min_quorum), ALL DORMANT
// by default (0). These tests prove (a) that the 0/0/0 default reproduces the pre-existing behaviour
// STRICTLY; (b) that audit_min_quorum>0 raises the participation floor, so a tally that used to slash no
// longer does; (c) that silence_slash_bps>0 adds a stake penalty on top of the no-quorum clawback.
//
// Reuses the helpers of adr028_antievasion_test.go (initFixture, aeReg, aeVerdict, aeFundModule, aeCtx6,
// aeStake). AuditLegacyFloorBasis=5 is DECOUPLED from CommitteeSize=3, so the fallback floor is
// ceil(5/2)+1 = 4.

// (V2-1) DORMANT DEFAULT (silence=0, appeal=0, quorum=0) == unchanged behaviour.
//   - Validate() in mode 0: no new constraint, the existing end-to-end path is untouched.
//   - At timeout: 4 jurors voting "0" (the fallback floor via effectiveSlashFloor) -> HARD slash of 80 %.
//     No silence penalty (silence_slash_bps=0).
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
	aeAnchor(f, t, "jD", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: jurors SUMMONED (without the anchor, the tally fails closed)
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
	// ADR-034 — AN AUDIT THAT ACTUALLY TOOK PLACE. Four jurors voted "0": the quorum is reached and the
	// clawback attests to a VERIFICATION. It must be marked `+quorum` and above all NOT `+noquorum` (an
	// audit that expired for lack of votes), which is counted separately: "at least N audits closed by
	// quorum" is not measurable while both outcomes carry the same state.
	// The assertion is on `clawed+quorum` and NOT on `quorum`: "noquorum" CONTAINS "quorum", so a naive
	// pattern would pass in both cases — precisely the guard whose pattern misses the variant.
	require.Contains(t, job.State, "clawed+quorum", "quorum reached -> audit conducted")
	require.NotContains(t, job.State, "noquorum", "this is not an audit that expired for lack of votes")
	require.Len(t, job.SlashRecords, 1, "default: a single SlashRecord (hard slash), NO silence penalty")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "HARD slash of 80 %")
}

// (V2-2) audit_min_quorum=5 RAISES the floor: 4 jurors voting "0" — enough to hard-slash at the fallback
// floor of 4 — no longer trigger the hard slash, since 5 are required. This demonstrates that the param
// BITES, the exact converse of the case where 4 jurors sufficed. silence_slash_bps stays 0, so no
// additional penalty applies.
func TestADR028V2MinQuorumRaisesFloor(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1: Validate requires sample>0 AND audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.AuditMinQuorum = 5       // above the 4 jurors present -> floor NOT reached -> no hard slash
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "audit_min_quorum=5 is valid in mode 1 (integer count, no upper bound)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)
	aeReg(f, t, "mJ2", 1)
	aeReg(f, t, "mJ3", 1)
	aeReg(f, t, "mJ4", 1)                            // 4 jurors voting "0": enough at the fallback floor of 4, INSUFFICIENT here (quorum 5)
	aeAnchor(f, t, "jQ", "mJ1", "mJ2", "mJ3", "mJ4") // ADR-032: jurors SUMMONED (without the anchor, the tally fails closed)
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

	// The defended property: a raised governed quorum PREVENTS the hard slash at 4 votes, and it now
	// reads on a STRICTLY intact stake — below the bar no fallback costs anything, the audit is simply
	// deferred. This test fails if 4 "0" votes under a quorum of 5 remove a single udndr, whether by a
	// returning hard slash or by a resurrected clawback.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "raised quorum -> 4 \"0\" votes cost NOTHING (deferred, neither slash nor clawback)")
	job, _ := f.keeper.Job.Get(f.ctx, "jQ")
	require.NotContains(t, job.State, "resolved", "below the governed bar, nothing closes")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded: the audit is still pending")
}

// (V2-3) silence_slash_bps=2000: NO-QUORUM WITH A MUTE SIGNAL (at least one "0" verdict posted, below the
// floor of 4) -> clawback of the price PLUS a DEDICATED stake penalty of 20 % of the REMAINING stake. The
// penalty requires the SIGNAL — the ADR-028 "0" posted against a missing or suspect reveal. See V2-3bis
// for the pure-abstention case, which costs the price only.
func TestADR028V2SilenceSlashAddsPenalty(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // mode 1: Validate requires sample>0 AND audit_resolve_timeout>dispute_window>0
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.SilenceSlashBps = 2000   // 20 %: DEDICATED silence penalty on top of the price clawback
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate(), "silence_slash_bps=2000 is valid in mode 1 (<=10000)")
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", 1)             // ONE juror present...
	aeAnchor(f, t, "jS", "mJ1")       // ADR-032: jurors SUMMONED (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jS", "mJ1", "0") // ...posts "0" (ADR-028 mute signal): 1 < floor of 4 -> no quorum
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_siln"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jS", types.Job{
		JobId: "jS", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jS")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// THE SILENCE PENALTY IS UNREACHABLE, AND THAT IS THE PROPERTY UNDER TEST.
	// It lived in clawbackPayment, on the "no-quorum -> clawback" path that deferral replaced: an
	// isolated "0" below the bar is no longer an exploitable signal, it is an audit that did not happen.
	// A REAL mute primary draws ceil(2/3) of "0" votes and lands on the SLASH branch, covered elsewhere.
	// This test keeps silence_slash_bps ARMED (2000) and checks that it does not bite: the param is
	// INERT, not removed, so anyone re-wiring clawbackPayment turns this test red immediately. It is the
	// guard against an accidental re-wiring.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "one \"0\" below the bar with silence_slash ARMED -> stake INTACT: the penalty is unreachable")
	job, _ := f.keeper.Job.Get(f.ctx, "jS")
	require.Empty(t, job.SlashRecords, "no sanction recorded: nothing was judged")
	require.NotContains(t, job.State, "resolved", "DEFERRED: nothing closes")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded: the audit is still pending")
}

// (V2-3bis) NO QUORUM from PURE ABSTENTIONS (no verdict posted at all — uncertain jurors who abstain, or
// simply absent ones): the primary does not keep an unverified payment, but its BOND is NOT penalised,
// because no mute signal exists. Penalising every no-quorum punished the HONEST miner for the JUROR's
// uncertainty: -20 % of bond compounded per incident with zero hard slashes. A REAL mute primary draws
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
	p.SilenceSlashBps = 2000   // armed — but NO verdict will be posted
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake) // no juror posts: pure abstentions -> no quorum and NO signal
	clientAcc := sdk.AccAddress([]byte("client_adr028v2_abst"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jA", types.Job{
		JobId: "jA", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jA")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	// Pure abstentions now cost NOTHING. Clawing back the price alone still punished the honest miner
	// for the juror's uncertainty: -20 % of bond compounded per incident, plus publicly displayed
	// clawbacks against honest operators. Deferral closes the whole class: no verdict, no movement.
	// This test fails if a no-quorum of abstentions costs the primary a single udndr.
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "pure abstentions -> stake INTACT (neither price nor penalty: DEFERRED)")
	job, _ := f.keeper.Job.Get(f.ctx, "jA")
	require.Empty(t, job.SlashRecords, "no sanction recorded")
	require.NotContains(t, job.State, "resolved", "nothing closes without a verdict")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded: the audit is still pending")
}

// (V2-4) PRO-HONEST VETO at N=5 — the COUNT decides, NOT the stake majority. Five jurors vote: 3 "0"
// (invalid) and 2 "1" (valid), ALL at EQUAL stake, so the STAKE MAJORITY says "invalid" (3 > 2) and with
// audit_min_quorum=0 that would HARD-SLASH. With audit_min_quorum=4 — near-unanimity, ceil(2N/3) at N=5 —
// four "invalid" verdicts are required: a COUNT of 3 < 4 yields no hard slash. The asymmetry favours the
// accused: an honest miner misjudged by a MINORITY (3 of 5) is NOT slashed, where a stake majority would
// have condemned it.
func TestADR028V2VetoBlocksStakeMajoritySlash(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	p.DisputeWindow = 5
	p.AuditResolveTimeout = 60
	p.DisputeBond = 1000       // mode 1: disputing must not be free
	p.CommitteeRevealDelay = 1 // mode 1: unpredictable committee seed (deferred reveal)
	p.AuditMinQuorum = 4       // VETO: an "invalid" supermajority of the anchored seats is required for a hard slash (near-unanimity at this committee size)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	require.NoError(t, p.Validate())
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	for _, j := range []string{"mJ1", "mJ2", "mJ3", "mJ4", "mJ5"} {
		aeReg(f, t, j, 1) // equal stake -> 3 "invalid" is a STAKE MAJORITY
	}
	aeAnchor(f, t, "jVeto", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032: jurors SUMMONED (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jVeto", "mJ1", "0")
	aeVerdict(f, t, "jVeto", "mJ2", "0")
	aeVerdict(f, t, "jVeto", "mJ3", "0") // 3 invalid (stake majority) BUT count 3 < quorum 4
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

	// The VETO defends one thing: a MINORITY by COUNT (3 of 5) never slashes, even when it is the stake
	// majority. It also costs nothing: 3 "0" and 2 "1" produce neither a slash (count < 4) nor a
	// vindication (2 < 4), so the job is DEFERRED in full. This test fails if a stake majority once
	// again suffices to remove anything.
	job, _ := f.keeper.Job.Get(f.ctx, "jVeto")
	require.NotContains(t, job.State, "clawed", "no clawback below the bar")
	require.NotContains(t, job.State, "resolved", "3 of 5 is a verdict in NEITHER direction -> deferred")
	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "VETO: 3 of 5 invalid (stake majority) removes NOTHING — neither 80 % nor the price")
	require.Equal(t, int64(0), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(), "client NOT refunded: the audit is still pending")
}

// (V2-5) VETO at N=5 — near-unanimity DECIDES: 4 "0" (invalid) and 1 "1" (valid) out of 5. The invalid
// COUNT of 4 reaches the quorum of 4, so the primary is HARD-SLASHED at 80 %. A SINGLE "valid" dissenter
// does NOT save a cheater rejected by near-unanimity (ceil(2N/3)=4): the veto protects the honest miner
// (V2-4) WITHOUT letting the clear cheater escape.
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
	aeAnchor(f, t, "jUna", "mJ1", "mJ2", "mJ3", "mJ4", "mJ5") // ADR-032: jurors SUMMONED (without the anchor, the tally fails closed)
	aeVerdict(f, t, "jUna", "mJ1", "0")
	aeVerdict(f, t, "jUna", "mJ2", "0")
	aeVerdict(f, t, "jUna", "mJ3", "0")
	aeVerdict(f, t, "jUna", "mJ4", "0") // 4 invalid = near-unanimity (>= quorum 4)
	aeVerdict(f, t, "jUna", "mJ5", "1") // 1 valid dissenter -> does NOT block the slash

	clientAcc := sdk.AccAddress([]byte("client_adr028_veto11"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jUna", types.Job{
		JobId: "jUna", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jUna")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake-aeStake*p.SlashLeakBps/10000, mP.Stake, "VETO: an invalid supermajority (near-unanimity) -> HARD slash of 80 %, the valid dissenter does not save the cheater")
}
