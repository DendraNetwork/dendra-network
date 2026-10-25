package keeper

import (
	"context"
	"encoding/hex"
	"errors"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// ProveAvailability lets a miner prove it is online for the current epoch by answering the challenge,
// which is the AppHash of the epoch boundary: unpredictable, so impossible to precompute, and the
// miner must read the chain now. Signed by the miner's operator. This records presence; the payout
// (weighted by the bond, from the AvailPool) happens at the next epoch boundary, in EndBlock.
//
// This proves the operator's liveness (presence and responsiveness), not its inference capability,
// which the work flow plus semantic verification attest. The AvailPool therefore pays for
// availability even when there is no demand, which keeps miners online.
func (k msgServer) ProveAvailability(ctx context.Context, msg *types.MsgProveAvailability) (*types.MsgProveAvailabilityResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	eb := int64(params.AvailEpochBlocks)
	if eb == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "availability disabled (avail_epoch_blocks=0)")
	}

	miner, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "unknown miner")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "only the miner's operator may prove its availability")
	}

	// Fresh challenge: it must equal the current one (anti precomputation; rolled from the AppHash at
	// every epoch).
	cur, err := k.AvailChallenge.Get(ctx)
	if err != nil || cur == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no active availability challenge (wait for the next epoch boundary)")
	}
	if msg.Challenge != cur {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid or stale challenge (answer the current epoch's challenge)")
	}

	// Answer deadline: when avail_deadline_blocks > 0 (liveness-slashable mode), a proof that
	// arrives too late in the epoch does not count. The miner must answer the unpredictable challenge
	// quickly, otherwise an absent operator would wait until the last moment to avoid the slash.
	// Dormant at 0 (the whole epoch counts). Absent miners are slashed at the next boundary
	// (runAvailabilitySlash).
	if dl := int64(params.AvailDeadlineBlocks); dl > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if rollH := (h / eb) * eb; h > rollH+dl {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "availability proof too late (past avail_deadline_blocks within the epoch)")
		}
	}

	// Closing the AvailPool farm: in incentivised mode (verification_mode=1), an anchored
	// vrf_pubkey is required to prove availability. Otherwise echoing the AppHash proves presence
	// without a GPU, and the availability rent (a share of the emission per epoch, weighted by bond)
	// can be farmed by a Sybil, since the -EV pin bounds only the WorkPool. Requiring the VRF makes
	// presence cryptographically attributable, non-replayable and impossible to precompute. In
	// legacy mode (0, the default) the echo is still accepted, for backward compatibility. This gate
	// is coupled to verification_mode, the rewarded configuration; promoting it to a dedicated
	// `avail_require_vrf` param would require a proto regeneration and is only worth it if the two
	// are to be decoupled.
	if params.VerificationMode == 1 && miner.VrfPubkey == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"in incentivised mode (verification_mode=1), an anchored vrf_pubkey is required to prove availability (the GPU-less echo is closed)")
	}

	// ECVRF: when the miner has anchored a vrf_pubkey, availability must be proven by a VRF proof over
	// the current challenge rather than a plain echo, so presence is cryptographically attributable to
	// the key, non-replayable (someone else's proof is worthless) and impossible to precompute without
	// the key. Miners without a vrf_pubkey keep the echo, for backward compatibility. This is what makes
	// the availability challenge sound.
	if miner.VrfPubkey != "" {
		pk, perr := hex.DecodeString(miner.VrfPubkey)
		if perr != nil || len(pk) != vrf.PublicKeySize {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "the miner's on-chain vrf_pubkey is invalid")
		}
		pi, perr := hex.DecodeString(msg.VrfProof)
		if perr != nil || len(pi) != vrf.ProofSize {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_proof required (ECVRF proof over the challenge, 80 bytes hex)")
		}
		if ok, _ := vrf.Verify(pk, []byte(cur), pi); !ok {
			return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "invalid VRF proof for this challenge")
		}
	}

	epoch := sdk.UnwrapSDKContext(ctx).BlockHeight() / eb
	if err := k.Available.Set(ctx, collections.Join(epoch, msg.MinerId)); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// The draws read presence from here, because `Available` is purged at every window
	// boundary and could no longer say who proved the window that just closed. Written only forward: a
	// late transaction cannot move a miner's last proven window backwards.
	if epoch >= 0 {
		if last, lErr := k.MinerLastPresentEpoch.Get(ctx, msg.MinerId); lErr != nil || uint64(epoch) > last {
			if err := k.MinerLastPresentEpoch.Set(ctx, msg.MinerId, uint64(epoch)); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
		}
	}
	return &types.MsgProveAvailabilityResponse{}, nil
}
