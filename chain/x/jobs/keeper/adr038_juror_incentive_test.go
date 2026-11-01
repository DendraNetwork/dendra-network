package keeper_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The juror incentive, and why no incentive is added.
//
// The gap is real and is measured here: no path pays a juror and no path sanctions one. The obvious
// reaction is to add a reward or a penalty, and it is wrong for a reason that has nothing to do with
// money.
//
// A verdict is an unpriced, unproven byte. `CreateCommit` accepts any `ResultCommit` string under the
// key `<jobId>__verdict__<minerId>`, and nothing on-chain binds that string to an inference having been
// run. The tally (`auditVerdictTally`) then distinguishes exactly one token, `"0"`, the accusation.
// Every other string, including one a juror can emit without loading a model, counts as a valid vote.
//
// So the cheapest output a juror can produce is a vindication, not silence (measured by T1/T2 below).
// Silence changes nothing (the audit defers, the fee stays held); one free byte per seat closes the
// audit and releases the payment. Any pressure to participate (a reward per vote, a reward per
// participation, a stake penalty for silence, or the loss of access to paid work) is therefore a
// pressure to emit that byte, and buys "the primary is paid without verification", the outcome the
// audit exists to deny.
//
// The problem is that the observable is one-sided: there is no way to be counted present and
// undecided. Until abstention is a first-class, distinguishable token, no incentive of any kind can be
// made safe. So none is added, and these tests pin the absence so that one cannot be wired in silently.
//
// Same intent as `params_inert_test.go` and `readme_public_claims_test.go`: an absence that a public
// document describes must be executable, otherwise it is only believed.

// a38Seats / a38Bar are the real draw size (`auditCommitteeDrawSize` = 15) and the relative bar it
// implies under default params (`audit_min_quorum = 0`): ⌈2/3 x 15⌉ = 10, which dominates the absolute
// v1 floor of 4. Both the slash and the vindication read that same bar (`auditRelativeBar`).
const (
	a38Seats = 15
	a38Bar   = 10
)

// a38Miner registers a juror with an operator address, so that "was this juror paid?" is a question
// about a bank balance and not only about a stake field. `aeReg` leaves the operator empty, which would
// make every juror share one bucket and hide a payment.
func a38Miner(f *fixture, t *testing.T, id string, stake uint64) sdk.AccAddress {
	t.Helper()
	acc := sdk.AccAddress(([]byte(id + "_op_adr038________________"))[:20])
	op, err := f.addressCodec.BytesToString(acc)
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: stake, Operator: op}))
	return acc
}

// a38Held is the fee held on the audited job. It is non-zero on purpose: "the audit releases the
// payment" must be an observed transfer to the primary's operator, not an inference from the branch
// taken. A claim about money is measured in money.
const a38Held = uint64(70000)

// a38Jury sets up a primary plus `a38Seats` anchored jurors, an optimistic job under dispute with a
// held fee, and a resolution deadline at height 6. It returns the juror ids, their operator accounts,
// and the primary's operator account.
func a38Jury(f *fixture, t *testing.T, jobId string) ([]string, []sdk.AccAddress, sdk.AccAddress) {
	t.Helper()
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	aeFundModule(f)

	primAcc := a38Miner(f, t, "p38", aeStake)
	ids := make([]string, 0, a38Seats)
	accs := make([]sdk.AccAddress, 0, a38Seats)
	for i := 0; i < a38Seats; i++ {
		id := "j38_" + string(rune('a'+i))
		ids = append(ids, id)
		accs = append(accs, a38Miner(f, t, id, 1_000_000))
	}
	aeAnchor(f, t, jobId, ids...)

	clientAcc := sdk.AccAddress(([]byte("client_adr038_______"))[:20])
	client, err := f.addressCodec.BytesToString(clientAcc)
	require.NoError(t, err)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
		JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: "p38", Client: client, Fee: 100000,
	}))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, a38Held))
	require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(int64(6), jobId)))
	return ids, accs, primAcc
}

// T1: the cheapest juror output is a vindication.
//
// Ten of fifteen anchored seats post `"x"`, a byte that attests to no inference (no model loaded, no
// answer read, no comparison made). It is what a juror emits when it is rewarded only for being
// counted. The audit closes at the relative bar, the primary is vindicated and its payment released.
//
// This measurement is why no juror incentive exists: an incentive to participate is not neutral with respect to the
// verdict, because the zero-effort direction and the "pay the primary" direction are the same. Any
// mechanism that makes silence more expensive than a byte therefore subsidises unverified payment.
func TestADR038ZeroEffortVerdictVindicatesThePrimary(t *testing.T) {
	f := initFixture(t)
	ids, _, primAcc := a38Jury(f, t, "j38job")
	primBefore := f.bank.balOf(primAcc)

	// The zero-effort byte. Not "0": "0" is the accusation, the only token that costs a juror
	// something to be wrong about. Anything else is free and reads as "valid".
	for i := 0; i < a38Bar; i++ {
		aeVerdict(f, t, "j38job", ids[i], "x")
	}

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "j38job")
	require.NoError(t, err)
	require.Contains(t, job.State, "+resolved+vindicated+quorum",
		"ten free bytes over fifteen anchored seats close the audit at the relative bar and vindicate")
	require.Equal(t, uint64(a38Bar), job.AuditVoters, "the ten free bytes are counted as votes")

	mP, err := f.keeper.Miner.Get(f.ctx, "p38")
	require.NoError(t, err)
	require.Equal(t, aeStake, mP.Stake, "vindicated: the primary keeps its stake")

	// And the money moves: the held fee lands on the primary's operator, and the retention key is
	// gone. In coins, the decision rests on this: ten bytes that attest to no inference are enough to make
	// the network pay for unverified work.
	require.True(t, primBefore.Add(sdk.NewInt64Coin("udndr", int64(a38Held))).Equal(f.bank.balOf(primAcc)),
		"the held fee was released to the primary on the strength of ten free bytes")
	_, hErr := f.keeper.HeldFee.Get(f.ctx, "j38job")
	require.Error(t, hErr, "the retention is consumed, not merely marked")
}

// T2: silence decides nothing, the byte decides everything.
//
// The counterfactual of T1, identical except that the same ten seats post nothing. The audit does not
// close: it defers (audit_sampling.go, `default` branch), the fee stays held, the stake intact, and
// nothing is released.
//
// Read together, T1 and T2 give the exact size of what a participation incentive would buy: the
// difference between "nothing happens" and "the payment is released" is ten bytes, and no juror had to
// verify anything to produce them. Any reward, or any penalty for silence, would push miners down that
// gradient.
func TestADR038SilenceDefersInsteadOfDeciding(t *testing.T) {
	f := initFixture(t)
	_, _, primAcc := a38Jury(f, t, "j38mute")
	primBefore := f.bank.balOf(primAcc)

	require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

	job, err := f.keeper.Job.Get(f.ctx, "j38mute")
	require.NoError(t, err)
	require.NotContains(t, job.State, "+resolved", "no quorum -> the audit defers, it does not conclude")
	require.NotContains(t, job.State, "vindicated")
	require.NotContains(t, job.State, "clawed")
	require.Equal(t, uint64(0), job.AuditVoters, "nobody voted")

	// The other half of the comparison with T1: no coin moves, and the retention survives. Held is not
	// lost, it is owed, which is why the deferral is acceptable and the free byte is not.
	require.True(t, primBefore.Equal(f.bank.balOf(primAcc)), "silence releases nothing")
	held, hErr := f.keeper.HeldFee.Get(f.ctx, "j38mute")
	require.NoError(t, hErr, "the retention survives the deferral: the debt stays on the books")
	require.Equal(t, a38Held, held)
}

// a38MinerPaid returns `Pools.MinerPaid`, the module's own cumulative record of what it has paid out
// to miners, or 0 when no `Pools` record exists yet. That default is sound here: `MinerPaid` is only
// ever credited (`+= paid`, never decremented), so "no record" cannot hide a payout and can only mean
// none has happened. A defaulted read would be wrong on a field that moves in both directions, which is
// why the justification is written down.
//
// The presence of the record is not the invariant: the hard-slash branch legitimately creates `Pools`
// to credit the Treasury, so requiring "absent before, absent after" would fail on a path that pays no
// juror at all. Only `MinerPaid` is compared.
func a38MinerPaid(f *fixture) uint64 {
	pools, err := f.keeper.Pools.Get(f.ctx)
	if err != nil {
		return 0
	}
	return pools.MinerPaid
}

// T3: no juror is paid, and no juror is sanctioned.
//
// The gap itself, measured on both outcomes an audit can produce. Across a vindication and a hard
// slash, the two branches that dispose of real coins, every anchored juror ends with the stake and the
// bank balance it started with, and `Pools.MinerPaid` (the module's own record of what it has paid out
// to miners) does not move.
//
// This is the executable form of the rule: judging is unpaid and unpunished. If a future
// change wires a juror payment or a juror penalty, in either direction and whatever its justification,
// this test goes red and the decision must be revisited before it can go green again. The absence is a
// decision, so it is defended like one.
func TestADR038NoJurorIsPaidNorSanctioned(t *testing.T) {
	for _, tc := range []struct {
		name, jobId, vote, wantState string
	}{
		{"vindication", "j38pay_v", "x", "+resolved+vindicated+quorum"},
		{"hard slash", "j38pay_s", "0", "+resolved+clawed+quorum"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := initFixture(t)
			ids, accs, _ := a38Jury(f, t, tc.jobId)

			before := make([]sdk.Coins, len(accs))
			for i, a := range accs {
				before[i] = f.bank.balOf(a)
			}
			paidBefore := a38MinerPaid(f)

			for i := 0; i < a38Bar; i++ {
				aeVerdict(f, t, tc.jobId, ids[i], tc.vote)
			}
			require.NoError(t, f.keeper.EndBlock(aeCtx6(f)))

			job, err := f.keeper.Job.Get(f.ctx, tc.jobId)
			require.NoError(t, err)
			require.Contains(t, job.State, tc.wantState, "the branch under test must be the one exercised")

			for i, id := range ids {
				m, gErr := f.keeper.Miner.Get(f.ctx, id)
				require.NoError(t, gErr)
				require.Equal(t, uint64(1_000_000), m.Stake,
					"juror %s: stake moved — a juror is neither rewarded nor slashed for its verdict", id)
				require.True(t, before[i].Equal(f.bank.balOf(accs[i])),
					"juror %s: operator balance moved — no coin path may reach a juror", id)
			}
			require.Equal(t, paidBefore, a38MinerPaid(f),
				"MinerPaid moved: something was paid out for the act of judging")
		})
	}
}

// T4: `silence_slash_bps` has no production caller.
//
// The second half of the gap. `silence_slash_bps` is displayed by `query jobs params` and documented in
// the shipped genesis as "what keeps staying mute -EV". It lives inside `clawbackPayment`, which is
// called from nowhere: the no-quorum path it belonged to was replaced by deferral. The parameter is
// inert, not dormant. A governance vote raising it would pass and change nothing, which is worse than a
// value at zero because the decision would appear to have been taken.
//
// A source scan is the right instrument here, and only here: the claim is "no call site exists", and
// no runtime observation can establish the absence of a caller. Both directions go red: a new caller
// (someone wired the penalty again without re-reading why it was unwired) and a vanished definition
// (the entry would describe a state of the tree that is gone).
func TestADR038SilenceSlashHasNoProductionCaller(t *testing.T) {
	var definitions, callSites []string

	err := filepath.Walk("..", func(path string, info os.FileInfo, e error) error {
		if e != nil {
			return e
		}
		if info.IsDir() || !strings.HasSuffix(path, ".go") ||
			strings.HasSuffix(path, "_test.go") || strings.HasSuffix(path, ".pb.go") {
			return nil
		}
		b, rErr := os.ReadFile(path)
		if rErr != nil {
			return rErr
		}
		for _, line := range strings.Split(string(b), "\n") {
			code := line
			if i := strings.Index(code, "//"); i >= 0 {
				code = code[:i] // a comment naming the function is not a call site
			}
			if !strings.Contains(code, "clawbackPayment") {
				continue
			}
			if strings.Contains(code, "func (k Keeper) clawbackPayment(") {
				definitions = append(definitions, path)
				continue
			}
			if strings.Contains(code, "clawbackPayment(") {
				callSites = append(callSites, path+": "+strings.TrimSpace(line))
			}
		}
		return nil
	})
	require.NoError(t, err)

	require.Len(t, definitions, 1,
		"clawbackPayment must still exist exactly once: the design keeps an inert function, not a removed one")
	require.Empty(t, callSites,
		"clawbackPayment acquired a caller -> silence_slash_bps is no longer inert -> revisit the juror-penalty decision "+
			"before making this green again: the penalty presupposes a payment the juror never receives, "+
			"and its cheapest avoidance is a free byte that vindicates")
}
