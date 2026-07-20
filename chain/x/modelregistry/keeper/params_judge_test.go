package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/modelregistry/keeper"
	"dendra/x/modelregistry/types"
)

// ADR-027 D4 — le modèle-juge est épinglé on-chain (Params.audit_judge_model), gouvernable.
// Tous les membres du comité d'audit DOIVENT utiliser ce même modèle.

func TestAuditJudgeModelDefault(t *testing.T) {
	f := initFixture(t) // initFixture pose DÉJÀ DefaultParams()

	// 1) Le défaut est le juge canonique MoE-CPU (internal audit verdict 2026-06-22).
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", types.DefaultParams().AuditJudgeModel)
	require.Equal(t, types.DefaultAuditJudgeModel, types.DefaultParams().AuditJudgeModel)

	// 2) Le getter keeper lit la valeur stockée.
	got, err := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, err)
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", got)
}

func TestAuditJudgeModelGovUpdate(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	authorityStr, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)

	// La gouvernance change le modèle-juge épinglé.
	_, err = ms.UpdateParams(f.ctx, &types.MsgUpdateParams{
		Authority: authorityStr,
		Params:    types.NewParams("llama3.1:70b"),
	})
	require.NoError(t, err)

	got, err := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, err)
	require.Equal(t, "llama3.1:70b", got, "le getter doit refléter la valeur gouvernée")
}

func TestAuditJudgeModelNonGovRejected(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	// Un signataire non-gouvernance ne peut PAS changer le modèle-juge.
	_, err := ms.UpdateParams(f.ctx, &types.MsgUpdateParams{
		Authority: "dendra1notgov00000000000000000000000000000",
		Params:    types.NewParams("attacker-model"),
	})
	require.Error(t, err)

	// La valeur reste le défaut canonique.
	got, gerr := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, gerr)
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", got)
}

func TestAuditJudgeModelValidate(t *testing.T) {
	// Vide = dormant (toléré : aucun enforcement du juge).
	require.NoError(t, types.NewParams("").Validate())
	// Non vide sans espace = OK.
	require.NoError(t, types.NewParams("mistral-nemo").Validate())
	// Espace interne = rejeté (identifiant de modèle invalide).
	require.Error(t, types.NewParams("mistral nemo").Validate())
	require.Error(t, types.NewParams(" mistral-nemo").Validate())
}
