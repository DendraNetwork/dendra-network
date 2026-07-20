package keeper

import (
	"context"
	"encoding/hex"
	"errors"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"

	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"
)

// ProveAvailability -- Phase 1b. Un mineur prouve qu'il est EN LIGNE pour l'époque courante en
// répondant au DÉFI = AppHash de la frontière d'époque (imprévisible -> impossible à pré-calculer : le
// mineur doit lire la chaîne MAINTENANT). Signé par l'OPÉRATEUR du mineur. Enregistre la présence ; le
// versement (pondéré par le bond, depuis l'AvailPool) a lieu à la frontière d'époque suivante (EndBlock).
//
// NB : ceci prouve la LIVENESS de l'opérateur (présence/réactivité), pas la capacité d'inférence — cette
// dernière est attestée par le flux TRAVAIL + la vérification sémantique. L'AvailPool rémunère donc la
// disponibilité même en l'absence de demande (incite les mineurs à rester en ligne).
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
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "disponibilite desactivee (avail_epoch_blocks=0)")
	}

	miner, err := k.Miner.Get(ctx, msg.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "mineur inconnu")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "seul l'operateur du mineur peut prouver sa disponibilite")
	}

	// défi frais : doit égaler le défi courant (anti pré-calcul ; roulé depuis l'AppHash chaque époque).
	cur, err := k.AvailChallenge.Get(ctx)
	if err != nil || cur == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "aucun defi de disponibilite actif (attendez la prochaine frontiere d'epoque)")
	}
	if msg.Challenge != cur {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "defi invalide ou perime (repondez au defi de l'epoque courante)")
	}

	// ADR-022 PLEIN (internal audit 2026-06-27) — ÉCHÉANCE DE RÉPONSE : si avail_deadline_blocks>0 (mode liveness-
	// slashable), une preuve TROP TARDIVE dans l'époque ne compte pas — le mineur doit répondre VITE au défi
	// imprévisible (sinon un absent attendrait la dernière seconde pour éviter le slash). Dormant à 0 (toute
	// l'époque, comportement actuel). Le slash des absents est appliqué à la frontière suivante (runAvailabilitySlash).
	if dl := int64(params.AvailDeadlineBlocks); dl > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if rollH := (h / eb) * eb; h > rollH+dl {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "preuve de disponibilite trop tardive (apres avail_deadline_blocks dans l'epoque)")
		}
	}

	// ADR-022 (internal audit 2026-06-26) — FIN DU FARM AvailPool : en mode INCENTIVÉ (verification_mode=1), une
	// vrf_pubkey ancrée est OBLIGATOIRE pour prouver la disponibilité. Sinon l'echo de l'AppHash prouve la
	// présence SANS GPU -> rente dispo (20 % de l'émission, par époque, pondérée bond) farmable par un Sybil
	// (le pin -EV ne borne QUE le WorkPool). Exiger la VRF rend la présence cryptographiquement attribuable,
	// non rejouable, non pré-calculable. En mode legacy (0, défaut), l'echo reste accepté (rétro-compat, tests
	// e2e intacts). NB : ce gate est couplé à verification_mode (= la config récompensée) pour rester sans
	// régén proto ; promotion en param dédié `avail_require_vrf` = suivi possible (régén) si découplage voulu.
	if params.VerificationMode == 1 && miner.VrfPubkey == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"ADR-022 : en mode incentive (verification_mode=1), une vrf_pubkey ancree est OBLIGATOIRE pour prouver la disponibilite (fin de l'echo sans GPU)")
	}

	// CR-10 (ECVRF) -- si le mineur a ancré une vrf_pubkey, la disponibilité doit être prouvée par une
	// PREUVE VRF sur le défi courant (et non un simple echo) : présence cryptographiquement attribuable
	// à la clé, NON rejouable (la preuve d'un autre ne vaut pas) et NON pré-calculable sans la clé. Les
	// mineurs SANS vrf_pubkey gardent l'echo (rétro-compat). Débloque ADR-022 (défi réellement sûr).
	if miner.VrfPubkey != "" {
		pk, perr := hex.DecodeString(miner.VrfPubkey)
		if perr != nil || len(pk) != vrf.PublicKeySize {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_pubkey du mineur invalide on-chain")
		}
		pi, perr := hex.DecodeString(msg.VrfProof)
		if perr != nil || len(pi) != vrf.ProofSize {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "vrf_proof requis (preuve ECVRF sur le defi, 80 octets hex)")
		}
		if ok, _ := vrf.Verify(pk, []byte(cur), pi); !ok {
			return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "preuve VRF invalide pour ce defi")
		}
	}

	epoch := sdk.UnwrapSDKContext(ctx).BlockHeight() / eb
	if err := k.Available.Set(ctx, collections.Join(epoch, msg.MinerId)); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgProveAvailabilityResponse{}, nil
}
