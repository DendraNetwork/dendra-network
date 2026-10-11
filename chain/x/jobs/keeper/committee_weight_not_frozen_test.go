package keeper_test

import (
	"fmt"
	"strings"
	"testing"

	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ADR-037 froze who may be drawn and when vitality is evaluated, but not the weight.
//
// `assignedCommittee` computes the draw again on every read, and `committee.go` explains why that is
// dangerous for the vitality predicate: "a predicate that moved with the clock would silently reassign
// the primary of an already-settled job, so `assigned_committee` would stop agreeing with what was
// paid". So the predicate's argument was pinned to the freeze height, and membership was pinned too
// (`MinerRegisteredHeight <= JobPoolFreezeHeight`).
//
// The third input was not. The draw orders miners by `h/stake`, and `stake` was read live from the
// miner record: a slash on some other job lowered that miner's stake, its score changed, and the
// ordering of every open job changed with it. Measured on eight miners with distinct stakes below the
// assignment ceiling: the committee of an open job went from [cm2 cm1 cm5] to [cm2 cm5 cm6] after cm1,
// a member, was slashed for something else. A miner unrelated to the job walked into it, and a member
// walked out.
//
// The consequence lands on money, through eight call sites that all derive the committee again:
// settle-pay, settle-semantic, payout, finalize-job, adjudicate, job-expiry and the vitality pass. A
// miner that committed in good faith stops being "assigned", so its answer is discarded and it is not
// paid; the newcomer never committed, so the committee reads incomplete and settlement refuses, leaving
// the escrow in the module with no path out.
//
// ADR-045 (12) closes it by anchoring the work committee the first time it is derivable, as the audit
// committee already was (`AuditCommittee`). The cases below check that an anchored committee no longer
// moves.

const cwFreeze = 400

func cwMiners(t *testing.T, f *fixture, n int) []string {
	t.Helper()
	ids := []string{}
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("cw%d", i)
		ids = append(ids, id)
		op := ssAddr20(t, f, "cw-op-"+id)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: op, Operator: op,
			// Distinct and below the assignment ceiling, so the cap does not flatten the differences
			// this case exists to move.
			Stake: uint64(1000 + i*137),
		}))
	}
	return ids
}

// (a) Slashing a member for something else leaves an anchored committee alone, without a re-draw.
func TestSlashingAMemberElsewhereLeavesAnANCHOREDCommitteeAlone(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	cwMiners(t, f, 8)
	require.NoError(t, gvSetJob(f, f.ctx, "jCW", types.Job{JobId: "jCW", State: "open", Fee: 10000}))
	// The anchoring, driven through the shipped helper: on chain it lands at `OpenJob` or at the reveal;
	// here the fixture builds the job, so it calls the same helper at the same logical moment.
	f.keeper.AnchorWorkCommitteeForTest(f.ctx, "jCW")

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCW", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, before, keeper.CommitteeSize, "premise: a full committee is drawn")

	// The slash happens elsewhere: no field of this job is touched, and neither is the frozen pool
	// anchor or any registration height. Only the miner's own bond moves.
	member := before[1]
	m, err := f.keeper.Miner.Get(f.ctx, member)
	require.NoError(t, err)
	m.Stake = 1
	require.NoError(t, gvSetMiner(f, f.ctx, member, m))

	after, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCW", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Equal(t, before, after,
		"an anchored committee does not move when a member's stake changes elsewhere: %v", before)
	require.Contains(t, after, member,
		"and the slashed member stays assigned to the job it may already have answered")

	// And nobody who was never assigned walks in, the half that would strand the escrow, because such a
	// miner has no commit and the committee would then read incomplete.
	for _, id := range after {
		require.Contains(t, before, id,
			"no stranger invites itself: %q had not been drawn for this job", id)
	}
}

// (b) The control: slashing a non-member changes nothing. What could move the draw is the weight of a
// drawn miner, not any write to any miner; a fixture that drew again on every state change would prove
// nothing about this case.
func TestSlashingANonMemberLeavesTheCommitteeAlone(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	ids := cwMiners(t, f, 8)
	require.NoError(t, gvSetJob(f, f.ctx, "jCWb", types.Job{JobId: "jCWb", State: "open", Fee: 10000}))

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWb", keeper.CommitteeSize)
	require.NoError(t, err)

	stranger := ""
	for _, id := range ids {
		if id != before[0] && id != before[1] && id != before[2] {
			stranger = id
			break
		}
	}
	require.NotEmpty(t, stranger)
	m, err := f.keeper.Miner.Get(f.ctx, stranger)
	require.NoError(t, err)
	m.Stake = 1 // slashed to nothing, and it was already outside
	require.NoError(t, gvSetMiner(f, f.ctx, stranger, m))

	after, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWb", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Equal(t, before, after,
		"lowering the weight of a miner already outside the top seats moves nothing")
}

// (c) The same case read on the money: the miners that committed stay assigned after a member is
// slashed elsewhere, so their answers count and settle-pay goes through, instead of refusing an
// incomplete committee and leaving the job open with no settlement path.
func TestAnANCHOREDCommitteeStillSettlesAfterAMemberIsSlashedElsewhere(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	cwMiners(t, f, 8)

	client := ssAddr20(t, f, "cw-client")
	// Fee 0: since ADR-045 (9) `SettlePay` refuses a job that still holds its escrow, and a refusal for
	// that reason would prove the wrong fact. The subject here is the committee.
	require.NoError(t, gvSetJob(f, f.ctx, "jCWc", types.Job{
		JobId: "jCWc", State: "open", Fee: 0, Client: client,
	}))
	f.keeper.AnchorWorkCommitteeForTest(f.ctx, "jCWc")
	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWc", keeper.CommitteeSize)
	require.NoError(t, err)

	for _, id := range before {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jCWc__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	// The ordinary event that must not break anything: a member is slashed for an unrelated job.
	m, err := f.keeper.Miner.Get(f.ctx, before[1])
	require.NoError(t, err)
	m.Stake = 1
	require.NoError(t, gvSetMiner(f, f.ctx, before[1], m))

	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jCWc", Amount: 3000})
	require.NoError(t, err,
		"the committee that answered is still the one being asked: the settlement goes through")

	job, err := f.keeper.Job.Get(f.ctx, "jCWc")
	require.NoError(t, err)
	require.Contains(t, job.State, "settled", "and the job closes instead of staying open with no way out")
}

// The two hooks, exercised through the real path.
//
// The cases above call the anchoring helper directly: they prove the read path and the anchor's
// contents, and would say nothing if nobody called that helper any more. Testing a rule is not testing
// its invocation.
//
// There are two anchoring moments because the seed arrives at two moments: immediately when
// `committee_reveal_delay = 0`, deferred otherwise. The live chain runs with a delay of 1, so the
// second hook is the production one, and testing only the first would leave it unguarded.

func TestOpenJobAnchorsTheWorkCommitteeWhenTheSeedIsIMMEDIATE(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	p := types.DefaultParams()
	require.Zero(t, p.CommitteeRevealDelay, "premise: the default has no reveal delay")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// Real staked miners, registered before the freeze: the seeding pool carries zero stake, hence zero
	// weight, hence no draw. A test that forgets them measures an empty committee, not an anchoring.
	for i := 0; i < 6; i++ {
		qzMiner(t, f, fmt.Sprintf("waiM%d", i), 5, p.MinStake)
	}

	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("waImmediate_________________"))
	require.NoError(t, err)
	ctx10 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(10)
	_, err = srv.OpenJob(ctx10, &types.MsgOpenJob{Creator: creator, JobId: "jWAi", Fee: 10})
	require.NoError(t, err)

	raw, gErr := f.keeper.WorkCommitteeRawForTest(ctx10, "jWAi")
	require.NoError(t, gErr, "OpenJob must write the anchor: the seed is there, so the committee exists")
	require.NotEmpty(t, raw)
	require.Equal(t, keeper.CommitteeSize, len(strings.Split(raw, ",")),
		"and it carries the whole committee, ordered, primary first")
}

func TestTheDEFERREDRevealAnchorsTheWorkCommitteeAtItsOwnMoment(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	p := types.DefaultParams()
	p.CommitteeRevealDelay = 2
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// Real staked miners, registered before the freeze: the seeding pool carries zero stake, hence zero
	// weight, hence no draw. A test that forgets them measures an empty committee, not an anchoring.
	for i := 0; i < 6; i++ {
		qzMiner(t, f, fmt.Sprintf("wadM%d", i), 5, p.MinStake)
	}

	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("waDeferred__________________"))
	require.NoError(t, err)
	ctx10 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(10)
	_, err = srv.OpenJob(ctx10, &types.MsgOpenJob{Creator: creator, JobId: "jWAd", Fee: 10})
	require.NoError(t, err)

	// Nothing at open time, on purpose: anchoring here would amount to inventing a committee the seed
	// does not allow drawing yet.
	_, gErr := f.keeper.WorkCommitteeRawForTest(ctx10, "jWAd")
	require.Error(t, gErr, "no anchor while the seed is unrevealed")

	ctx12 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(12).
		WithHeaderInfo(header.Info{Height: 12, AppHash: []byte("apphash-12")})
	require.NoError(t, f.keeper.EndBlock(ctx12))

	raw, gErr := f.keeper.WorkCommitteeRawForTest(ctx12, "jWAd")
	require.NoError(t, gErr, "the EndBlocker that reveals the seed must anchor in the same gesture")
	require.NotEmpty(t, raw)
}
