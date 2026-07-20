package keeper

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

func (k msgServer) CreateMiner(ctx context.Context, msg *types.MsgCreateMiner) (*types.MsgCreateMinerResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}
	// Check if the value already exists
	ok, err := k.Miner.Has(ctx, msg.MinerId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	} else if ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "index already set")
	}

	// --- Règle v4 (ADR-018) : bond minimum. Refuse un stake < min_stake. ---
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Stake < params.MinStake {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			fmt.Sprintf("stake %d < min_stake %d", msg.Stake, params.MinStake))
	}

	// GO-13 : BOND RÉEL. Séquestre le stake en VRAIS coins (signataire -> compte de module jobs).
	// Avant, `Stake` n'était qu'un COMPTEUR : un mineur pouvait se déclarer un stake énorme sans rien
	// déposer, et un slash ne lui retirait rien de bankable (skin-in-the-game fictif). Désormais le
	// bond est un dépôt réel détenu par le module ; le slash le retire pour de bon, l'exit rembourse
	// le restant. Échoue si le signataire n'a pas les fonds -> pas de mineur sans bond effectif.
	signerBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, err.Error())
	}
	bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Stake)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(signerBz), types.ModuleName, bond); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "bond: "+err.Error())
	}

	// MM-02 (audit v5) -- ANCRAGE ON-CHAIN de la cle de chiffrement X25519 du mineur, signe par sa
	// cle Cosmos. Le client chiffre le prompt vers CETTE pub (lue on-chain), pas celle annoncee au
	// relais -> la substitution de pub au relais (MITM) devient inoperante. Optionnel (retro-compat),
	// mais si present il DOIT etre une pub X25519 valide (32 octets en hex).
	if msg.EncPubkey != "" {
		if b, derr := hex.DecodeString(msg.EncPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "enc_pubkey invalide (attendu 32 octets X25519 en hex)")
		}
	}
	// CR-10 (ECVRF) : pub VRF Ed25519 ancrée on-chain (sert à prouver la disponibilité par une preuve
	// VRF vérifiable, et plus tard la sélection de comité). Optionnel ; si présent, 32 octets hex.
	if msg.VrfPubkey != "" {
		if b, derr := hex.DecodeString(msg.VrfPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_pubkey invalide (attendu 32 octets Ed25519 en hex)")
		}
	}
	// VE-04 (audit v7) : Operator vide -> impossible de tourner les clés (RotateMinerKeys owner-check) à jamais.
	if msg.Operator == "" {
		msg.Operator = msg.Creator
	}
	var miner = types.Miner{
		Creator:  msg.Creator,
		MinerId:  msg.MinerId,
		Operator: msg.Operator,
		Region:   msg.Region,
		Stake:    msg.Stake,
		EncPubkey: msg.EncPubkey,
		VrfPubkey: msg.VrfPubkey,
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgCreateMinerResponse{}, nil
}

func (k msgServer) UpdateMiner(ctx context.Context, msg *types.MsgUpdateMiner) (*types.MsgUpdateMinerResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "index not set")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "incorrect owner")
	}
	var miner = types.Miner{
		Creator:  msg.Creator,
		MinerId:  msg.MinerId,
		Operator: msg.Operator,
		Region:   msg.Region,
		// GO-13 : le BOND ne change PAS via update (préserve l'escrow réel ; msg.Stake ignoré).
		// Changer de bond = exit (delete -> remboursement) puis re-création (nouvel escrow).
		Stake: val.Stake,
		// MM-02 : pub X25519 ancree PRESERVEE a l'update (rotation = feature ulterieure).
		EncPubkey: val.EncPubkey,
	// CR-10 : pub VRF aussi PRESERVEE a l'update.
		VrfPubkey: val.VrfPubkey,
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to update miner")
	}
	return &types.MsgUpdateMinerResponse{}, nil
}

func (k msgServer) DeleteMiner(ctx context.Context, msg *types.MsgDeleteMiner) (*types.MsgDeleteMinerResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "index not set")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Creator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "incorrect owner")
	}
	// GO-13 : rembourse le bond RÉEL restant (post-slash) au signataire avant de retirer le mineur.
	// Le montant déjà slashé n'est PAS remboursé (resté au module = trésorerie). Exit = sortie propre.
	if val.Stake > 0 {
		toBz, err := k.addressCodec.StringToBytes(val.Creator)
		if err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, err.Error())
		}
		refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(val.Stake)))
		if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), refund); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund bond: "+err.Error())
		}
	}
	if err := k.Miner.Remove(ctx, msg.MinerId); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to remove miner")
	}
	return &types.MsgDeleteMinerResponse{}, nil
}


// RotateMinerKeys (V6-03) -- l'OPÉRATEUR remplace les pubkeys ancrées (enc X25519 / vrf Ed25519) sans
// Delete+Create (qui remettrait Demand à 0 et casserait la gate subsidy). Vide = clé inchangée.
func (k msgServer) RotateMinerKeys(ctx context.Context, msg *types.MsgRotateMinerKeys) (*types.MsgRotateMinerKeysResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid signer address: %s", err))
	}
	val, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "mineur inconnu")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != val.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "seul l'operateur du mineur peut tourner ses cles")
	}
	if msg.NewEncPubkey != "" {
		if b, derr := hex.DecodeString(msg.NewEncPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "new_enc_pubkey invalide (32 octets hex)")
		}
		val.EncPubkey = msg.NewEncPubkey
	}
	if msg.NewVrfPubkey != "" {
		if b, derr := hex.DecodeString(msg.NewVrfPubkey); derr != nil || len(b) != 32 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "new_vrf_pubkey invalide (32 octets hex)")
		}
		val.VrfPubkey = msg.NewVrfPubkey
	}
	// Demand / Stake / Operator / Region PRÉSERVÉS : on ne touche QUE les pubkeys.
	if err := k.Miner.Set(ctx, msg.MinerId, val); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to rotate miner keys")
	}
	return &types.MsgRotateMinerKeysResponse{}, nil
}
