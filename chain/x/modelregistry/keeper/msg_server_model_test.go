package keeper_test

import (
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"

	"dendra/x/modelregistry/keeper"
	"dendra/x/modelregistry/types"
)

func govAddr(t *testing.T, f *fixture) string {
	s, err := f.addressCodec.BytesToString(authtypes.NewModuleAddress(types.GovModuleName))
	require.NoError(t, err)
	return s
}

// RegisterModel : gouvernance uniquement ; le modèle devient actif et son hash de poids est ancré.
func TestRegisterModelGovOnly(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)
	model := types.Model{Id: "llama3.1:8b-q4", WeightsSha256: "abc123", Quant: "Q4_K_M", Engine: "ollama"}

	// un tiers ne peut pas enregistrer
	bad, err := f.addressCodec.BytesToString([]byte("notTheGovAuthority__________"))
	require.NoError(t, err)
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: bad, Model: model})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// la gouvernance enregistre -> actif + hash ancré
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: model})
	require.NoError(t, err)

	got, err := f.keeper.Models.Get(f.ctx, "llama3.1:8b-q4")
	require.NoError(t, err)
	require.Equal(t, "abc123", got.WeightsSha256)
	require.True(t, got.Active)
}

// RegisterModel rejette un modèle sans hash de poids (l'attestation est obligatoire).
func TestRegisterModelRequiresHash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)
	_, err := srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{Id: "m1"}})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
}

// DeregisterModel désactive (Active=false) mais conserve l'entrée ; modèle inconnu -> erreur.
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
	require.False(t, got.Active) // désactivé mais conservé

	_, err = srv.DeregisterModel(f.ctx, &types.MsgDeregisterModel{Authority: auth, Id: "ghost"})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
}

// INT-8 — ancrage Hugging Face : hf_repo sans hf_revision = refus (source mutable) ;
// hf_repo + hf_revision = OK + ancré ; aucun champ HF = OK (rétro-compat, champs optionnels).
func TestRegisterModelHfPin(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	auth := govAddr(t, f)

	// hf_repo SANS hf_revision -> refus
	_, err := srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-hf", WeightsSha256: "h", HfRepo: "org/Model-GGUF"}})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// hf_repo + hf_revision (commit SHA immuable) -> OK + ancré
	rev := "0123456789abcdef0123456789abcdef01234567"
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-hf", WeightsSha256: "h", HfRepo: "org/Model-GGUF", HfRevision: rev}})
	require.NoError(t, err)
	got, err := f.keeper.Models.Get(f.ctx, "m-hf")
	require.NoError(t, err)
	require.Equal(t, "org/Model-GGUF", got.HfRepo)
	require.Equal(t, rev, got.HfRevision)

	// aucun champ HF -> OK (rétro-compatibilité)
	_, err = srv.RegisterModel(f.ctx, &types.MsgRegisterModel{Authority: auth, Model: types.Model{
		Id: "m-plain", WeightsSha256: "h"}})
	require.NoError(t, err)
}
