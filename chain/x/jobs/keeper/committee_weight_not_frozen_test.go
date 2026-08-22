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

// ADR-037 FROZE WHO MAY BE DRAWN, AND WHEN VITALITY IS EVALUATED. NOBODY FROZE THE WEIGHT.
//
// `assignedCommittee` recomputes the draw on every read, and `committee.go` says why that is dangerous
// in its own words, about the vitality predicate: "a predicate that moved with the clock would silently
// reassign the primary of an already-settled job, so `assigned_committee` would stop agreeing with what
// was paid". So the predicate's ARGUMENT was pinned to the freeze height. Membership was pinned too
// (`MinerRegisteredHeight <= JobPoolFreezeHeight`).
//
// The third input was not. The draw orders miners by `h/stake`, and `stake` is read LIVE from the miner
// record. A slash on some OTHER job lowers that miner's stake, its score changes, and the ordering of
// every open job changes with it.
//
// MEASURED, on eight miners with distinct stakes below the assignment ceiling: the committee of an open
// job went from [cm2 cm1 cm5] to [cm2 cm5 cm6] after cm1 — a MEMBER — was slashed for something else.
// A miner that never had anything to do with this job walked into it, and a member walked out.
//
// The consequence lands on money, through eight call sites that all re-derive: settle-pay,
// settle-semantic, payout, finalize-job, adjudicate, job-expiry and the vitality pass. A miner that
// committed in good faith stops being "assigned", so its answer is discarded and it is not paid; and
// the newcomer never committed, so the committee reads INCOMPLETE and settlement refuses — the escrow
// stays in the module with no path out.
//
// ⛔ THIS PINS A DEFECT, NOT A RULE, AND THE FIX IS AN OWNER'S CALL because every shape costs state:
//   - snapshot each drawn miner's weight at the freeze height (new per-job state, a genesis field, a
//     migration) — faithful, and the heaviest;
//   - anchor the work committee at OpenJob the way the AUDIT committee is already anchored
//     (`AuditCommittee`), which reuses an existing mechanism but changes when the draw happens;
//   - draw on a value that cannot move (registration-time stake, or the floor), which drops the
//     economic weighting the cap was built to bound.
// Whichever lands, case (a) must flip.

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
			// Distinct and BELOW the assignment ceiling, so the cap does not flatten the differences
			// this case exists to move.
			Stake: uint64(1000 + i*137),
		}))
	}
	return ids
}

// (a) THE DEFECT: slashing a MEMBER for something else re-draws an open job's committee.
func TestSlashingAMemberElsewhereLeavesAnANCHOREDCommitteeAlone(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	cwMiners(t, f, 8)
	require.NoError(t, gvSetJob(f, f.ctx, "jCW", types.Job{JobId: "jCW", State: "open", Fee: 10000}))
	// THE ANCHORING, driven through the SHIPPED helper: on chain it lands at `OpenJob` or at the reveal;
	// here the fixture builds the job, so it calls the same helper at the same logical moment.
	f.keeper.AnchorWorkCommitteeForTest(f.ctx, "jCW")

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCW", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, before, keeper.CommitteeSize, "premise: a full committee is drawn")

	// The slash happens ELSEWHERE — no field of this job is touched, and neither is the frozen pool
	// anchor or any registration height. Only the miner's own bond moves.
	member := before[1]
	m, err := f.keeper.Miner.Get(f.ctx, member)
	require.NoError(t, err)
	m.Stake = 1
	require.NoError(t, gvSetMiner(f, f.ctx, member, m))

	after, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCW", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Equal(t, before, after,
		"an ANCHORED committee does not move when a member's stake changes elsewhere: %v", before)
	require.Contains(t, after, member,
		"and the slashed member stays assigned to the job it may already have answered")

	// And someone who was never assigned walks in — the half that strands the escrow, because that
	// miner has no commit and the committee then reads incomplete.
	for _, id := range after {
		require.Contains(t, before, id,
			"no stranger invites itself: %q had not been drawn for this job", id)
	}
}

// (b) THE CONTROL, and the reason (a) means anything: slashing a NON-member changes nothing. So what
// moves the draw is the weight of a DRAWN miner, not any write to any miner — a fixture that re-drew on
// every state change would prove nothing about this defect.
func TestSlashingANonMemberLeavesTheCommitteeAlone(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	ids := cwMiners(t, f, 8)
	require.NoError(t, gvSetJob(f, f.ctx, "jCWb", types.Job{JobId: "jCWb", State: "open", Fee: 10000}))

	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWb", keeper.CommitteeSize)
	require.NoError(t, err)

	etranger := ""
	for _, id := range ids {
		if id != before[0] && id != before[1] && id != before[2] {
			etranger = id
			break
		}
	}
	require.NotEmpty(t, etranger)
	m, err := f.keeper.Miner.Get(f.ctx, etranger)
	require.NoError(t, err)
	m.Stake = 1 // slashed to nothing, and it was already outside
	require.NoError(t, gvSetMiner(f, f.ctx, etranger, m))

	after, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWb", keeper.CommitteeSize)
	require.NoError(t, err)
	require.Equal(t, before, after,
		"lowering the weight of a miner already outside the top seats moves nothing")
}

// (c) WHAT IT COSTS, read on the money rather than on the draw: the miner that committed is no longer
// assigned, so its answer is discarded and settle-pay refuses for an incomplete committee. The client's
// wallet is untouched — and so is the job, which keeps its escrow with no settlement path.
func TestAnANCHOREDCommitteeStillSettlesAfterAMemberIsSlashedElsewhere(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	cwMiners(t, f, 8)

	client := ssAddr20(t, f, "cw-client")
	// Fee 0: since ADR-045 (9) `SettlePay` refuses a job that still holds its escrow, and a refusal
	// for THAT reason would prove the wrong fact. The subject here is the COMMITTEE.
	require.NoError(t, gvSetJob(f, f.ctx, "jCWc", types.Job{
		JobId: "jCWc", State: "open", Fee: 0, Client: client,
	}))
	f.keeper.AnchorWorkCommitteeForTest(f.ctx, "jCWc")
	before, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, "jCWc", keeper.CommitteeSize)
	require.NoError(t, err)

	for _, id := range before {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jCWc__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	// The ordinary event that broke everything: a member is slashed for an UNRELATED job.
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

// ══ LES DEUX CROCHETS, JOUES PAR LE CHEMIN REEL ═══════════════════════════════════════════════════
//
// The cases above call the anchoring helper directly: they prove the READ path and the anchor's
// contents, and would say NOTHING if nobody called that helper any more. That is the difference
// between testing a rule and testing its INVOCATION, and this repository has paid for it often enough.
//
// There are TWO anchoring moments because the seed arrives at two moments: immediate when
// `committee_reveal_delay = 0`, deferred otherwise. The live chain runs with a delay of 1, so the
// second hook is production's -- testing only the first would leave the interesting half unguarded.
// interesting half unguarded.

func TestOpenJobAnchorsTheWorkCommitteeWhenTheSeedIsIMMEDIATE(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	p := types.DefaultParams()
	require.Zero(t, p.CommitteeRevealDelay, "premise: the default has no reveal delay")
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	// REAL staked miners, registered BEFORE the freeze: the seeding pool carries zero stake, hence zero
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
	require.NoError(t, gErr, "OpenJob must WRITE the anchor: the seed is there, so the committee exists")
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
	// REAL staked miners, registered BEFORE the freeze: the seeding pool carries zero stake, hence zero
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

	// NOTHING at open time, and that is the point: anchoring here would amount to inventing a committee
	// the seed does not allow drawing yet.
	_, gErr := f.keeper.WorkCommitteeRawForTest(ctx10, "jWAd")
	require.Error(t, gErr, "no anchor while the seed is unrevealed")

	ctx12 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(12).
		WithHeaderInfo(header.Info{Height: 12, AppHash: []byte("apphash-12")})
	require.NoError(t, f.keeper.EndBlock(ctx12))

	raw, gErr := f.keeper.WorkCommitteeRawForTest(ctx12, "jWAd")
	require.NoError(t, gErr, "the EndBlocker that reveals the seed must anchor in the same gesture")
	require.NotEmpty(t, raw)
}
