package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/bech32"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ ADR-048 — WHAT THE MONEY REVIEW OF THE RELAUNCH BATCH FOUND ════════════════════════════════
//
// An independent review of epoch 14 confirmed that coins are conserved (nothing paid twice, nothing
// refunded twice) and found three paths where they had no way out or reached the wrong party. Each case
// below reproduces one of them on the shipped handlers.

// (1) A CHEAT PROVEN AFTER FINALITY IS REFUNDED OUT OF THE BOND. The retention was released and the cut
// paid out at finality, so nothing is retained; AdjudicateDispute used to send the whole slash to the
// Treasury and the client got nothing back. The timeout path (`slashCheatedPrimary`) already owed the
// client what had left the module; the permissionless path now does the same.
func TestEpoch14_ACheatProvenAfterFinalityIsRefundedOutOfTheBond(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	disp, _ := f.addressCodec.BytesToString(sdk.AccAddress([]byte("disputer_e14_final__")))

	const bigStake = uint64(1_000_000_000_000)
	const fee = uint64(100000)
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.HoldBps = 10000
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20)
	aeFundModule(f)

	cut := fee * p.ProtocolFeeBps / 10000
	credited := cut - cut*p.ValidatorRewardBps/10000 // the right the job credited at finality
	require.NoError(t, gvSetMiner(f, f.ctx, "mA", types.Miner{MinerId: "mA", Stake: bigStake, Demand: credited}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mB", types.Miner{MinerId: "mB", Stake: bigStake}))
	require.NoError(t, gvSetMiner(f, f.ctx, "mC", types.Miner{MinerId: "mC", Stake: bigStake}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Stake: 1}))
	}
	clientAcc := sdk.AccAddress([]byte("client_e14_final_adj"))
	client, _ := f.addressCodec.BytesToString(clientAcc)
	f.bank.setBalance(clientAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Commit.Set(f.ctx, "jf__mA", types.Commit{ResultCommit: "1,0,0"}))
	for _, id := range []string{"mD", "mE", "mF"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jf__redo__"+id, types.Commit{ResultCommit: "0,1,0"}))
	}
	redoAnchor(f, t, "jf", "mD", "mE", "mF")
	// FINAL: no HeldFee, no HeldBurn, no retained share — the state releaseHeld leaves.
	require.NoError(t, gvSetJob(f, f.ctx, "jf", types.Job{
		JobId: "jf", State: "open+paid+optimistic+disputed", MinerId: "mA", Client: client, Fee: fee,
		Disputer: disp, DisputeBond: 0, DisputeHeight: 1,
	}))
	var treasuryBefore uint64
	if pl, err := f.keeper.Pools.Get(f.ctx); err == nil {
		treasuryBefore = pl.Treasury
	}

	_, err := srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{Creator: disp, JobId: "jf"})
	require.NoError(t, err)

	slash := bigStake * p.SlashLeakBps / 10000
	require.Equal(t, int64(fee), f.bank.balOf(clientAcc).AmountOf("udndr").Int64(),
		"nothing is retained after finality, so the WHOLE price is owed by the bond, as on the timeout path")
	pl, _ := f.keeper.Pools.Get(f.ctx)
	require.Equal(t, treasuryBefore+slash-fee, pl.Treasury, "the rest of the slash, and only the rest, goes to the Treasury")
	mA, _ := f.keeper.Miner.Get(f.ctx, "mA")
	require.Equal(t, bigStake-slash, mA.Stake)
	require.Equal(t, uint64(0), mA.Demand, "and the right the job had credited at finality is reversed")
}

// (2) A RESOLVED JOB CANNOT BE DISPUTED. An unwound job is `+resolved+unwound` without `+disputed`; the
// dispute used to be accepted, the bond escrowed, and every resolution path then refused the job.
func TestEpoch14_AResolvedJobCannotBeDisputedAndItsBondIsNotTaken(t *testing.T) {
	for _, c := range []struct {
		state    string
		accepted bool
	}{{"open+paid+optimistic+resolved+unwound", false}, {"open+paid+optimistic", true}} {
		f := initFixture(t)
		p := types.DefaultParams()
		p.DisputeWindow = 10
		p.AuditResolveTimeout = 120
		p.DisputeBond = 1000
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		dispAcc := sdk.AccAddress([]byte("disputer_e14_resolvd"))
		disp, _ := f.addressCodec.BytesToString(dispAcc)
		f.bank.setBalance(dispAcc, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(5000))))
		require.NoError(t, gvSetJob(f, f.ctx, "jR", types.Job{JobId: "jR", State: c.state, MinerId: "mR", Fee: 1000}))

		_, err := keeper.NewMsgServerImpl(f.keeper).DisputeVerdict(f.ctx, &types.MsgDisputeVerdict{Creator: disp, JobId: "jR"})
		if c.accepted {
			require.NoError(t, err, "control: a settled, open job is still disputable")
			require.Equal(t, int64(4000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(), "and its bond is escrowed")
		} else {
			require.Error(t, err, "%s: no instance remains to hear the dispute", c.state)
			require.Equal(t, int64(5000), f.bank.balOf(dispAcc).AmountOf("udndr").Int64(), "so the bond is NOT taken")
		}
	}
}

// (3) GOVERNANCE RESOLUTION SETTLES THE RETENTION. ResolveDispute marked the job `+resolved` and left the
// retention, the cut and the pending right in the module for good.
func TestEpoch14_GovernanceResolutionSettlesTheRetention(t *testing.T) {
	for _, upheld := range []bool{true, false} {
		f := initFixture(t)
		r := e14FinalSetup(t, f, true, true)
		r.job.State = "open+paid+optimistic+disputed"
		require.NoError(t, f.keeper.Job.Set(f.ctx, "jFin", r.job))

		_, err := keeper.NewMsgServerImpl(f.keeper).ResolveDispute(f.ctx, &types.MsgResolveDispute{Authority: f.authority, JobId: "jFin", Upheld: upheld})
		require.NoError(t, err)
		e14NoneRetained(t, f, "jFin")
		_, hErr := f.keeper.HeldFee.Get(f.ctx, "jFin")
		require.Error(t, hErr, "upheld=%v: the retention leaves the module either way", upheld)
		if upheld {
			require.Equal(t, int64(100000), e14Udndr(f, r.client), "upheld: the optimistic payment was wrong, the client is refunded")
			require.True(t, f.bank.mod[authtypes.FeeCollectorName].IsZero(), "and nothing was paid out to stakers")
		} else {
			require.Equal(t, int64(80000), e14Udndr(f, r.op), "rejected: the payment stands, the retention is released")
			require.Equal(t, "7500udndr", f.bank.mod[authtypes.FeeCollectorName].String(), "and the cut is paid")
		}
	}
}

// (4) A JOB THE LOTTERY SELECTED IS NOT RELEASED BY A HUMAN DISPUTE. Found by the liveness review: below
// the jury floor the selected job carries no audit committee, and the "never summoned" branches released
// its retention to the primary at the dispute deadline — a primary could dispute its own job and be paid
// unverified. The control (not selected) keeps the epoch-13 outcome, so the case measures the selection.
//
// The same hole had a second door, found by the adversarial review of the relaunch: a job whose lottery
// has NOT RUN YET — deferred for want of a seed, so neither selected nor cleared — is not "never
// summoned" either. Disputed during a seed outage, it was released at the deadline. `pending` measures it.
func TestEpoch14_ASelectedJobIsNotReleasedByAHumanDispute(t *testing.T) {
	for _, c := range []string{"selected", "pending", "control"} {
		selected := c != "control"
		f := initFixture(t)
		p := types.DefaultParams()
		p.VerificationMode = 1
		p.HoldBps = 10000
		p.DisputeWindow = 10
		p.AuditResolveTimeout = 120
		p.DisputeBond = 1000
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		opAcc := sdk.AccAddress([]byte("operator_e14_selfdsp"))
		op, _ := f.addressCodec.BytesToString(opAcc)
		f.bank.setBalance(opAcc, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(1000))))
		require.NoError(t, gvSetMiner(f, f.ctx, "mSel", types.Miner{MinerId: "mSel", Operator: op, Stake: 1000}))
		require.NoError(t, gvSetJob(f, f.ctx, "jSel", types.Job{JobId: "jSel", State: "open+paid+optimistic", MinerId: "mSel", Fee: 100000}))
		require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jSel", 80000))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jSel", 900))
		const h = int64(1000)
		switch c {
		case "selected":
			require.NoError(t, f.keeper.AuditSelected.Set(f.ctx, "jSel")) // drawn, deferred for a small jury
		case "pending":
			// The lottery has not run: the entry was pushed past the dispute deadline by a missing seed.
			require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(h+int64(p.AuditResolveTimeout)+1000, "jSel")))
		}
		ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h)
		_, err := keeper.NewMsgServerImpl(f.keeper).DisputeVerdict(ctx, &types.MsgDisputeVerdict{Creator: op, JobId: "jSel"})
		require.NoError(t, err, "the primary disputes its own job")
		end := h + int64(p.AuditResolveTimeout)
		require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(end).WithHeaderInfo(header.Info{Height: end, AppHash: []byte("ah")})))

		held, hErr := f.keeper.HeldFee.Get(f.ctx, "jSel")
		if selected {
			require.NoError(t, hErr, "selected: the retention stays HELD — no verdict, no release")
			require.Equal(t, uint64(80000), held)
			require.Equal(t, int64(0), f.bank.balOf(opAcc).AmountOf("udndr").Int64(), "the self-dispute bought nothing")
			job, _ := f.keeper.Job.Get(f.ctx, "jSel")
			require.NotContains(t, job.State, "resolved", "deferred like any audit without a quorum")
		} else {
			require.Error(t, hErr, "control: a never-selected job is released at the deadline, as before")
			require.Equal(t, int64(80000), f.bank.balOf(opAcc).AmountOf("udndr").Int64())
		}
	}
}

// (5) THE MISSING-SEED DEFERRAL DROPS A JOB THAT IS ALREADY RESOLVED instead of unwinding it: the
// retention of a vindicated job waiting for its absent primary belongs to that primary.
func TestEpoch14_NoSeedDeferralDropsAResolvedJob(t *testing.T) {
	f := initFixture(t)
	clientAcc, ctx := e14NoSeedSetup(t, f, 500)
	job, _ := f.keeper.Job.Get(f.ctx, "jNS")
	job.State = "open+paid+optimistic+disputed+resolved+vindicated+quorum"
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jNS", job))
	require.NoError(t, f.keeper.EndBlock(ctx))

	require.Empty(t, e14PendingAuditOf(t, f, "jNS"), "nothing left to audit: the entry is dropped, not re-deferred for ever")
	// And the event does not count the dropped entry as a deferred job.
	var jobsAttr, droppedAttr string
	for _, ev := range sdk.UnwrapSDKContext(ctx).EventManager().Events() {
		if ev.Type != "audit_draw_deferred" {
			continue
		}
		for _, a := range ev.Attributes {
			switch a.Key {
			case "jobs":
				jobsAttr = a.Value
			case "dropped":
				droppedAttr = a.Value
			}
		}
	}
	require.Equal(t, "0", jobsAttr, "a dropped entry is not a deferred job")
	require.Equal(t, "1", droppedAttr, "it is counted as dropped")
	held, err := f.keeper.HeldFee.Get(f.ctx, "jNS")
	require.NoError(t, err, "and the retention owed to the vindicated miner is NOT refunded to the client")
	require.Equal(t, uint64(80000), held)
	require.Equal(t, int64(0), e14Udndr(f, clientAcc))
}

// (6) PAYOUT IS REFUSED IN OPTIMISTIC MODE. Found by the parameter review: at `work_committee_size = 1`
// the primary's lone commit was a "complete committee", and anyone could pay it the whole fee through
// the redundant path — no retention, no audit, no cut. The control is the same call in mode 0.
func TestEpoch14_PayoutIsRefusedInOptimisticMode(t *testing.T) {
	for _, mode := range []uint64{1, 0} {
		f := initFixture(t)
		e14DrawSetup(t, f, 0)
		p, _ := f.keeper.Params.Get(f.ctx)
		p.VerificationMode = mode
		p.WorkCommitteeSize = 1
		require.NoError(t, f.keeper.Params.Set(f.ctx, p))
		aeFundModule(f)
		job, _ := f.keeper.Job.Get(f.ctx, "jDraw")
		job.Fee = 1000
		require.NoError(t, f.keeper.Job.Set(f.ctx, "jDraw", job))
		ordered, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jDraw", 1)
		require.NoError(t, err)
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jDraw__"+ordered[0], types.Commit{ResultCommit: "1,0,0"}))

		_, err = keeper.NewMsgServerImpl(f.keeper).Payout(f.ctx, &types.MsgPayout{Creator: ssAddr20(t, f, "anyone"), JobId: "jDraw"})
		got, _ := f.keeper.Job.Get(f.ctx, "jDraw")
		if mode == 1 {
			require.Error(t, err, "optimistic mode: payout would bypass the retention, the audit and the cut")
			require.NotContains(t, got.State, "paid")
		} else {
			require.NoError(t, err, "control: redundant mode still settles through payout")
		}
	}
}

// (7) A TEAM ADDRESS OF ANOTHER CHAIN IS REFUSED WHERE PARAMS ENTER THE STORE. Validate is prefix-agnostic
// by design; the keeper knows the prefix. Accepted, it would fail every finality and grow the retry queue.
func TestEpoch14_AForeignTeamAddressIsRefusedByGovernance(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	foreign, err := bech32ConvertForTest("osmo", make([]byte, 20))
	require.NoError(t, err)
	p := types.DefaultParams()
	p.TeamAddress = foreign
	require.NoError(t, p.Validate(), "premise: the prefix-agnostic Validate accepts it")
	_, err = srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
	require.Error(t, err, "the keeper refuses an address this chain cannot pay")

	// A MODULE ACCOUNT OF THIS CHAIN is the same trap one step closer: the prefix is right, and the bank
	// refuses to credit it (fee_collector, distribution, the bonded pools...), so every finality queues.
	for _, name := range []string{authtypes.FeeCollectorName, "distribution", "bonded_tokens_pool", "jobs", "nft", "transfer", "interchainaccounts"} {
		mod, _ := f.addressCodec.BytesToString(authtypes.NewModuleAddress(name))
		p.TeamAddress = mod
		_, err = srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
		require.Error(t, err, "the module account %q is refused as team_address", name)
	}

	own, _ := f.addressCodec.BytesToString(make([]byte, 20))
	p.TeamAddress = own
	_, err = srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
	require.NoError(t, err, "control: an address of this chain is accepted")
}

func bech32ConvertForTest(hrp string, b []byte) (string, error) {
	return bech32.ConvertAndEncode(hrp, b)
}
