package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// Incrément C — registre APPLIQUÉ (param ON) : un commit doit citer un modèle enregistré + actif,
// et le model_id est ancré dans le commit.
func TestCreateCommitEnforcesModelRegistry(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	// activer l'application du registre (flip gouvernance)
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	commit := func(modelId string) *types.MsgCreateCommit {
		return &types.MsgCreateCommit{Creator: op, JobId: "job1__m0", PromptCommit: "p", ResultCommit: "r", Kind: "semantic", ModelId: modelId}
	}

	// modèle non enregistré -> rejet
	_, err = srv.CreateCommit(f.ctx, commit("ghost"))
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// modèle enregistré + actif -> accepté, model_id ancré
	f.modelReg.active["good"] = true
	_, err = srv.CreateCommit(f.ctx, commit("good"))
	require.NoError(t, err)

	got, err := f.keeper.Commit.Get(f.ctx, "job1__m0")
	require.NoError(t, err)
	require.Equal(t, "good", got.ModelId)
}

// Param OFF (défaut) -> aucun contrôle du modèle : le devnet (sans modèles enregistrés) n'est pas cassé.
func TestCreateCommitNoEnforceByDefault(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))

	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "job1__m0", ResultCommit: "r", Kind: "semantic"})
	require.NoError(t, err) // pas de modèle requis quand OFF
}

// NEW-MR-03 (audit v5) : quand le registre a une ANCRE de poids (weights_sha256), le commit doit
// porter un weights_hash CORRESPONDANT, sinon rejet -> lie le model_id déclaré à un artefact.
func TestCreateCommitBindsWeightsHash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.modelReg.active["good"] = true
	f.modelReg.weights["good"] = "deadbeef"

	mk := func(job, wh string) *types.MsgCreateCommit {
		return &types.MsgCreateCommit{Creator: op, JobId: job, PromptCommit: "p", ResultCommit: "r", Kind: "semantic", ModelId: "good", WeightsHash: wh}
	}
	_, err = srv.CreateCommit(f.ctx, mk("j1__m0", ""))
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // ancre présente, weights_hash manquant
	_, err = srv.CreateCommit(f.ctx, mk("j2__m0", "00000000"))
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized) // weights_hash != ancre
	_, err = srv.CreateCommit(f.ctx, mk("j3__m0", "DEADBEEF"))
	require.NoError(t, err) // match insensible à la casse
	got, err := f.keeper.Commit.Get(f.ctx, "j3__m0")
	require.NoError(t, err)
	require.Equal(t, "DEADBEEF", got.WeightsHash)
}

// Registre actif mais SANS ancre de poids -> aucun contrôle de weights_hash (rétro-compat devnet).
func TestCreateCommitNoWeightsAnchorSkipsCheck(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: op}))
	p := types.DefaultParams()
	p.EnforceModelRegistry = true
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.modelReg.active["good"] = true // pas de weights -> ancre vide
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "jx__m0", ResultCommit: "r", Kind: "semantic", ModelId: "good"})
	require.NoError(t, err) // pas d'ancre -> weights_hash non exigé
}
