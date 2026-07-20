package keeper

import (
	"context"
	"sort"
	"strings"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// Payout -- verse aux mineurs de la MAJORITE honnete ASSIGNEE, DEPUIS l'escrow du module.
//
// CORRIGE (audit 2026-06-10) :
//
//	GO-02 : montant DERIVE de l'escrow de CE job (job.Fee / nb gagnants), borne Σ <= job.Fee
//	        -> plus de drain inter-jobs via un msg.Amount libre.
//	GO-03 : msg.Amount n'est plus utilise (a retirer du proto a la prochaine regen).
//	GO-10 : l'erreur de k.Job.Set est verifiee (sinon flag +paid non ecrit -> rejeu).
//	PY-05/TK-06 : le SURPLUS d'escrow (job.Fee - total paye) est REMBOURSE au client.
//	(Anti-rejeu existant conserve : job ouvert + non "paid".)
func (k msgServer) Payout(ctx context.Context, msg *types.MsgPayout) (*types.MsgPayoutResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job non ouvert (pas d'escrow) -> open-job d'abord")
	}
	if jobIsPaid(job.State) { // GO-08 : anti-rejeu de règlement UNIFIÉ (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja paye")
	}

	prefix := msg.JobId + "__"
	commitByMiner := map[string]string{}
	tally := map[string]int{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				commitByMiner[mid] = c.ResultCommit
				tally[c.ResultCommit]++
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(commitByMiner) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "aucun commit de mineur assigne pour ce job")
	}
	// GO-06 PROPAGÉ — même règle que `verify_semantic`/`settle_semantic`, qui
	// l'appliquaient déjà : sans comité COMPLET, le PREMIER membre à commiter puis à relayer ce
	// message encaissait 100 % de l'escrow aux dépens de ses deux pairs, avant même qu'ils aient pu
	// soumettre. Le correctif GO-06 avait été posé sur deux fichiers et pas sur les trois autres.
	if len(commitByMiner) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite incomplet -> paiement refuse")
	}

	commits := make([]string, 0, len(tally))
	for c := range tally {
		commits = append(commits, c)
	}
	sort.Strings(commits)
	canonical, best := "", -1
	for _, c := range commits {
		if tally[c] > best {
			best, canonical = tally[c], c
		}
	}

	// GO-02/GO-03 : gagnants = commit == canonical ; part DERIVEE de l'escrow (Σ <= job.Fee).
	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	winners := make([]string, 0, len(mids))
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			winners = append(winners, mid)
		}
	}
	if len(winners) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de majorite -> rien a payer")
	}
	// v5 : BURN DOUX réel (déflation). On brûle FeeBurnBps de l'escrow puis on répartit le RESTE aux
	// gagnants -> la « burn 5% » du modèle n'est plus un compteur mais une VRAIE destruction de coins
	// (supply globale ↓). Conservation : burn + payé + surplus rendu = job.Fee.
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	distributable := job.Fee - burnAmt
	per := distributable / uint64(len(winners))
	if per == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow insuffisant pour le nombre de gagnants")
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(per)))
	for _, mid := range winners {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
		if aErr != nil {
			continue
		}
		if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}

	// BURN réel depuis l'escrow du module (déflation v5 ; supply globale ↓).
	if burnAmt > 0 {
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}

	// PY-05/TK-06 : rembourse le surplus d'escrow au client (sinon le delta reste piege).
	if surplus := distributable - per*uint64(len(winners)); surplus > 0 {
		if cliBz, e := k.addressCodec.StringToBytes(job.Client); e == nil {
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(surplus)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), refund); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund surplus: "+err.Error())
			}
		}
	}

	// GO-10 : erreur de Set verifiee (anti-rejeu fiable).
	job.State = job.State + "+paid"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage +paid : "+err.Error())
	}
	return &types.MsgPayoutResponse{}, nil
}
