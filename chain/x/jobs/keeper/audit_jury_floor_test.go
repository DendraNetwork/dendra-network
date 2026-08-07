package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// TOO SMALL A JURY DEFERS, IT NEVER PUNISHES (keeper/audit_jury_floor.go).
//
// These two tests are deliberately BILATERAL: the first proves the guard BITES (jury below the floor ->
// no audit opened), the second proves it knows how to STAY QUIET (jury at the floor -> audit opens as
// usual). A guard tested only on its trigger may be a disguised unconditional `return`; a guard tested
// only on its silence may be dead code. Both directions are required.
//
// The first test defends a property that a chain without the floor violates: with one eligible juror
// against a quorum floor of four, the dispute opened anyway (`+disputed` plus an anchored committee) and
// `runAuditResolveTimeout` then clawed back the payment of a blameless primary. The assertion
// "NOT Contains(state, disputed)" is what pins that down.

// auditDeferStride from the keeper (unexported): the deferral lands at h+20.
const jfDeferStride = int64(20)

// (1) THE GUARD BITES — 2 registered miners, so a single juror is eligible (the primary excludes
// itself), against audit_min_quorum=4: the quorum is unreachable BY CONSTRUCTION. Expected: no audit
// opened, deadline DEFERRED, job untouched, payment not clawed back.
func TestAuditJuryTooSmallDefersInsteadOfPunishing(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000 // audit is CERTAIN: this exercises the jury floor, not the random draw
	p.AuditMinQuorum = 4     // floor above the number of eligible jurors
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)  // the primary
	aeReg(f, t, "mMuet", 50000) // the only other registrant -> sole eligible juror, and silent

	clientAcc := sdk.AccAddress([]byte("client_jury_small___"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jSmall", types.Job{
		JobId: "jSmall", State: "open+paid+optimistic", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jSmall")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jSmall")
	require.NoError(t, err)
	require.NotContains(t, job.State, "disputed",
		"unreachable jury -> NO dispute may be opened, otherwise the timeout claws back an honest primary")
	require.NotContains(t, job.State, "clawed", "an honest primary never pays for the absence of jurors")
	require.NotContains(t, job.State, "resolved", "the job waits for a network able to audit it")

	_, cErr := f.keeper.AuditCommittee.Get(f.ctx, "jSmall")
	require.Error(t, cErr, "no committee may be anchored while the jury is below the floor")

	// The current deadline is removed and RESCHEDULED further out: the held fee stays held, and the
	// deferral resolves itself once enough jurors have joined.
	has, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(int64(6), "jSmall"))
	require.NoError(t, err)
	require.False(t, has, "the deadline at the current height must be removed, otherwise the KV entry leaks")
	has, err = f.keeper.PendingAudit.Has(f.ctx, collections.Join(int64(6)+jfDeferStride, "jSmall"))
	require.NoError(t, err)
	require.True(t, has, "the audit must be DEFERRED, not dropped, otherwise evasion becomes free")

	mP, _ := f.keeper.Miner.Get(f.ctx, "mP")
	require.Equal(t, aeStake, mP.Stake, "no slash: the primary cannot be held responsible for the absence of jurors")
}

// (3) AN EXPIRED AUDIT IS NOT A CONDUCTED AUDIT (ADR-034). An audit that opens but is never voted on
// reaches its timeout. It must NOT be counted as a conducted audit, otherwise the C3 gate ("N audits
// closed by quorum") turns green on a network that verifies nothing — the counter feeding the gate
// would be structurally unable to contradict it.
func TestNoQuorumClawbackIsMarkedDistinctly(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditMinQuorum = 4
	// hold_bps = 10000 is the LIVE CHAIN configuration, and it changes the outcome here. The clawback
	// refunds the client out of the HELD FEE first, and only touches the bond for the SHORTFALL
	// (`antievasion.go`). At `hold_bps=0` (the dormant default) nothing is held, so the refund comes out
	// of the BOND. A test left at the default would therefore "prove" a violation of the "never the bond"
	// invariant that does not exist under the production configuration.
	p.HoldBps = 10000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)
	aeReg(f, t, "mP", aeStake)

	clientAcc := sdk.AccAddress([]byte("client_noquorum_____"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	// An audit ALREADY open (+disputed) reaching its deadline without a single verdict posted.
	require.NoError(t, gvSetJob(f, f.ctx, "jNoQ", types.Job{
		JobId: "jNoQ", State: "open+paid+optimistic+disputed", MinerId: "mP", Client: client, Fee: 100000,
	}))
	// The HELD fee, as `settleOptimistic` would have written it at full hold: it is what refunds the
	// client, leaving an honest miner's bond intact.
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jNoQ", uint64(100000)))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), "jNoQ")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jNoQ")
	require.NoError(t, err)
	// A no-quorum audit produces NO terminal state at all: nothing is written that a counter could read
	// as a conducted audit. The unconducted audit stays PENDING, the fee stays HELD, and the client
	// waits. This turns red if a terminal marker ever appears without a verdict (the return of the false
	// "conducted audit"), or if the held fee leaves the module account.
	require.NotContains(t, job.State, "resolved", "no verdict -> NO terminal state: the audit is PENDING, not conducted")
	require.NotContains(t, job.State, "quorum", "no quorum marker can be born of zero votes")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "jNoQ")
	require.NoError(t, hErr, "the fee is STILL held (released neither to the client nor to the operator)")
	require.Equal(t, uint64(100000), held)
	mPost, mErr := f.keeper.Miner.Get(f.ctx, "mP")
	require.NoError(t, mErr)
	require.Equal(t, aeStake, mPost.Stake,
		"pure abstention: NOTHING is taken — neither the price nor the bond")
}

// (2) THE GUARD STAYS QUIET — 5 registered miners, so 4 eligible jurors (the primary excludes itself),
// exactly the floor. Expected: the audit opens and the committee is anchored, unchanged. Without this
// second test, a change that disarmed the audit under all circumstances would look correct.
func TestAuditJurySufficientStillOpensAudit(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 10000
	p.AuditMinQuorum = 4
	p.AuditResolveTimeout = 240
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	aeReg(f, t, "mP", aeStake)
	aeReg(f, t, "mJ1", aeStake)
	aeReg(f, t, "mJ2", aeStake)
	aeReg(f, t, "mJ3", aeStake)
	aeReg(f, t, "mJ4", aeStake) // 4 eligible jurors excluding the primary == the floor

	clientAcc := sdk.AccAddress([]byte("client_jury_ok______"))
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, "jOk", types.Job{
		JobId: "jOk", State: "open+paid+optimistic", MinerId: "mP", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(int64(6), "jOk")))

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "jOk")
	require.NoError(t, err)
	require.Contains(t, job.State, "disputed", "jury at the floor -> the audit MUST open: the guard does not block here")

	comm, cErr := f.keeper.AuditCommittee.Get(f.ctx, "jOk")
	require.NoError(t, cErr, "the committee must be anchored at draw time (ADR-032)")
	require.NotEmpty(t, comm)

	has, err := f.keeper.PendingAudit.Has(f.ctx, collections.Join(int64(6)+jfDeferStride, "jOk"))
	require.NoError(t, err)
	require.False(t, has, "jury at the floor -> no deferral may be scheduled")
}
