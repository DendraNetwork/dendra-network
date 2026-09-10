package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/modelregistry/types"
)

func govAddr(t *testing.T, f *fixture) string {
	s, err := f.addressCodec.BytesToString(authtypes.NewModuleAddress(types.GovModuleName))
	require.NoError(t, err)
	return s
}

// RegisterModel: governance only; the model becomes active and its weights hash is anchored.
func TestRegisterModelGovOnly(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)
	model := types.Model{Id: "llama3.1:8b-q4", WeightsSha256: "abc123", Quant: "Q4_K_M", Engine: "ollama"}

	// a third party cannot register
	bad, err := f.addressCodec.BytesToString([]byte("notTheGovAuthority__________"))
	require.NoError(t, err)
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: bad, Model: model})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// governance registers -> active + hash anchored
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: model})
	require.NoError(t, err)

	got, err := f.keeper.Models.Get(f.ctx, "llama3.1:8b-q4")
	require.NoError(t, err)
	require.Equal(t, "abc123", got.WeightsSha256)
	require.True(t, got.Active)
}

// RegisterModel rejects a model without a weights hash: the attestation is mandatory.
func TestRegisterModelRequiresHash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)
	_, err := srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{Id: "m1"}})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}

// DeregisterModel deactivates (Active=false) but keeps the entry; an unknown model is an error.
func TestDeregisterModel(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)

	_, err := srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{Id: "m1", WeightsSha256: "h"}})
	require.NoError(t, err)

	_, err = srv.DeregisterModel(f.ctx, &types.MsgDeregisterModel{Authority: auth, Id: "m1"})
	require.NoError(t, err)
	got, err := f.keeper.Models.Get(f.ctx, "m1")
	require.NoError(t, err)
	require.False(t, got.Active) // deactivated but retained

	_, err = srv.DeregisterModel(f.ctx, &types.MsgDeregisterModel{Authority: auth, Id: "ghost"})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
}

// Hugging Face pinning: hf_repo without hf_revision is refused (mutable source); hf_repo plus
// hf_revision is accepted and anchored; no HF field at all is accepted (the fields are optional).
func TestRegisterModelHfPin(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)

	// hf_repo WITHOUT hf_revision -> refused
	_, err := srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-hf", WeightsSha256: "h", HfRepo: "org/Model-GGUF"}})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// hf_repo + hf_revision (immutable commit SHA) -> accepted and anchored
	rev := "0123456789abcdef0123456789abcdef01234567"
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-hf", WeightsSha256: "h", HfRepo: "org/Model-GGUF", HfRevision: rev}})
	require.NoError(t, err)
	got, err := f.keeper.Models.Get(f.ctx, "m-hf")
	require.NoError(t, err)
	require.Equal(t, "org/Model-GGUF", got.HfRepo)
	require.Equal(t, rev, got.HfRevision)

	// no HF field at all -> accepted (the fields are optional)
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-plain", WeightsSha256: "h"}})
	require.NoError(t, err)
}
