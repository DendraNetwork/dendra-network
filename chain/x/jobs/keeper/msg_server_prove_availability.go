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

// ProveAvailability -- a miner proves it is ONLINE for the current epoch by answering the CHALLENGE,
// which is the AppHash of the epoch boundary: unpredictable, hence impossible to pre-compute, so the
// miner must read the chain NOW. Signed by the miner's OPERATOR. This records presence; the payout
// (weighted by the bond, from the AvailPool) happens at the next epoch boundary, in EndBlock.
//
// This proves the operator's LIVENESS (presence and responsiveness), not its inference capability —
// that is attested by the work flow plus semantic verification. The AvailPool therefore pays for
// availability even when there is no demand, which is what keeps miners online.
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

	// Fresh challenge: it must equal the current one (anti pre-computation; rolled from the AppHash at
	// every epoch).
	cur, err := k.AvailChallenge.Get(ctx)
	if err != nil || cur == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no active availability challenge (wait for the next epoch boundary)")
	}
	if msg.Challenge != cur {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid or stale challenge (answer the current epoch's challenge)")
	}

	// ADR-022 — ANSWER DEADLINE: when avail_deadline_blocks > 0 (liveness-slashable mode), a proof that
	// arrives TOO LATE in the epoch does not count. The miner must answer the unpredictable challenge
	// QUICKLY, otherwise an absent operator would wait until the last second to avoid the slash. Dormant
	// at 0 (the whole epoch counts). Absent miners are slashed at the next boundary
	// (runAvailabilitySlash).
	if dl := int64(params.AvailDeadlineBlocks); dl > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if rollH := (h / eb) * eb; h > rollH+dl {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "availability proof too late (past avail_deadline_blocks within the epoch)")
		}
	}

	// ADR-022 — CLOSING THE AvailPool FARM: in INCENTIVISED mode (verification_mode=1), an anchored
	// vrf_pubkey is MANDATORY to prove availability. Otherwise echoing the AppHash proves presence
	// WITHOUT a GPU, and the availability rent (a share of the emission per epoch, weighted by bond)
	// becomes farmable by a Sybil — the -EV pin bounds only the WorkPool. Requiring the VRF makes
	// presence cryptographically attributable, non-replayable and impossible to pre-compute. In legacy
	// mode (0, the default) the echo is still accepted, for backward compatibility. This gate is coupled
	// to verification_mode, which is the rewarded configuration; promoting it to a dedicated
	// `avail_require_vrf` param would require a proto regeneration and is only worth it if the two are
	// to be decoupled.
	if params.VerificationMode == 1 && miner.VrfPubkey == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"ADR-022: in incentivised mode (verification_mode=1), an anchored vrf_pubkey is MANDATORY to prove availability (the GPU-less echo is closed)")
	}

	// ECVRF -- when the miner has anchored a vrf_pubkey, availability must be proven by a VRF PROOF over
	// the current challenge rather than a plain echo: presence becomes cryptographically attributable to
	// the key, NON-replayable (someone else's proof is worthless) and impossible to pre-compute without
	// the key. Miners WITHOUT a vrf_pubkey keep the echo, for backward compatibility. This is what makes
	// the ADR-022 challenge genuinely sound.
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
	return &types.MsgProveAvailabilityResponse{}, nil
}
