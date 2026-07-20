package keeper

import (
	"context"
	"fmt"
	"math/big"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// OpenJob -- ESCROW (H3) + BEACON (H6). Le client DEPOSE `fee` token sur le compte de MODULE "jobs"
// (escrow, il ne peut plus se defiler), et la chaine FIXE un BEACON imprevisible (hauteur+temps du
// bloc, decides par le consensus) pour ce job. Le comite assigne derivera de ce beacon (anti-grinding :
// le createur ne peut pas choisir le jobId pour obtenir un comite complice).
func (k msgServer) OpenJob(ctx context.Context, msg *types.MsgOpenJob) (*types.MsgOpenJobResponse, error) {
	clientBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// GO-09 : "__" est le separateur des cles Commit (jobId__minerId) -> l'interdire dans jobId
	// (sinon l'extraction du minerId via LastIndex se casse -> mauvaise attribution de vote/slash).
	if msg.JobId == "" || strings.Contains(msg.JobId, "__") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "jobId invalide (non vide, sans '__')")
	}

	// Params chargés UNE fois (réutilisés par la garde Nash M5 et le délai de révélation H6 plus bas).
	params, perr := k.Params.Get(ctx)
	// M5 (ADR-025) — GARDE NASH (mode optimiste seulement). Tricher doit rester -EV : avec un audit de taux
	// s=audit_sample_bps et un slash P=slash_leak_bps·stake, l'inégalité de Nash s·P > (1-s)·g (g≈fee) impose
	//     audit_sample_bps·slash_leak_bps·min_stake > (10000-audit_sample_bps)·10000·fee.
	// Référence = min_stake (plancher : tout primaire a stake ≥ min_stake -> garde CONSERVATRICE). big.Int =
	// anti-overflow (produits ~1e21). Fail-safe : mode 1 sans audit (sample 0) -> lhs=0 -> refuse tout job.
	// DORMANT : aucun effet en mode 0 (défaut).
	if perr == nil && params.VerificationMode == 1 {
		// (audit ADR-025) calcul de (10000 - s) SÛR à l'underflow : si s>=10000 (Validate l'interdit, mais
		// défense en profondeur dans le chemin consensus), 1-s = 0 -> rhs=0 -> garde toujours satisfaite
		// (audit ~100% -> Nash trivialement tenu).
		oneMinusS := uint64(0)
		if params.AuditSampleBps < 10000 {
			oneMinusS = 10000 - params.AuditSampleBps
		}
		lhs := new(big.Int).Mul(new(big.Int).SetUint64(params.AuditSampleBps), new(big.Int).SetUint64(params.SlashLeakBps))
		lhs.Mul(lhs, new(big.Int).SetUint64(params.MinStake))
		rhs := new(big.Int).Mul(new(big.Int).SetUint64(oneMinusS), big.NewInt(10000))
		rhs.Mul(rhs, new(big.Int).SetUint64(msg.Fee))
		if lhs.Cmp(rhs) <= 0 {
			return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
				"fee trop élevée pour l'équilibre de Nash optimiste (ADR-025 §2.5) : relever min_stake ou baisser la fee")
		}
	}

	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(msg.Fee)))
	if err := k.bankKeeper.SendCoinsFromAccountToModule(ctx, sdk.AccAddress(clientBz), types.ModuleName, coins); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
	}

	// H6 -- BEACON ANTI-GRINDING. Le comité dérive du beacon ; le créateur ne doit pas pouvoir choisir
	// le jobId pour obtenir un comité complice. Deux régimes selon le param committee_reveal_delay :
	//   - delay == 0 (défaut) : graine figée à l'open (hauteur:temps). PRÉVISIBLE -> devnet seulement.
	//     Conserve le comportement existant (boucle e2e intacte).
	//   - delay  > 0 : RÉVÉLATION DIFFÉRÉE. La graine reste VIDE ; l'EndBlocker la figera à H+delay à
	//     partir de l'AppHash de ce bloc futur, IMPRÉVISIBLE au moment de l'open -> grinding inopérant.
	//     Les commits sont refusés tant que la graine n'est pas révélée (cf. CreateCommit + assignedCommittee).
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	delay := uint64(0)
	if perr == nil {
		delay = params.CommitteeRevealDelay
	}
	if delay == 0 {
		seed := k.committeeBaseSeed(ctx, fmt.Sprintf("%d:%d", sdkCtx.BlockHeight(), sdkCtx.BlockTime().UnixNano()))
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: seed}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	} else {
		if err := k.Beacon.Set(ctx, msg.JobId, types.Beacon{JobId: msg.JobId, Seed: ""}); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		revealH := sdkCtx.BlockHeight() + int64(delay)
		if err := k.PendingReveal.Set(ctx, collections.Join(revealH, msg.JobId)); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}

	job := types.Job{JobId: msg.JobId, Client: msg.Creator, MinerId: "", Fee: msg.Fee, State: "open"}
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgOpenJobResponse{}, nil
}
