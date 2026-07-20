package keeper

import (
	"context"
	"encoding/hex"
	"fmt"

	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/oasisprotocol/curve25519-voi/primitives/ed25519"
)

// RegisterValidatorVrfKey (E4 -- VRF décentralisée, vote-extensions) : un validateur ancre sa clé
// PUBLIQUE VRF Ed25519 on-chain, signée par son compte d'opérateur. Elle sert à VÉRIFIER ses
// vote-extensions VRF (ExtendVote/VerifyVoteExtension) côté consensus. Stockée sous le compte signataire.
//
// Auto-autorisée : on ne peut ancrer QUE la clé de son propre compte (le signataire EST la clé du store).
// La clé d'un compte non-validateur peut être posée mais ne sera JAMAIS consultée (le consommateur app.go
// ne lit que la clé du compte d'opérateur d'un validateur réel). DORMANT tant que les vote-extensions sont
// OFF (vote_extensions_enable_height non posé) → zéro impact sur la chaîne actuelle.
func (k msgServer) RegisterValidatorVrfKey(ctx context.Context, msg *types.MsgRegisterValidatorVrfKey) (*types.MsgRegisterValidatorVrfKeyResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	// La clé publique VRF est une clé Ed25519 (32 octets, hex) — même format que la vrf_pubkey mineur.
	pkb, derr := hex.DecodeString(msg.VrfPubkey)
	if derr != nil || len(pkb) != 32 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_pubkey invalide (attendu 32 octets Ed25519 en hex)")
	}
	// VE-02 (audit v7) — PROOF-OF-POSSESSION : prouver qu'on détient la clé PRIVÉE correspondante (sinon on
	// pourrait ancrer une clé qu'on ne possède pas). PoP = preuve VRF sur "dendra/vrf-pop/<creator>" vérifiée
	// contre la pubkey annoncée. Seul le détenteur de la clé privée peut produire cette preuve.
	pib, perr := hex.DecodeString(msg.VrfPop)
	if perr != nil || len(pib) != vrf.ProofSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_pop invalide (attendu une preuve VRF de 80 octets en hex)")
	}
	if ok, _ := vrf.Verify(ed25519.PublicKey(pkb), []byte("dendra/vrf-pop/"+msg.Creator), pib); !ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "proof-of-possession invalide : cle VRF non prouvee possedee")
	}
	if err := k.ValidatorVrfPubkey.Set(ctx, msg.Creator, msg.VrfPubkey); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgRegisterValidatorVrfKeyResponse{}, nil
}
