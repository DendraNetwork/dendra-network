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

// ⚠️ SettleJob EST UN VESTIGE v1, ET IL EST DANGEREUX SANS GARDE.
//
// Ce handler règle un job — marque `+settled`, crédite les pools, incrémente la demande — sur la SEULE
// foi du format de l'adresse du signataire : aucun commit lu, aucun comité, aucune vérification. Le
// mode optimiste règle par `SettleSemantic`/`settleOptimistic`, le mode redondant par `SettleSemantic`
// aussi ; la stack de production n'appelle JAMAIS `settle-job` (seuls des scripts de démo legacy le
// font). Laissé permissionless, n'importe qui appelait `SettleJob` sur n'importe quel job non réglé,
// posait `+settled`, et TOUS les vrais chemins de règlement refusaient ensuite « job deja regle » :
// l'escrow restait verrouillé au module POUR TOUJOURS, le mineur n'était jamais payé, et les compteurs
// de pools étaient crédités comme s'il l'avait été (divergence comptable). Coût = le gas, rejouable
// sur chaque job dès son ouverture. C'est la forme la plus pure de « capital déplacé sur aucune base
// authentifiée ».
//
// Fermé en le réservant à l'AUTORITÉ, comme `SlashMiner`/`ResolveDispute` : un tiers ne peut plus
// geler un escrow. CANDIDAT AU RETRAIT COMPLET à la prochaine régén proto (internal audit) — un RPC de
// règlement brut n'a plus de raison d'exister à côté des chemins vérifiés ; le garder gaté est la
// mesure prudente en attendant que le retrait du proto soit fait et testé.
func (k msgServer) SettleJob(ctx context.Context, msg *types.MsgSettleJob) (*types.MsgSettleJobResponse, error) {
	creatorBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}

	job, err := k.Job.Get(ctx, msg.JobId)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not found")
		}
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if jobIsPaid(job.State) { // NEW-GO-35 : anti-rejeu UNIFIÉ (paid||settled) -> pas de re-règlement ni d'inflation de Demand
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja regle")
	}
	// GATE D'AUTORITÉ — placé APRÈS l'anti-rejeu à dessein : les chemins déjà réglés répondent « job
	// deja regle » (le test d'anti-rejeu unifié l'exige), et seul un job NON réglé atteint cette garde.
	if !bytes.Equal(k.GetAuthority(), creatorBz) {
		return nil, errorsmod.Wrap(types.ErrInvalidSigner, "settle-job (vestige v1) réservé à l'autorité ; utiliser settle-semantic pour un règlement vérifié")
	}

	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	cut := job.Fee * params.ProtocolFeeBps / 10000
	minerGross := job.Fee - cut
	minerLocked := minerGross * params.MinerVestBps / 10000
	minerLiquid := minerGross - minerLocked
	validators := cut * params.ValidatorRewardBps / 10000
	team := cut * params.TeamFeeBps / 10000
	treasury := cut - validators - team

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	pools.MinerPaid += minerLiquid
	pools.MinerLocked += minerLocked
	pools.Validators += validators
	pools.Team += team
	pools.Treasury += treasury
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// demande NON-RECUPERABLE (ADR-017): tresorerie + equipe, SAUF self-dealing (client == operateur)
	if miner, mErr := k.Miner.Get(ctx, job.MinerId); mErr == nil {
		if job.Client != miner.Operator {
			miner.Demand += treasury + team
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
		}
	}

	job.State = job.State + "+settled" // APPEND (ne PAS écraser les marqueurs existants : +verified/+finalized)
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	return &types.MsgSettleJobResponse{}, nil
}
