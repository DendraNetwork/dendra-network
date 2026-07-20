package keeper

import (
	"bytes"
	"context"

	"cosmossdk.io/collections"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// DisputeVerdict (INT-1 v0 — design docs/DISPUTE-FRAUDPROOF.md) — conteste le verdict d'un job REGLE, de
// façon PERMISSIONLESS et BONDEE (anti-grief). Sort du « honest-majority » : le `report-divergence` existant
// est gaté GOUVERNANCE ; ici N'IMPORTE QUI peut contester en posant un bond.
//
// INCREMENT 1 = la PRIMITIVE : validation + escrow du bond + marquage de l'état (`...+disputed`). La
// RE-ADJUDICATION (ré-assignation d'un comité FRAIS via le beacon -> re-settle sémantique -> slash des
// fautifs/refund, ou slash du bond si la dispute échoue) = increment 2.
//
// DORMANT par défaut : `dispute_window == 0` -> disputes DÉSACTIVÉES -> e2e/devnet strictement inchangés.
func (k msgServer) DisputeVerdict(ctx context.Context, msg *types.MsgDisputeVerdict) (*types.MsgDisputeVerdictResponse, error) {
	p, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if p.DisputeWindow == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "disputes desactivees (dispute_window=0)")
	}
	disputerBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job inconnu")
	}
	// On ne conteste qu'un job REGLE (un verdict existe), et UNE seule fois (anti-rejeu de dispute).
	if !jobIsPaid(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "on ne conteste qu'un job regle (paid/settled)")
	}
	if jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja conteste (anti-rejeu)")
	}
	// BOND (anti-grief) : le disputeur escrowe `dispute_bond` udndr sur le compte de MODULE "jobs"
	// (même mécanisme prouvé que l'escrow de la fee dans OpenJob). Perdre la dispute = perdre le bond (increment 2).
	if p.DisputeBond > 0 {
		bond := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(p.DisputeBond)))
		if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(disputerBz), types.ModuleName, bond); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}
	job.Disputer = msg.Creator
	job.DisputeBond = p.DisputeBond
	job.DisputeHeight = sdk.UnwrapSDKContext(ctx).BlockHeight()
	job.State = job.State + "+disputed"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// ADR-033 — ANCRAGE DU COMITÉ DE RE-ADJUDICATION, ICI et pas plus tard. La graine est celle de
	// la hauteur d'ouverture : postérieure aux commits d'origine (donc non grindable par le mineur)
	// ET antérieure aux re-commits (donc le contestataire ne peut pas choisir ses juges après coup).
	// Non bloquant : une erreur de tirage laisse l'ancre absente, et `AdjudicateDispute` refuse
	// alors de bouger le moindre coin (fail-closed) — le timeout d'audit tranchera.
	// MARQUE D'ORIGINE. Une fois le job en `+disputed`, plus rien ne dit si c'est l'échantillonnage
	// VRF ou un humain qui l'a ouvert — et la résolution par échéance doit trancher différemment dans
	// les deux cas (cf. `humanDisputeKey`). On l'écrit donc au moment où l'information existe encore.
	if err := k.AuditCommittee.Set(ctx, humanDisputeKey(job.JobId), msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if err := k.anchorRedoCommittee(ctx, job.JobId, job.MinerId, msg.Creator); err != nil {
		sdk.UnwrapSDKContext(ctx).Logger().Error("SECURITE: comite de re-adjudication NON ancre -> adjudication fail-closed sur ce job (le timeout d'audit tranchera).", "job_id", job.JobId, "err", err.Error())
	}
	// LIVENESS — ÉCHÉANCE AUTOMATIQUE. `runOptimisticAudit` programme `PendingAuditResolve` pour les
	// jobs qu'IL échantillonne ; une dispute HUMAINE sur un job jamais audité n'en programmait AUCUNE.
	// Sa seule sortie était `AdjudicateDispute`, qui exige maintenant un comité TIRÉ : si le vivier est
	// trop petit (il vaut N-5 : hors primaire, hors disputeur, hors les 3 d'origine) ou si les tirés se
	// taisent, le job restait `+disputed` POUR TOUJOURS — alors que `audit_sampling.go` affirme
	// « aucun job ne reste +disputed indéfiniment ». Le durcissement ADR-033 rendait ce cas structurel,
	// donc il se paie ici : toute dispute a une échéance, et `resolveDisputedAudit` (déjà éprouvé)
	// tranche. Doublon inoffensif si le job était aussi échantillonné : la résolution est idempotente
	// (`jobIsResolved`).
	if p.AuditResolveTimeout > 0 {
		h := sdk.UnwrapSDKContext(ctx).BlockHeight()
		if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+int64(p.AuditResolveTimeout), job.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	return &types.MsgDisputeVerdictResponse{}, nil
}

// ResolveDispute (INT-1 v0 inc.2) — résout une dispute OUVERTE et règle le BOND.
// INTERIM : gaté AUTORITÉ (gov), comme le report-divergence. L'auto-résolution PERMISSIONLESS par comité
// FRAIS (re-run via beacon -> re-settle sémantique -> slash des fautifs + reversement du slash original) =
// increment 3 (nécessite la ré-exécution off-chain + l'enregistrement du verdict original). Ici on clôt le
// cycle de dispute avec l'économie du bond :
//   - upheld=true  (dispute VALIDE)  : le bond est REMBOURSÉ au disputeur + RÉCOMPENSE depuis la Trésorerie
//                  (bornée par son solde) — il avait raison de contester.
//   - upheld=false (dispute REJETÉE) : le bond est SLASHÉ vers la Trésorerie (anti-grief).
// Anti-rejeu : une seule résolution par dispute (marqueur "resolved").
func (k msgServer) ResolveDispute(ctx context.Context, msg *types.MsgResolveDispute) (*types.MsgResolveDisputeResponse, error) {
	authBz, err := k.addressCodec.StringToBytes(msg.Authority)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid authority address")
	}
	if !bytes.Equal(k.GetAuthority(), authBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "resolve-dispute reserve a l'autorite (interim ; auto-resolution comite frais = increment 3)")
	}
	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job inconnu")
	}
	if !jobIsDisputed(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "aucune dispute ouverte pour ce job")
	}
	if jobIsResolved(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "dispute deja resolue (anti-rejeu)")
	}
	pools, perr := k.Pools.Get(ctx)
	if perr != nil {
		pools = types.Pools{}
	}
	bond := job.DisputeBond
	if msg.Upheld {
		// INT-1 v0 inc.3 : RESTITUTION d'abord — le verdict contesté était FAUX, on INVERSE les slashs
		// enregistrés au règlement. Re-crédite le stake de chaque mineur slashé (simple compteur : les coins
		// sont déjà dans le module depuis le slash) ; borné par la Trésorerie ; non-rejouable via "resolved".
		for _, rec := range job.SlashRecords {
			amt := rec.Amount
			if amt > pools.Treasury {
				amt = pools.Treasury
			}
			if amt == 0 {
				continue
			}
			m, mErr := k.Miner.Get(ctx, rec.MinerId)
			if mErr != nil {
				continue
			}
			m.Stake += amt
			if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
			pools.Treasury -= amt
		}
		// dispute VALIDE : rembourse le bond + récompense (bornée par la Trésorerie RESTANTE). Coins servis
		// depuis le module (qui détient l'escrow) ; le compteur Trésorerie est décrémenté de la récompense.
		reward := bond
		if reward > pools.Treasury {
			reward = pools.Treasury
		}
		if payout := bond + reward; payout > 0 {
			disputerBz, derr := k.addressCodec.StringToBytes(job.Disputer)
			if derr != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "disputer address")
			}
			coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(payout)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(disputerBz), coins); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
			}
		}
		pools.Treasury -= reward
	} else {
		// dispute REJETÉE : bond -> Trésorerie (anti-grief ; les coins restent dans le module).
		pools.Treasury += bond
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	job.State = job.State + "+resolved"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgResolveDisputeResponse{}, nil
}
