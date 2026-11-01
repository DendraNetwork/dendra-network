package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Epoch 15 leaves the deferral and the unwind unchanged. These cases pin the unwind as its two
// deferral sites reach it (the jury floor and the committee below the bar) and the order of its checks,
// each with a twin that keeps the deferral, so a function that always unwound would fail too.

// d8H is the height these cases run at: off the 100-block prune cadence of EndBlock, and far enough from
// a HeldSince of 1 that an unwind bound of 500 blocks finds that debt old.
const d8H = int64(100001)

func d8Ctx(f *fixture, h int64) sdk.Context {
	return sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).
		WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah-d8")}).
		WithEventManager(sdk.NewEventManager())
}

// d8Account returns an account that starts at zero, so a balance read afterwards is exactly what moved.
func d8Account(t *testing.T, f *fixture, seed string) (sdk.AccAddress, string) {
	t.Helper()
	acc := sdk.AccAddress([]byte(seed))
	s, err := f.addressCodec.BytesToString(acc)
	require.NoError(t, err)
	f.bank.setBalance(acc, sdk.NewCoins())
	return acc, s
}

// d8Params is the optimistic configuration of these cases, from the mode 1 set Validate accepts: a
// certain lottery, a full hold, and a resolve timeout beside an unwind bound (0 keeps it dormant).
func d8Params(t *testing.T, unwind uint64) types.Params {
	t.Helper()
	p := qzOptimisticParams(t)
	p.AuditSampleBps = 10000
	p.HoldBps = 10000
	p.AuditResolveTimeout = 240
	p.AuditUnwindBlocks = unwind
	require.NoError(t, p.Validate(), "premise: the configuration validates")
	return p
}

// d8HeightsOf lists the heights at which a job is filed in one of the deadline queues.
func d8HeightsOf(t *testing.T, f *fixture, set collections.KeySet[collections.Pair[int64, string]], jobId string) []int64 {
	t.Helper()
	var hs []int64
	require.NoError(t, set.Walk(f.ctx, nil, func(k collections.Pair[int64, string]) (bool, error) {
		if k.K2() == jobId {
			hs = append(hs, k.K1())
		}
		return false, nil
	}))
	return hs
}

// Below the jury floor the audit defers, and once its debt has reached audit_unwind_blocks the floor
// deferral itself unwinds it: the client is refunded, the miner is neither paid nor slashed. Driven
// through EndBlock so that the floor path makes the call, not the test seam.
func TestADR059Decision8_TheJuryFloorDeferralUnwindsADebtPastTheBound(t *testing.T) {
	for _, c := range []struct {
		name    string
		since   uint64
		unwound bool
	}{{"old debt", 1, true}, {"young debt", uint64(d8H) - 10, false}} {
		t.Run(c.name, func(t *testing.T) {
			f := initFixture(t)
			require.NoError(t, f.keeper.Params.Set(f.ctx, d8Params(t, 500)))
			aeFundModule(f)
			clientAcc, client := d8Account(t, f, "client_d8_floor_____")
			opAcc, op := d8Account(t, f, "operator_d8_floor___")
			// The primary is the only miner, so the draw seats nobody: a jury below the floor.
			require.NoError(t, gvSetMiner(f, f.ctx, "mFloor", types.Miner{MinerId: "mFloor", Operator: op, Stake: aeStake}))
			require.NoError(t, gvSetJob(f, f.ctx, "jFloor", types.Job{
				JobId: "jFloor", State: "open+paid+optimistic", MinerId: "mFloor", Client: client, Fee: 100000,
			}))
			require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jFloor", 80000))
			require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jFloor", c.since))
			require.NoError(t, f.keeper.PendingAudit.Set(f.ctx, collections.Join(d8H, "jFloor")))

			require.NoError(t, f.keeper.EndBlock(d8Ctx(f, d8H)))

			job, err := f.keeper.Job.Get(f.ctx, "jFloor")
			require.NoError(t, err)
			require.NotContains(t, job.State, "disputed", "below the floor no audit opens")
			m, _ := f.keeper.Miner.Get(f.ctx, "mFloor")
			require.Equal(t, aeStake, m.Stake, "the miner is never slashed for a missing jury")
			require.Equal(t, int64(0), e14Udndr(f, opAcc), "and never paid for an audit that did not take place")
			_, hErr := f.keeper.HeldFee.Get(f.ctx, "jFloor")
			selected, sErr := f.keeper.AuditSelected.Has(f.ctx, "jFloor")
			require.NoError(t, sErr)
			if !c.unwound {
				require.NoError(t, hErr, "a young debt stays held")
				require.NotContains(t, job.State, "resolved")
				require.Equal(t, int64(0), e14Udndr(f, clientAcc))
				require.Equal(t, []int64{d8H + keeper.AuditDeferStrideForTest}, e14PendingAuditOf(t, f, "jFloor"),
					"a young debt is deferred by the stride")
				require.True(t, selected, "and stays selected, so the retry does not replay the lottery")
				return
			}
			require.Error(t, hErr, "past the bound the retention is consumed")
			require.Contains(t, job.State, "resolved")
			require.Contains(t, job.State, "unwound")
			require.NotContains(t, job.State, "clawed", "no sanction: no committee ruled")
			require.Equal(t, int64(80000), e14Udndr(f, clientAcc), "the client gets the retention back")
			require.Empty(t, e14PendingAuditOf(t, f, "jFloor"), "settled, not postponed: no pending audit entry survives")
			require.False(t, selected, "the selection marker leaves with the debt")
		})
	}
}

// The second deferral site: an anchored committee below the bar at the deadline. Past
// audit_unwind_blocks the job is unwound there, with nothing rescheduled; a young debt defers.
func TestADR059Decision8_ACommitteeBelowTheBarUnwindsADebtPastTheBound(t *testing.T) {
	for _, c := range []struct {
		name    string
		since   uint64
		unwound bool
	}{{"old debt", 1, true}, {"young debt", uint64(d8H) - 10, false}} {
		t.Run(c.name, func(t *testing.T) {
			f := initFixture(t)
			p := d8Params(t, 500)
			require.NoError(t, f.keeper.Params.Set(f.ctx, p))
			aeFundModule(f)
			clientAcc, client := d8Account(t, f, "client_d8_bar_______")
			opAcc, op := d8Account(t, f, "operator_d8_bar_____")
			require.NoError(t, gvSetMiner(f, f.ctx, "mBar", types.Miner{MinerId: "mBar", Operator: op, Stake: aeStake}))
			for _, id := range []string{"mJ1", "mJ2", "mJ3", "mJ4"} {
				aeReg(f, t, id, aeStake)
			}
			aeAnchor(f, t, "jBar", "mJ1", "mJ2", "mJ3", "mJ4")
			aeVerdict(f, t, "jBar", "mJ1", "1") // one vote of four: below the bar
			require.NoError(t, gvSetJob(f, f.ctx, "jBar", types.Job{
				JobId: "jBar", State: "open+paid+optimistic+disputed", MinerId: "mBar", Client: client, Fee: 100000,
				Disputer: f.authority,
			}))
			require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jBar", 80000))
			require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jBar", c.since))
			require.NoError(t, f.keeper.PendingAuditResolve.Set(f.ctx, collections.Join(d8H, "jBar")))

			require.NoError(t, f.keeper.EndBlock(d8Ctx(f, d8H)))

			job, err := f.keeper.Job.Get(f.ctx, "jBar")
			require.NoError(t, err)
			m, _ := f.keeper.Miner.Get(f.ctx, "mBar")
			require.Equal(t, aeStake, m.Stake, "below the bar nobody is slashed")
			require.Equal(t, int64(0), e14Udndr(f, opAcc), "and the miner is not paid")
			require.Empty(t, d8HeightsOf(t, f, f.keeper.PendingAppealResolve, "jBar"), "appeal_window is 0: no appeal")
			_, hErr := f.keeper.HeldFee.Get(f.ctx, "jBar")
			_, cErr := f.keeper.AuditCommittee.Get(f.ctx, "jBar")
			if !c.unwound {
				require.NoError(t, hErr, "a young debt stays held")
				require.NotContains(t, job.State, "resolved")
				require.Equal(t, int64(0), e14Udndr(f, clientAcc))
				require.Equal(t, []int64{d8H + int64(p.AuditResolveTimeout)}, d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jBar"),
					"a young debt is deferred to the next deadline")
				require.NoError(t, cErr, "and the jury stays summoned")
				return
			}
			require.Error(t, hErr, "past the bound the retention is consumed")
			require.Contains(t, job.State, "resolved")
			require.Contains(t, job.State, "unwound")
			require.NotContains(t, job.State, "clawed")
			require.NotContains(t, job.State, "vindicated")
			require.Equal(t, int64(80000), e14Udndr(f, clientAcc), "the client gets the retention back")
			require.Empty(t, d8HeightsOf(t, f, f.keeper.PendingAuditResolve, "jBar"), "an unwound audit has no next deadline")
			require.Error(t, cErr, "the jurors are released")
		})
	}
}

// The bound is an age in blocks: one block short defers (TestUnwindArmedButTooYoungDefersAsBefore), and
// a debt exactly audit_unwind_blocks old has reached it and unwinds.
func TestADR059Decision8_ADebtExactlyAuditUnwindBlocksOldIsUnwound(t *testing.T) {
	f := initFixture(t)
	unwSeed(t, f, 1000, 0)

	require.True(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 1500, unwJob, unwParams(500)),
		"an age of 500 against a bound of 500 has reached the bound")
	_, err := f.keeper.HeldFee.Get(f.ctx, unwJob)
	require.Error(t, err, "the retention is consumed")
	job, _ := f.keeper.Job.Get(f.ctx, unwJob)
	require.Contains(t, job.State, "unwound")
}

// The job check comes before any mutation: a retention whose job record is missing names no client
// and no state to mark, so it is refused and nothing is written under that id.
func TestADR059Decision8_ARetentionWithoutItsJobIsNotUnwound(t *testing.T) {
	f := initFixture(t)
	aeFundModule(f)
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, "jOrphan", 80000))
	require.NoError(t, f.keeper.HeldSince.Set(f.ctx, "jOrphan", 1))

	require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, "jOrphan", unwParams(500)),
		"without the job there is nothing to mark and no client to refund")
	held, err := f.keeper.HeldFee.Get(f.ctx, "jOrphan")
	require.NoError(t, err)
	require.Equal(t, uint64(80000), held, "the retention stays on the books")
	has, err := f.keeper.Job.Has(f.ctx, "jOrphan")
	require.NoError(t, err)
	require.False(t, has, "no job record is invented to carry an unwound state")
}

// No retention means nothing to unwind, and a retention read as 0 is no retention: absent and stored
// at 0 must give the same refusal, and the audit stays open.
func TestADR059Decision8_ARetentionOfZeroIsNoDebtToUnwind(t *testing.T) {
	for _, stored := range []bool{false, true} {
		f := initFixture(t)
		aeFundModule(f)
		require.NoError(t, gvSetJob(f, f.ctx, unwJob, types.Job{
			JobId: unwJob, State: "open+paid+optimistic+disputed", MinerId: unwMiner, Fee: 100000,
		}))
		require.NoError(t, gvSetMiner(f, f.ctx, unwMiner, types.Miner{MinerId: unwMiner, Stake: 1000000}))
		require.NoError(t, f.keeper.HeldSince.Set(f.ctx, unwJob, 1))
		if stored {
			require.NoError(t, f.keeper.HeldFee.Set(f.ctx, unwJob, 0))
		}

		require.False(t, f.keeper.UnwindStrandedRetentionForTest(f.ctx, 100001, unwJob, unwParams(500)),
			"stored=%v: a zero retention is no debt", stored)
		job, _ := f.keeper.Job.Get(f.ctx, unwJob)
		require.NotContains(t, job.State, "resolved", "stored=%v: the audit is not closed", stored)
	}
}
