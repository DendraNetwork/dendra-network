package keeper_test

import (
	"encoding/hex"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// Verifiable VRF beacon: a job's committee seed comes from a VRF proof, not from a hash.
func TestSubmitVrfBeacon(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("beaconAuthority_____________"))
	require.NoError(t, err)

	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)

	// Off by default (empty vrf_beacon_pubkey) -> refused
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: "00"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// anchor the beacon key in params
	p := types.DefaultParams()
	p.VrfBeaconPubkey = hex.EncodeToString(pk)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// VALID proof on jobA -> the seed is set to beta (the VRF output)
	pi, err := vrf.Prove(sk, []byte("jobA"))
	require.NoError(t, err)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(f.ctx, "jobA")
	require.NoError(t, err)
	_, beta := vrf.Verify(pk, []byte("jobA"), pi)
	require.Equal(t, hex.EncodeToString(beta), b.Seed) // the seed IS the verifiable VRF output

	// resubmitting on jobA -> seed already set -> refused (one-shot)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobA", VrfProof: hex.EncodeToString(pi)})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// a VALID proof produced for ANOTHER job_id -> refused (not replayable across jobs)
	piOther, err := vrf.Prove(sk, []byte("autreJob"))
	require.NoError(t, err)
	_, err = srv.SubmitVrfBeacon(f.ctx, &types.MsgSubmitVrfBeacon{Creator: creator, JobId: "jobC", VrfProof: hex.EncodeToString(piOther)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
}
