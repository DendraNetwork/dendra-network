package keeper

import (
	"context"
	"encoding/hex"
	"fmt"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"
)

// RegisterValidatorVrfKey — decentralized VRF over vote extensions: a validator anchors its Ed25519
// VRF PUBLIC key on-chain, signed by its operator account. The key is used to VERIFY that validator's
// VRF vote extensions (ExtendVote/VerifyVoteExtension) on the consensus side. It is stored under the
// signer account.
//
// Self-authorized: only the key of one's own account can be anchored (the signer IS the store key).
// A non-validator account may anchor a key, but it will NEVER be consulted — the consumer in app.go
// reads only the key of a real validator's operator account. The message is inert while vote
// extensions are off (vote_extensions_enable_height unset).
func (k msgServer) RegisterValidatorVrfKey(ctx context.Context, msg *types.MsgRegisterValidatorVrfKey) (*types.MsgRegisterValidatorVrfKeyResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	// The VRF public key is an Ed25519 key (32 bytes, hex) — the same format as a miner's vrf_pubkey.
	pkb, derr := hex.DecodeString(msg.VrfPubkey)
	if derr != nil || len(pkb) != 32 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid vrf_pubkey (expected 32 Ed25519 bytes in hex)")
	}
	// PROOF OF POSSESSION: the sender must prove it holds the matching PRIVATE key, otherwise it could
	// anchor a key it does not own. The PoP is a VRF proof over "dendra/vrf-pop/<creator>" verified
	// against the announced pubkey. Only the private-key holder can produce it.
	pib, perr := hex.DecodeString(msg.VrfPop)
	if perr != nil || len(pib) != vrf.ProofSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "invalid vrf_pop (expected an 80-byte VRF proof in hex)")
	}
	if ok, _ := vrf.Verify(ed25519.PublicKey(pkb), []byte("dendra/vrf-pop/"+msg.Creator), pib); !ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "invalid proof-of-possession: VRF key not proven to be held")
	}
	if err := k.ValidatorVrfPubkey.Set(ctx, msg.Creator, msg.VrfPubkey); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgRegisterValidatorVrfKeyResponse{}, nil
}
