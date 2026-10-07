package keeper_test

import (
	"fmt"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// IN OPTIMISTIC MODE AT audit_min_quorum = 0, ADMISSION DEMANDS NOTHING AND THE DRAW DEMANDS FOUR.
//
// `enoughEligibleMinersToVerify` is the pre-condition that refuses a job whose frozen pool could never
// supply a jury — `OpenJob` is its only caller, and its own header says why it must be a refusal and not
// a warning: the pool freezes at the job's birth and never widens (ADR-037), so an under-supplied job is
// unverifiable FOREVER, and `deferAuditForSmallJury` re-queues it without bound. Measured on the chain
// destroyed on 2026-08-13: 42 jobs out of 51 in exactly that state, holding 272 406 udndr that no
// verdict, no timeout and no governance message could release.
//
// It builds its floor from the raw parameter and returns nil at zero, on the ground that "there is no
// number to compare against here". There is one, and the same package returns it in a single call:
// `effectiveSlashFloor` yields `audit_min_quorum` when set and `auditSlashFloor()` = 4 when it is zero —
// and that is the very function the draw consults before deferring a jury it judges too small
// (audit_sampling.go). `Params.Validate` keeps zero legal on purpose, in its own words "the explicit
// fallback, not a silent weakening". So the fallback IS a floor of four, and admission is the only place
// that reads zero as "no constraint".
//
// ⚠️ THE ABSTENTION IS CORRECT IN k=3 MODE, which is why these cases set mode 1. `runOptimisticAudit`
// returns immediately when `verification_mode != 1`, so no audit jury is ever drawn there and an audit
// floor has no business gating admission. Making the guard unconditional was measured first: twenty
// existing cases went red, every one a k=3 fixture never at risk. A fail-closed check applied outside
// the failure it guards stops the network instead of protecting it.
//
// ⛔ THESE CASES PIN A DEFECT, NOT A RULE, AND THE FIX IS AN OWNER'S CALL — the two candidates differ in
// what they cost, and neither is a one-line change:
//   - ask `effectiveSlashFloor` at admission: correct, and aligned with the documented intent of zero.
//     It extends the bootstrap ratchet — no job at all until five miners exist — to quorum zero. Measured
//     side effect: four existing mode-1 fixtures open jobs with a single miner, so they model networks
//     the chain would refuse, and seeding their pools CHANGES THE DRAW; one of them then failed with
//     "primary commit missing" because the drawn primary was no longer the miner that had committed.
//   - bound the deferral instead: after N attempts, release the held fee rather than re-queue for ever.
//     That decides WHO receives the fee of a job nobody can judge, which is a tokenomics decision.
//
// Whichever lands, the first case must flip: it asserts the job is ACCEPTED today.

// qzMiner registers a miner directly in state at a given registration height, the way the neighbouring
// ADR-037 and ADR-042 cases do: the draw reads `MinerRegisteredHeight`, not a message.
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
	require.Zero(t, p.AuditMinQuorum, "premise: zero IS the default -- if that changes, so must this file")
	p.VerificationMode = 1 // the only mode that draws an audit jury
	p.CommitteeRevealDelay = 1
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	p.AuditSampleBps = 1000
	p.DisputeBond = p.MinStake // Validate: an active dispute window with a free bond is refused
	require.NoError(t, p.Validate(),
		"premise: mode 1 beside a ZERO quorum is a state governance reaches in one MsgUpdateParams")
	return p
}

func TestQuorumZeroRefusesAJobTheDrawCouldNeverJudge(t *testing.T) {
	f := initFixture(t)
	p := qzOptimisticParams(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	ms := keeper.NewMsgServerImpl(f.keeper)
	client := ssAddr20(t, f, "qz-client")
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(int64(qzFreeze))

	// FOUR eligible miners. At quorum zero the draw's floor is 4, and the jury excludes the miner under
	// audit, so four is one short of what any verdict needs.
	for i := 0; i < 4; i++ {
		qzMiner(t, f, fmt.Sprintf("qzFew%d", i), qzFreeze-10, p.MinStake)
	}

	_, err := ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jQZFew", Fee: 30})
	require.Error(t, err,
		"admission must demand what the DRAW demands: a zero quorum selects the fallback floor of 4, so four eligible miners are one short")
	require.Contains(t, err.Error(), "5 needed",
		"the refusal has to name the number it applied, or the operator cannot tell which floor refused")
	require.Contains(t, err.Error(), "effective audit floor=4",
		"and it names the EFFECTIVE floor, not the raw parameter: at quorum zero the raw value is 0 and printing it explained the refusal wrongly")

	// NOTHING was written. A refusal that still froze the pool or booked the fee would trade an
	// unjudgeable job for an unreachable one.
	_, gErr := f.keeper.JobPoolFreezeHeight.Get(f.ctx, "jQZFew")
	require.Error(t, gErr, "a refused job must not freeze a pool")
	_, jErr := f.keeper.Job.Get(f.ctx, "jQZFew")
	require.Error(t, jErr, "nor leave a job record holding a fee")
}

// The control, and it is what makes the refusal above a FLOOR rather than a blanket refusal: one more
// eligible miner and the same call is accepted. Without it, a guard that refused everything would pass.
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

// THE COMPARISON THAT MAKES IT A DEFECT RATHER THAN A CHOICE: the SAME under-supplied pool, with the
// quorum GOVERNED to the very floor the zero already selects, is refused. One parameter value, two
// opposite admissions, for a draw that behaves identically in both.
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
