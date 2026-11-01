package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Registry enforced (parameter on): a commit must cite a registered and active model, and the model_id
// is anchored in the commit.
func TestCreateCommitEnforcesModelRegistry(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))
	// The job exists first: CreateCommit requires that the job named by the key was opened.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "job1", types.Job{JobId: "job1"}))

	// Enable registry enforcement (governance flip).
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	commit := func(modelId string) *types.MsgCreateCommit {
		return &types.MsgCreateCommit{Creator: op, JobId: "job1__m0", PromptCommit: "p", ResultCommit: "r", Kind: "semantic", ModelId: modelId}
	}

	// Unregistered model: rejected.
	_, err = srv.CreateCommit(f.ctx, commit("ghost"))
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// Registered and active model: accepted, model_id anchored.
	f.modelReg.active["good"] = true
	_, err = srv.CreateCommit(f.ctx, commit("good"))
	require.NoError(t, err)

	got, err := f.keeper.Commit.Get(f.ctx, "job1__m0")
	require.NoError(t, err)
	require.Equal(t, "good", got.ModelId)
}

// Parameter off (the default): no model check, so a devnet without registered models still works.
func TestCreateCommitNoEnforceByDefault(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	// The job exists first: CreateCommit requires that the job named by the key was opened.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "job1", types.Job{JobId: "job1"}))
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "job1__m0", ResultCommit: "r", Kind: "semantic"})
	require.NoError(t, err) // no model required when off
}

// When the registry carries a weights anchor (weights_sha256), the commit must carry a matching
// weights_hash or it is rejected. This binds the declared model_id to an artefact.
func TestCreateCommitBindsWeightsHash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.modelReg.active["good"] = true
	f.modelReg.weights["good"] = "deadbeef"

	mk := func(job, wh string) *types.MsgCreateCommit {
		return &types.MsgCreateCommit{Creator: op, JobId: job, PromptCommit: "p", ResultCommit: "r", Kind: "semantic", ModelId: "good", WeightsHash: wh}
	}
	// The job exists first: CreateCommit requires that the job named by the key was opened.
	for _, j := range []string{"j1", "j2", "j3"} {
		require.NoError(t, f.keeper.Job.Set(f.ctx, j, types.Job{JobId: j}))
	}
	_, err = srv.CreateCommit(f.ctx, mk("j1__m0", ""))
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // anchor present, weights_hash missing
	_, err = srv.CreateCommit(f.ctx, mk("j2__m0", "00000000"))
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized) // weights_hash != anchor
	_, err = srv.CreateCommit(f.ctx, mk("j3__m0", "DEADBEEF"))
	require.NoError(t, err) // case-insensitive match
	got, err := f.keeper.Commit.Get(f.ctx, "j3__m0")
	require.NoError(t, err)
	require.Equal(t, "DEADBEEF", got.WeightsHash)
}

// Registry active but without a weights anchor: no weights_hash check (devnet backward compatibility).
func TestCreateCommitNoWeightsAnchorSkipsCheck(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.modelReg.active["good"] = true // no weights -> empty anchor
	// The job exists first: CreateCommit requires that the job named by the key was opened.
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jx", types.Job{JobId: "jx"}))
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "jx__m0", ResultCommit: "r", Kind: "semantic", ModelId: "good"})
	require.NoError(t, err) // no anchor, so weights_hash is not required
}
