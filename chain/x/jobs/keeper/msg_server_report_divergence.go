package keeper

import (
	"bytes"
	"context"
	"errors"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// ReportDivergence — vérification S2 (ADR-020) : un comité/vérificateur signale que le résultat du
// mineur (miner_commit) diverge du résultat canonique (correct_commit). Divergence prouvée -> slash
// du mineur du job ; le montant alimente la trésorerie (indemnisation).
//
// NB démo : la "preuve" (honnête-majorité / cosinus / canari) est ici fournie par l'appelant ; en
// prod, ce message est gaté par le module de vérification (vote on-chain + oracle sémantique).
func (k msgServer) ReportDivergence(ctx context.Context, msg *types.MsgReportDivergence) (*types.MsgReportDivergenceResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// GO-13 (durcissement) : la "preuve" de divergence est ici FOURNIE PAR L'APPELANT (msg.*Commit) —
	// fabricable. Avec un bond en VRAIS coins, laisser ce handler permissionless = grief (faux
	// signalement -> slash + destruction du bond d'un honnête). INTERIM : réservé à l'AUTORITÉ (gov).
	// Le slash automatique légitime passe par le comité (verify/settle). FOLLOW-UP : version
	// permissionless GATÉE par une vérification ON-CHAIN (commit réel du mineur vs canonique du comité).
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "report-divergence réservé à l'autorité (gov) en attendant la preuve on-chain")
	}

	// pas de divergence -> rien à faire (le mineur a rendu le bon résultat)
	if msg.MinerCommit == msg.CorrectCommit {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de divergence (commits identiques)")
	}

	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	miner, err := k.Miner.Get(ctx, job.MinerId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "miner not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// divergence prouvée -> slash (slash_leak_bps) ; le montant va à la trésorerie
	amt := miner.Stake * params.SlashLeakBps / 10000
	miner.Stake -= amt
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.Treasury += amt
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgReportDivergenceResponse{}, nil
}
