package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

// ADR-027 D4: `Params.audit_judge_model` records an audit judge model on-chain and puts it under
// governance. These tests cover the storage and the authority, nothing more: no consensus path reads
// the field. The kit's judge reads it off-chain to choose its model (ADR-057), so a jury of kit judges
// runs one model, yet the chain enforces none: a juror running another model is accepted the same way.
// Reading these tests as "the chain enforces one model for every juror" would misread what they
// cover.

func TestAuditJudgeModelDefault(t *testing.T) {
	f := initFixture(t) // initFixture already writes DefaultParams()

	// 1) The default is the canonical MoE-CPU judge.
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", types.DefaultParams().AuditJudgeModel)
	require.Equal(t, types.DefaultAuditJudgeModel, types.DefaultParams().AuditJudgeModel)

	// 2) The keeper getter reads the stored value.
	got, err := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, err)
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", got)
}

func TestAuditJudgeModelGovUpdate(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	authorityStr, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)

	// Governance changes the recorded audit judge model.
	_, err = ms.UpdateParams(f.ctx, &types.MsgUpdateParams{
		Authority: authorityStr,
		Params:    types.NewParams("llama3.1:70b"),
	})
	require.NoError(t, err)

	got, err := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, err)
	require.Equal(t, "llama3.1:70b", got, "the getter must reflect the governed value")
}

func TestAuditJudgeModelNonGovRejected(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	// A non-governance signer cannot change the judge model.
	_, err := ms.UpdateParams(f.ctx, &types.MsgUpdateParams{
		Authority: "dendra1notgov00000000000000000000000000000",
		Params:    types.NewParams("attacker-model"),
	})
	require.Error(t, err)

	// The value stays the canonical default.
	got, gerr := f.keeper.AuditJudgeModel(f.ctx)
	require.NoError(t, gerr)
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", got)
}

func TestAuditJudgeModelValidate(t *testing.T) {
	// Empty = dormant (tolerated: no judge enforcement).
	require.NoError(t, types.NewParams("").Validate())
	// Non-empty without whitespace = OK.
	require.NoError(t, types.NewParams("mistral-nemo").Validate())
	// Internal whitespace = rejected (invalid model identifier).
	require.Error(t, types.NewParams("mistral nemo").Validate())
	require.Error(t, types.NewParams(" mistral-nemo").Validate())
}
