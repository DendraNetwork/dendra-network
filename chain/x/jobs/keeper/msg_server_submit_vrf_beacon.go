package keeper

import (
	"context"
	"encoding/hex"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// SubmitVrfBeacon sets a job's committee seed from a verifiable VRF proof rather than from a raw
// AppHash: seed = beta = VRF(beacon_key, job_id), verified on-chain against params.vrf_beacon_pubkey.
// Only the holder of the beacon key can produce a valid proof, and the output is deterministic per
// job, so there is nothing to grind. One-shot: the seed is set only while it is empty, so it never
// overwrites a deferred reveal. Disabled when vrf_beacon_pubkey is empty, leaving the AppHash reveal
// path unchanged.
//
// The beacon authority is designated, which is a devnet arrangement. Full decentralization is the
// per-validator VRF carried by ABCI++ vote extensions. This verifiable primitive is the building
// block: the chain already verifies the proof, and what remains is to distribute the production of
// the randomness.
func (k msgServer) SubmitVrfBeacon(ctx context.Context, msg *types.MsgSubmitVrfBeacon) (*types.MsgSubmitVrfBeaconResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, err.Error())
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if params.VrfBeaconPubkey == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "VRF beacon disabled (vrf_beacon_pubkey is empty)")
	}
	pk, derr := hex.DecodeString(params.VrfBeaconPubkey)
	if derr != nil || len(pk) != vrf.PublicKeySize {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "invalid on-chain vrf_beacon_pubkey")
	}
	pi, derr := hex.DecodeString(msg.VrfProof)
	if derr != nil || len(pi) != vrf.ProofSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid vrf_proof (expected 80 bytes, hex-encoded)")
	}
	ok, beta := vrf.Verify(pk, []byte(msg.JobId), pi)
	if !ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "invalid VRF proof for this job_id")
	}
	// One-shot: never overwrite a seed that is already set, whether by a deferred reveal or by an
	// earlier beacon.
	if b, gerr := k.Beacon.Get(ctx, msg.JobId); gerr == nil && b.Seed != "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "seed already set for this job")
	}
	beacon := types.Beacon{JobId: msg.JobId, Seed: hex.EncodeToString(beta), Creator: msg.Creator}
	if err := k.Beacon.Set(ctx, msg.JobId, beacon); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgSubmitVrfBeaconResponse{}, nil
}
