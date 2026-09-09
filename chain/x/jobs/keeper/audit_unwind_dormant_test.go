package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-044, gesture 2 — THE BENCH EXISTS FOR THE ZERO, NOT FOR THE FEATURE.
//
// The record promises the parameter ships "dormant by default", and dormant means byte-identical: at
// `audit_unwind_blocks = 0` nothing may be read, nothing written, nothing decided. That promise is the
// one a reviewer cannot check by reading — a refusal and a call that never happened look the same from
// outside — so the decision is driven DIRECTLY here, through the test seam, on a debt so old that any
// armed configuration would unwind it. If the guard on zero ever moves below one of the reads, this is
// the case that goes red.
//
// The armed cases are the twins that give the dormant one its meaning: without them, a function that
// always returned false would pass the first test and the whole file would prove nothing.

const (
	unwJob   = "j-unw"
	unwMiner = "m-unw"
	unwHeld  = uint64(80000)
)

// unwParams returns params whose ONLY difference from the shipped defaults is the unwind age, so a
// failure can never be blamed on some other field drifting.
func unwParams(age uint64) types.Params {
	p := types.DefaultParams()
	p.AuditUnwindBlocks = age
	return p
}

func unwSeed(t *testing.T, f *fixture, since uint64, withBond uint64) {
	t.Helper()
	// ⚠️ A BOND WITHOUT A DISPUTER IS NOT A FIXTURE, IT IS A STATE THAT CANNOT ARISE. `MsgDispute`
	// writes both in the same breath, and `refundDisputeBond` is a NO-OP on an empty disputer — so a
	// fixture missing it would have measured the no-op and called it a stranded bond. First version of
	// this bench did exactly that, and blamed the code.
	aeFundModule(f)
	// ⚠️ AND A JOB WITHOUT A CLIENT IS NOT A FIXTURE EITHER. `refundRetainedToClient` is best-effort:
	// with no reachable client the retention goes to the TREASURY rather than being lost. A fixture
	// omitting the client therefore measures that fallback and reads it as a confiscation — the second
	// time this bench blamed the code for a field it had not set.
	clientAcc := sdk.AccAddress([]byte("client_unwind_1a____"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())

	disputer := ""
	if withBond > 0 {
		acc := sdk.AccAddress([]byte("disputer_unwind_1a__"))
		disputer, _ = f.addressCodec.BytesToString(acc)
		f.bank.setBalance(acc, sdk.NewCoins())
	}
	require.NoError(t, gvSetJob(f, f.ctx, unwJob, types.Job{
		JobId: unwJob, State: "open+paid+optimistic+disputed", MinerId: unwMiner,
		Fee: 100000, DisputeBond: withBond, Disputer: disputer, Client: client,
	}))
	require.NoError(t, gvSetMiner(f, f.ctx, unwMiner, types.Miner{
		MinerId: unwMiner, Stake: 1000000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, unwJob, unwHeld))
	if since > 0 {
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, unwJob, since))
	}
}

// ⭐ LE CAS CENTRAL. A debt 100000 blocks old, which every armed configuration below unwinds.
func TestUnwindDormantAtZeroRefusesEvenAnAncientDebt(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 1, 0)

	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(0)),
		"at audit_unwind_blocks=0 the decision must refuse BEFORE reading anything: dormant is a guarantee, not a tendency")

	held, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.NoError(t, err, "the retention must be untouched")
	require.Equal(t, unwHeld, held)
	job, _ := f.keeper.Job.Get(f.ctx, unwJob)
	require.NotContains(t, job.State, "unwound")
	require.NotContains(t, job.State, "resolved", "nothing is closed while the parameter is dormant")
}

func TestUnwindArmedPastTheAgeUndoesTheTradeWithoutSanction(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 1, 0)

	require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(500)),
		"armed and past the age, the retention is unwound")

	_, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.Error(t, err, "the held fee is CONSUMED, not left to age for ever")
	_, errSince := f.keeper.HeldSince.Get(f.ctx, unwJob)
	require.Error(t, errSince, "the date leaves with its debt, or held_oldest_age LIES about a settled job")

	job, _ := f.keeper.Job.Get(f.ctx, unwJob)
	require.Contains(t, job.State, "resolved", "resolved carries the anti-replay, as everywhere in this module")
	require.Contains(t, job.State, "unwound", "and unwound names WHY, so it is never read as a verdict")
	require.NotContains(t, job.State, "clawed", "NO sanction: no committee ruled")
	require.NotContains(t, job.State, "vindicated", "and none acquitted either")

	// ⛔ THE MINER IS NEITHER PAID NOR SLASHED. This is the whole difference between "silence yields
	// zero" and the two options the record refuses.
	m, mErr := f.keeper.Miner.Get(f.ctx, unwMiner)
	require.NoError(t, mErr)
	require.Equal(t, uint64(1000000), m.Stake, "the stake is INTACT: an audit that cannot conclude is not a verdict")
}

func TestUnwindArmedButTooYoungDefersAsBefore(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 1000, 0)

	// age = 1499 - 1000 = 499, one block short of the armed 500.
	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 1499, unwJob, unwParams(500)),
		"one block short of the age must still defer: the boundary is the parameter, not an approximation")
	held, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.NoError(t, err)
	require.Equal(t, unwHeld, held)
}

// ⛔ FAIL-CLOSED. A debt with no readable date is precisely what must NOT be unwound: the age is the
// only thing separating "stranded" from "still being verified".
func TestUnwindWithoutAHeldSinceDefers(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 0, 0) // HeldFee written, HeldSince deliberately absent

	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(500)),
		"an age that cannot be established is not an age; deferring is the only answer that loses nobody")
	held, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.NoError(t, err)
	require.Equal(t, unwHeld, held)
}

// ⛔ THE ZERO-VALUE TRAP IN ITS ARITHMETIC FORM. `now - since` on uint64 wraps when the date is ahead,
// and a wrapped age is ENORMOUS — the unwind would fire on the YOUNGEST debts, which is the exact
// inverse of the rule.
func TestUnwindWithADateAheadOfTheHeightDefers(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 9000, 0)

	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 10, unwJob, unwParams(500)),
		"a date ahead of the height must refuse: subtracting would wrap and read as a very old debt")
	held, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.NoError(t, err)
	require.Equal(t, unwHeld, held)
}

func TestUnwindWithNoRetentionDefers(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, gvSetJob(f, f.ctx, unwJob, types.Job{
		JobId: unwJob, State: "open+paid+optimistic+disputed", MinerId: unwMiner, Fee: 100000,
	}))
	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(500)),
		"no debt means nothing to unwind, and the deferral is then about the audit itself")
}

// ⭐ THE BOND IS RETURNED, AND THE MODULE'S OWN RULE IS THE REASON. `resolveDisputedAudit` writes it:
// RETURN when no instance remains. An unwind is that case. Escrowing it on a job now resolved would
// strand it for ever; confiscating it would sanction a disputer no committee ever found wrong.
func TestUnwindReturnsTheDisputeBondRatherThanStrandingIt(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 1, 50000)

	require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(500)))

	job, _ := f.keeper.Job.Get(f.ctx, unwJob)
	require.Equal(t, uint64(0), job.DisputeBond,
		"the bond is consumed by the REFUND, not left escrowed on a resolved job nothing revisits")
	// ⛔ ABSENT IS ZERO, AND DEMANDING A RECORD HERE WAS THE MISTAKE. Nothing reached the Treasury, so
	// the Pools entry was never written at all — `Get` returns not-found, and reading that as a failure
	// would turn the very proof we want into a red. The treasury is read with its zero default, and
	// only a READ ERROR would be a genuine unknown.
	var treasury uint64
	if pools, pErr := f.keeper.Pools.Get(f.ctx); pErr == nil {
		treasury = pools.Treasury
	}
	require.Equal(t, uint64(0), treasury,
		"nothing was confiscated: confiscation is for a disputer proven wrong, and none was")
}

// ⛔ AN UNWIND SHORTER THAN THE RESOLVE TIMEOUT REPLACES THE DEFERRAL INSTEAD OF BOUNDING IT.
func TestValidateRefusesAnUnwindShorterThanTheResolveTimeout(t *testing.T) {
	p := types.DefaultParams()
	p.AuditResolveTimeout = 240
	p.AuditUnwindBlocks = 239
	require.Error(t, p.Validate(),
		"the retention would be undone before the audit it waits for has been retried even once")

	p.AuditUnwindBlocks = 240
	require.NoError(t, p.Validate(), "equal is the boundary and it is allowed")

	p.AuditUnwindBlocks = 0
	require.NoError(t, p.Validate(), "a pair with a zero compares nothing: dormant must always validate")
}

// ⭐ DORMANT COSTS ZERO BYTES, AND THAT IS WHY THE SWITCH ITSELF CHANGES NO STATE. proto3 omits a zero,
// so Params carrying the new field at 0 serialise EXACTLY as they did before the field existed. This is
// the measurable half of the claim written in docker/CONSENSUS_EPOCH; the epoch still moves, because an
// ARMED parameter is where the two binaries diverge.
func TestDormantParamAddsNoBytesToTheStoredParams(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.AuditUnwindBlocks, "the shipped default is dormant")
	dormant, err := p.Marshal()
	require.NoError(t, err)

	p.AuditUnwindBlocks = 1
	armed, errArmed := p.Marshal()
	require.NoError(t, errArmed)

	require.Less(t, len(dormant), len(armed),
		"a non-zero value MUST cost bytes; if it does not, proto3 is not omitting the zero and the "+
			"byte-identity claim in docker/CONSENSUS_EPOCH is false")
}
