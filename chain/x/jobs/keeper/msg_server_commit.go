package keeper

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// CreateCommit -- H1 : LIAISON DE SIGNATURE (anti-usurpation). La cle d'un commit est
// "<jobId>__<minerId>". Seul l'OPERATEUR du mineur (== signataire de la tx) peut ancrer le commit
// de ce mineur. Sans ca, n'importe qui pourrait ancrer une preuve au nom d'un autre et fausser le
// verdict. Couple a L5 (chaque mineur a sa propre cle), ca verrouille l'identite des preuves.
func (k msgServer) CreateCommit(ctx context.Context, msg *types.MsgCreateCommit) (*types.MsgCreateCommitResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, fmt.Sprintf("invalid address: %s", err))
	}

	// 1) extraire le minerId de la cle "<jobId>__<minerId>"
	idx := strings.LastIndex(msg.JobId, "__")
	if idx < 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"cle de commit invalide (attendu \"<jobId>__<minerId>\")")
	}
	minerId := msg.JobId[idx+2:]

	// H6 (révélation différée) : si le comité de CE job n'est pas encore révélé (beacon présent mais
	// graine vide), refuser le commit -- sinon assignedCommittee retomberait sur une graine grindable.
	if b, berr := k.Beacon.Get(ctx, msg.JobId[:idx]); berr == nil && b.Seed == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"comite pas encore revele (revelation differee) : reessayez apres le bloc de revelation")
	}

	// 2) le mineur doit exister, et le signataire doit etre SON operateur
	miner, err := k.Miner.Get(ctx, minerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "mineur inconnu pour ce commit")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if msg.Creator != miner.Operator {
		return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized,
			"seul l'operateur du mineur peut ancrer son commit")
	}

	// 3) refuser l'ecrasement d'un commit existant
	ok, err := k.Commit.Has(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	} else if ok {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "index already set")
	}

	// Incrément C : si le registre de modèles est APPLIQUÉ (param gov, OFF par défaut), le modèle
	// servi DOIT être enregistré + actif. OFF -> ne casse pas le devnet sans modèles enregistrés.
	if params, perr := k.Params.Get(ctx); perr == nil && params.EnforceModelRegistry {
		if strings.TrimSpace(msg.ModelId) == "" {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "model_id requis (registre de modeles applique)")
		}
		if !k.modelRegistryKeeper.IsActive(ctx, msg.ModelId) {
			return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "modele non enregistre ou inactif")
		}
		// NEW-MR-03 (audit v5) -- LIAISON model_id <-> ARTEFACT. Le commit porte le weights_hash du
		// modele REELLEMENT servi ; on le compare au weights_sha256 ANCRE au registre. Un mineur ne
		// peut donc plus declarer un model_id et servir autre chose sans soit diverger du comite (slash
		// semantique), soit presenter un weights_hash != registre (refus ici). HONNETE : ceci LIE la
		// declaration a un artefact -- ce n'est PAS une preuve d'execution (modele optimiste/statistique
		// assume pour le GPU grand public, cf. ADR confidentialite). Gate : seulement si le registre a un
		// weights_sha256 NON VIDE pour ce modele (sinon on n'exige rien -> retro-compat devnet).
		if want, ok := k.modelRegistryKeeper.ExpectedWeights(ctx, msg.ModelId); ok && strings.TrimSpace(want) != "" {
			if strings.TrimSpace(msg.WeightsHash) == "" {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "weights_hash requis (registre applique avec ancre de poids)")
			}
			if !strings.EqualFold(strings.TrimSpace(msg.WeightsHash), strings.TrimSpace(want)) {
				return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "weights_hash != poids enregistres pour ce model_id")
			}
		}
	}

	commit := types.Commit{
		Creator:      msg.Creator,
		JobId:        msg.JobId,
		PromptCommit: msg.PromptCommit,
		ResultCommit: msg.ResultCommit,
		Kind:         msg.Kind,
		ModelId:      msg.ModelId,
		WeightsHash:  msg.WeightsHash,
	}
	if err := k.Commit.Set(ctx, commit.JobId, commit); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgCreateCommitResponse{}, nil
}

// NEW-GO-32 (audit v2) — NEUTRALISÉ. Un commit ancré est IMMUABLE (c'est le fondement de H1). Sinon un
// opérateur observe les commits PUBLICS des autres puis réécrit le sien pour s'aligner sur la majorité
// (échapper au slash) ou le supprime pour disparaître du tally → verdict on-chain faussable à volonté.
// Seul `CreateCommit` ancre (une fois, signé par l'opérateur). Toute correction = via dispute gaté gov.
func (k msgServer) UpdateCommit(ctx context.Context, msg *types.MsgUpdateCommit) (*types.MsgUpdateCommitResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "commit IMMUABLE (H1) : un resultat ancre ne peut etre reecrit")
}

func (k msgServer) DeleteCommit(ctx context.Context, msg *types.MsgDeleteCommit) (*types.MsgDeleteCommitResponse, error) {
	return nil, errorsmod.Wrap(sdkerrors.ErrUnauthorized, "commit IMMUABLE (H1) : suppression interdite (evasion du tally)")
}
