package keeper_test

import (
	"fmt"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// In optimistic mode at audit_min_quorum = 0, admission must apply the same jury floor of four as the
// draw.
//
// `enoughEligibleMinersToVerify` is the precondition that refuses a job whose frozen pool could never
// supply a jury. `OpenJob` is its only caller, and its own comment explains why it must be a refusal
// and not a warning: the pool freezes at the job's birth and never widens, so an
// under-supplied job can never be verified, and `deferAuditForSmallJury` queues it again without bound.
// On a chain where this happened, 42 of 51 jobs were stuck in that state, holding 272 406 udndr that no
// verdict, timeout or governance message could release.
//
// A floor built from the raw parameter would return nil at zero, on the grounds that "there is no
// number to compare against here". There is one: `effectiveSlashFloor` yields `audit_min_quorum`
// when set and `auditSlashFloor()` = 4 when it is zero, and it is the function the draw consults before
// deferring a jury it judges too small (audit_sampling.go). `Params.Validate` deliberately keeps zero
// legal ("the explicit fallback, not a silent weakening"), so the fallback is a floor of four, and
// admission must not be the one place that reads zero as "no constraint". Admission asks
// `effectiveSlashFloor`.
//
// The check applies in mode 1 only, which is why these cases set mode 1: `runOptimisticAudit` returns
// immediately when `verification_mode != 1`, so no audit jury is ever drawn there and an audit floor
// has no business gating admission. Making the guard unconditional was measured first: twenty existing
// cases went red, all of them k=3 fixtures never at risk. A fail-closed check applied outside the
// failure it guards stops the network instead of protecting it.

// qzMiner registers a miner directly in state at a given registration height, like the neighbouring
// pool-freeze and assignment-ceiling cases: the draw reads `MinerRegisteredHeight`, not a message.
func qzMiner(t *testing.T, f *fixture, id string, regHeight uint64, stake uint64) {
	t.Helper()
	op := ssAddr20(t, f, "qz-op-"+id)
	require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
		MinerId: id, Creator: op, Operator: op, Stake: stake,
	}))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, regHeight))
}

const qzFreeze = 500

func qzOptimisticParams(t *testing.T) types.Params {
	t.Helper()
	p := types.DefaultParams()
	require.Zero(t, p.AuditMinQuorum, "premise: zero is the default -- if that changes, so must this file")
	p.VerificationMode = 1 // the only mode that draws an audit jury
	p.CommitteeRevealDelay = 1
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.AuditSampleBps = 1000
	p.DisputeBond = p.MinStake // Validate refuses an active dispute window with a free bond
	require.NoError(t, p.Validate(),
		"premise: mode 1 beside a zero quorum is a state governance reaches in one MsgUpdateParams")
	return p
}

func TestQuorumZeroRefusesAJobTheDrawCouldNeverJudge(t *testing.T) {
	f := initFixture(t)
	p := qzOptimisticParams(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	ms := keeper.NewMsgServerImpl(f.keeper)
	client := ssAddr20(t, f, "qz-client")
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))

	// Four eligible miners. At quorum zero the draw's floor is 4, and the jury excludes the miner under
	// audit, so four is one short of what any verdict needs.
	for i := 0; i < 4; i++ {
		qzMiner(t, f, fmt.Sprintf("qzFew%d", i), qzFreeze-10, p.MinStake)
	}

	_, err := ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jQZFew", Fee: 30})
	require.Error(t, err,
		"admission must demand what the draw demands: a zero quorum selects the fallback floor of 4, so four eligible miners are one short")
	require.Contains(t, err.Error(), "5 needed",
		"the refusal has to name the number it applied, or the operator cannot tell which floor refused")
	require.Contains(t, err.Error(), "effective audit floor=4",
		"and it names the effective floor, not the raw parameter: at quorum zero the raw value is 0 and printing it explained the refusal wrongly")

	// Nothing was written. A refusal that still froze the pool or booked the fee would trade an
	// unjudgeable job for an unreachable one.
	_, gErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jQZFew")
	require.Error(t, gErr, "a refused job must not freeze a pool")
	_, jErr := f.keeper.Job.Get(f.ctx, "jQZFew")
	require.Error(t, jErr, "nor leave a job record holding a fee")
}

// The control, which makes the refusal above a floor rather than a blanket refusal: one more eligible
// miner and the same call is accepted. Without it, a guard that refused everything would pass.
func TestQuorumZeroAcceptsAsSoonAsTheFloorIsSupplied(t *testing.T) {
	f := initFixture(t)
	p := qzOptimisticParams(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	ms := keeper.NewMsgServerImpl(f.keeper)
	client := ssAddr20(t, f, "qz-client")
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))

	for i := 0; i < 5; i++ {
		qzMiner(t, f, fmt.Sprintf("qzEnough%d", i), qzFreeze-10, p.MinStake)
	}

	_, err := ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jQZEnough", Fee: 30})
	require.NoError(t, err, "five eligible miners supply the four jurors the draw needs plus the one it excludes")
	_, gErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jQZEnough")
	require.NoError(t, gErr, "and the accepted job freezes its pool as before")
}

// The comparison that showed the defect: the same under-supplied pool, with the quorum governed to the
// floor the zero already selects, is refused as well. One parameter value, the same admission, for a
// draw that behaves identically in both.
func TestTheSamePoolIsRefusedAsSoonAsTheQuorumIsGoverned(t *testing.T) {
	f := initFixture(t)
	p := qzOptimisticParams(t)
	p.AuditMinQuorum = 4 // exactly what `effectiveSlashFloor` returns when the quorum is zero
	require.NoError(t, p.Validate())
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	ms := keeper.NewMsgServerImpl(f.keeper)
	client := ssAddr20(t, f, "qz-client-gov")
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))

	for i := 0; i < 4; i++ {
		qzMiner(t, f, fmt.Sprintf("qzGov%d", i), qzFreeze-10, p.MinStake)
	}
	_, err := ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jQZGovFew", Fee: 30})
	require.Error(t, err, "governed to 4, the identical pool is refused")
	require.Contains(t, err.Error(), "eligible miner", "and the refusal names what is missing")

	// ...and the fifth opens it, so what refuses is the floor and not a fixture that refuses everything.
	qzMiner(t, f, "qzGov4", qzFreeze-10, p.MinStake)
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jQZGovOk", Fee: 30})
	require.NoError(t, err, "at floor+1 the job is verifiable: refusing here would halt the network")
}
