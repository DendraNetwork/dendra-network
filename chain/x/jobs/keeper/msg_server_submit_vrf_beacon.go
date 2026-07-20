package keeper

import (
	"context"
	"encoding/hex"

	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"
)

// SubmitVrfBeacon -- CR-10 (VRF du proposant/beacon). Pose la graine de comité d'un job à partir d'une
// PREUVE VRF VÉRIFIABLE (et non l'AppHash brut) : seed = beta = VRF(beacon_key, job_id), vérifiée on-chain
// contre params.vrf_beacon_pubkey. Seul le détenteur de la clé beacon produit une preuve valide ; la sortie
// est déterministe par job (pas de re-grinding). ONE-SHOT : ne pose la graine que si elle est vide (n'écrase
// pas une révélation différée). OFF si vrf_beacon_pubkey vide (-> révélation AppHash inchangée).
//
// HONNÊTE : autorité beacon DÉSIGNÉE (devnet) ; la décentralisation pleine = VRF par-validateur via
// vote-extensions ABCI++ (étape suivante documentée, cf docs/MULTI-VALIDATEUR.md). Cette primitive vérifiable
// est le bloc de construction : la chaîne VÉRIFIE déjà la preuve ; reste à distribuer la production de l'aléa.
func (k msgServer) SubmitVrfBeacon(ctx context.Context, msg *types.MsgSubmitVrfBeacon) (*types.MsgSubmitVrfBeaconResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, err.Error())
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if params.VrfBeaconPubkey == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "beacon VRF desactive (vrf_beacon_pubkey vide)")
	}
	pk, derr := hex.DecodeString(params.VrfBeaconPubkey)
	if derr != nil || len(pk) != vrf.PublicKeySize {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "vrf_beacon_pubkey invalide on-chain")
	}
	pi, derr := hex.DecodeString(msg.VrfProof)
	if derr != nil || len(pi) != vrf.ProofSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_proof invalide (attendu 80 octets en hex)")
	}
	ok, beta := vrf.Verify(pk, []byte(msg.JobId), pi)
	if !ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "preuve VRF invalide pour ce job_id")
	}
	// ONE-SHOT : ne pas écraser une graine déjà fixée (révélation différée OU beacon déjà posé).
	if b, gerr := k.Beacon.Get(ctx, msg.JobId); gerr == nil && b.Seed != "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "graine deja fixee pour ce job")
	}
	beacon := types.Beacon{JobId: msg.JobId, Seed: hex.EncodeToString(beta), Creator: msg.Creator}
	if err := k.Beacon.Set(ctx, msg.JobId, beacon); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgSubmitVrfBeaconResponse{}, nil
}
