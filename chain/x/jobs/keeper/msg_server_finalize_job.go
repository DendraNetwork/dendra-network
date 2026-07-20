package keeper

import (
	"context"
	"errors"
	"sort"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// FinalizeJob -- VERDICT ON-CHAIN (L1) borne au COMITE ASSIGNE (L3). N'importe qui relaie ; la
// chaine ne se fie pas a l'appelant : elle lit les commits ancres du job, NE garde que ceux des
// mineurs ASSIGNES par sa propre regle (assignedCommittee), calcule l'honnete-majorite de facon
// DETERMINISTE, et slashe les divergents (SlashLeakBps -> Treasury). Un mineur non assigne ne peut
// donc pas injecter de vote pour fausser le verdict.
func (k msgServer) FinalizeJob(ctx context.Context, msg *types.MsgFinalizeJob) (*types.MsgFinalizeJobResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize) // L3
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// H4 + NEW-GO-33 (audit v2) : le job doit EXISTER (ouvert via OpenJob) AVANT tout slash — sinon
	// grief : un attaquant slashe le bond RÉEL d'un mineur honnête sur un job FANTÔME au prix d'une tx.
	// Puis anti-rejeu : un job déjà finalisé ne peut pas être re-finalisé (pas de double-slash).
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job inexistant -> finalisation/slash refuses")
	}
	if strings.Contains(job.State, "finalized") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja finalise")
	}

	prefix := msg.JobId + "__"
	commitByMiner := map[string]string{}
	tally := map[string]int{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] { // seuls les mineurs ASSIGNES votent
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
	// GO-06 PROPAGÉ — `verify_semantic`/`settle_semantic` exigent déjà le comité
	// COMPLET ; ce chemin-ci, qui SLASHE, se contentait d'`au moins un commit`. Conséquence : avec 2
	// commits sur 3 en désaccord (1-1), le « canonique » était tranché par l'ORDRE LEXICOGRAPHIQUE
	// des chaînes de commit — un membre du comité pouvait donc commiter, attendre un seul pair
	// honnête, appeler FinalizeJob avant le troisième, et lui faire perdre `slash_leak_bps` de son
	// bond sur un tirage de dés alphabétique. On exige désormais une MAJORITÉ STRICTE du comité
	// ASSIGNÉ (pas des présents) : une égalité ne slashe plus personne, et un absent ne fait pas
	// quorum à lui seul. Liveness préservée : 2 accords sur 3 suffisent.
	if best*2 <= CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de majorite stricte du comite assigne -> finalisation/slash refuses (egalite ou comite trop incomplet)")
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			continue
		}
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		amt := miner.Stake * params.SlashLeakBps / 10000
		miner.Stake -= amt
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		pools.Treasury += amt
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// H4 + GO-10 (NEW-GO-33) : marque finalise (append -> ne perd pas un eventuel "+paid") ; le job
	// existe forcément ici (vérifié plus haut) et l'erreur de Set est VÉRIFIÉE (anti-rejeu fiable).
	job.State = job.State + "+finalized"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage +finalized : "+err.Error())
	}
	return &types.MsgFinalizeJobResponse{}, nil
}
