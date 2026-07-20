package keeper_test

import (
	"encoding/hex"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"
)

// CR-10 — beacon VRF vérifiable : la graine de comité d'un job vient d'une preuve VRF (et non d'un hash).
func TestSubmitVrfBeacon(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("beaconAuthority_____________"))
	require.NoError(t, err)

	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)

	// OFF par défaut (vrf_beacon_pubkey vide) -> refus
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: "00"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// ancrer la clé beacon en params
	p := types.DefaultParams()
	p.VrfBeaconPubkey = hex.EncodeToString(pk)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// preuve VALIDE sur jobA -> graine posée = beta (VRF output)
	pi, err := vrf.Prove(sk, []byte("jobA"))
	require.NoError(t, err)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(f.ctx, "jobA")
	require.NoError(t, err)
	_, beta := vrf.Verify(pk, []byte("jobA"), pi)
	require.Equal(t, hex.EncodeToString(beta), b.Seed) // la graine EST la sortie VRF vérifiable

	// re-soumission sur jobA -> graine déjà fixée -> refus (one-shot)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: hex.EncodeToString(pi)})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// preuve VALIDE mais produite pour un AUTRE job_id -> refus (non rejouable)
	piOther, err := vrf.Prove(sk, []byte("autreJob"))
	require.NoError(t, err)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobC", VrfProof: hex.EncodeToString(piOther)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
}
