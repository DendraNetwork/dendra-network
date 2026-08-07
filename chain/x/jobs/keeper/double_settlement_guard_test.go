package keeper_test

import (
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// DOUBLE SLASH OF THE k=3 COMMITTEE.
//
// VerifySemantic and FinalizeJob slash the cosine outliers of a k=3 committee. Each one recognised only
// ITS OWN anti-replay marker (`+verified` / `+finalized`), so `settle(k3) -> verify` and then
// `verify -> finalize` re-slashed the SAME outlier (80% of the remainder on each pass). Nothing disabled
// them in OPTIMISTIC mode either, where a permissionless attacker could slash HONEST non-primary miners
// (cosine is dead as a judge there). Two locks: a mode-0 gate plus a UNIFIED anti-replay
// (jobK3Adjudicated).

func dsCreator(f *fixture, t *testing.T) string {
	t.Helper()
	c, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("declencheur_ds_2107_")))
	require.NoError(t, err)
	return c
}

// (1) OPTIMISTIC MODE: verify-semantic is REFUSED (cosine plays no role; the VRF audit does the job).
func TestVerifySemanticRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	c := dsCreator(f, t)
	require.NoError(t, gvSetJob(f, f.ctx, "jVS", types.Job{JobId: "jVS", State: "open", Fee: 100000}))

	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: c, JobId: "jVS"})
	require.Error(t, err, "verify-semantic must be REFUSED in optimistic mode (griefing of honest non-primary miners)")
}

// (2) ANTI-DOUBLE-SLASH: a job that is ALREADY settled (paid+finalized) can no longer be re-slashed by
// verify-semantic (mode 0). The guard used to test only `+verified`, so settle then verify re-slashed.
func TestVerifySemanticRefusedOnAlreadySettledJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	c := dsCreator(f, t)
	require.NoError(t, gvSetJob(f, f.ctx, "jPaid", types.Job{JobId: "jPaid", State: "open+paid+finalized", Fee: 100000}))

	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err := srv.VerifySemantic(f.ctx, &types.MsgVerifySemantic{Creator: c, JobId: "jPaid"})
	require.Error(t, err, "a k=3 committee already adjudicated (settled/verified/finalized) must not be re-slashed (anti-double-slash)")
}

// (3) SYMMETRY: finalize-job shares both locks.
func TestFinalizeJobRefusedInOptimisticMode(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.VerificationMode = 1
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	c := dsCreator(f, t)
	require.NoError(t, gvSetJob(f, f.ctx, "jFJ", types.Job{JobId: "jFJ", State: "open", Fee: 100000}))

	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: c, JobId: "jFJ"})
	require.Error(t, err, "finalize-job must be REFUSED in optimistic mode (same griefing as verify-semantic)")
}

func TestFinalizeJobRefusedOnAlreadyVerifiedJob(t *testing.T) {
	f := initFixture(t)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams())) // mode 0
	c := dsCreator(f, t)
	// Job already VERIFIED (the k=3 slash was applied by verify) -> finalize must not re-slash.
	require.NoError(t, gvSetJob(f, f.ctx, "jVerif", types.Job{JobId: "jVerif", State: "open+verified", Fee: 100000}))

	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: c, JobId: "jVerif"})
	require.Error(t, err, "verify -> finalize must not re-slash the same outlier (unified anti-replay)")
}
