package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A FAILED PARAMS READ AUTHORIZES NOTHING.
//
// A message path that reads the Params as `if params, perr := ...; perr == nil && <criterion>` has a
// property its shape does not show: when the READ fails, the criterion is deemed FALSE, so the guard
// it commands does not apply. Ignorance becomes authorization, always on the reassuring side — the
// message goes through.
//
// This is the zero rule applied to an error rather than to a field: "absent" and "compliant" must
// stay two distinct answers. These cases remove the Params from the store — the only way to force a
// read failure from a test — and require a REFUSAL at each site.
//
// What these cases do not prove: that the guards themselves are correct when the Params are readable.
// That is the subject of the neighbouring cases (model registry, Nash guard, deferred reveal).

// gvSansParams removes the Params item from the store: every later read fails.
func gvSansParams(t *testing.T, f *fixture) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Remove(f.ctx))
	_, err := f.keeper.Params.Get(f.ctx)
	require.Error(t, err, "the witness itself must observe the read failure")
}

// (1) CreateCommit — without Params, the model registry cannot be evaluated. Letting it through would
// anchor a commit with NO model_id <-> artifact binding, precisely what the enforced registry exists
// to prevent.
func TestCreateCommitRefuseQuandLesParamsSontIllisibles(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	gvSansParams(t, f)

	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "job1__m0", ResultCommit: "r", Kind: "semantic"})
	require.ErrorIs(t, err, sdkerrors.ErrLogic,
		"commit ANCHORED while the params are unreadable: the model registry could not be evaluated "+
			"and the read failure counted as authorization")

	ok, herr := f.keeper.Commit.Has(f.ctx, "job1__m0")
	require.NoError(t, herr)
	require.False(t, ok, "no commit must have been written")
}

// (2) OpenJob — without Params, TWO guards fall at once: the Nash guard of the optimistic mode (an
// arbitrarily large fee makes cheating profitable again) and `committee_reveal_delay` (the committee
// seed falls back to the PREDICTABLE variant that the deferred reveal closes).
func TestOpenJobRefuseQuandLesParamsSontIllisibles(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientOuvreur_______________"))
	require.NoError(t, err)

	gvSansParams(t, f)

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "jobA", Fee: 1000})
	require.ErrorIs(t, err, sdkerrors.ErrLogic,
		"job OPENED while the params are unreadable: neither the Nash guard nor the reveal delay "+
			"could be evaluated")

	ok, herr := f.keeper.Job.Has(f.ctx, "jobA")
	require.NoError(t, herr)
	require.False(t, ok, "no job must have been written")
	ok, herr = f.keeper.Beacon.Has(f.ctx, "jobA")
	require.NoError(t, herr)
	require.False(t, ok, "no beacon must have been frozen")
}

// (3) WITNESS — with READABLE Params, both paths succeed. Without it, an unconditional refusal would
// make the two cases above green while guarding nothing.
func TestParamsLisiblesLesDeuxCheminsPassent(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	client, err := f.addressCodec.BytesToString([]byte("clientOuvreur_______________"))
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
