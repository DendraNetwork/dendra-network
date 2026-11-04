package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A failed params read authorizes nothing.
//
// A message path that reads the Params as `if params, perr := ...; perr == nil && <criterion>` has a
// property its shape hides: when the read fails, the criterion is deemed false, so the guard it commands
// does not apply. Ignorance becomes authorization, always on the reassuring side: the message goes
// through.
//
// This is the zero rule applied to an error instead of a field: "absent" and "compliant" stay two
// distinct answers. These cases remove the Params from the store, the only way to force a read failure
// from a test, and require a refusal at each site.
//
// They do not prove that the guards are correct when the Params are readable; the neighbouring cases
// cover that (model registry, Nash guard, deferred reveal).

// gvWithoutParams removes the Params item from the store: every later read fails.
func gvWithoutParams(t *testing.T, f *fixture) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Remove(f.ctx))
	_, err := f.keeper.Params.Get(f.ctx)
	require.Error(t, err, "the witness itself must observe the read failure")
}

// (1) CreateCommit: without Params, the model registry cannot be evaluated. Letting the commit through
// would anchor it with no model_id <-> artifact binding, which the enforced registry exists to prevent.
func TestCreateCommitRefusesWhenParamsAreUnreadable(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	gvWithoutParams(t, f)

	// The job exists first, so this case still measures what it aims at: the existence guard refuses before
	// the params read, and without the job the test would see the wrong refusal and vouch for a fail-closed
	// it never exercised.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "job1", types.Job{JobId: "job1"}))
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "job1__m0", ResultCommit: "r", Kind: "semantic"})
	require.ErrorIs(t, err, sdkerrors.ErrLogic,
		"commit anchored while the params are unreadable: the model registry could not be evaluated "+
			"and the read failure counted as authorization")

	ok, herr := f.keeper.Commit.Has(f.ctx, "job1__m0")
	require.NoError(t, herr)
	require.False(t, ok, "no commit must have been written")
}

// (2) OpenJob: without Params, two guards fall at once, the Nash guard of optimistic mode (an
// arbitrarily large fee makes cheating profitable again) and `committee_reveal_delay` (the committee seed
// falls back to the predictable variant the deferred reveal closes).
func TestOpenJobRefusesWhenParamsAreUnreadable(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientOpener________________"))
	require.NoError(t, err)

	gvWithoutParams(t, f)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "jobA", Fee: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrLogic,
		"job opened while the params are unreadable: neither the Nash guard nor the reveal delay "+
			"could be evaluated")

	ok, herr := f.keeper.Job.Has(f.ctx, "jobA")
	require.NoError(t, herr)
	require.False(t, ok, "no job must have been written")
	ok, herr = f.keeper.Beacon.Has(f.ctx, "jobA")
	require.NoError(t, herr)
	require.False(t, ok, "no beacon must have been frozen")
}

// (3) Witness: with readable Params, both paths succeed. Without it, an unconditional refusal would make
// the two cases above green while guarding nothing.
func TestReadableParamsBothPathsPass(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientOpener________________"))
	require.NoError(t, err)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "jobA", Fee: 1000})
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jobA__m0", ResultCommit: "r", Kind: "semantic"})
	require.NoError(t, err)
}
