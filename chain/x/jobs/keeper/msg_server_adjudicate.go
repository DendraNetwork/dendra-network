package keeper

import (
	"context"
	"strconv"
	"strings"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// AdjudicateDispute (INT-1 v0 inc.4) — clôt une dispute de façon PERMISSIONLESS / TRUSTLESS, à la place de
// l'autorité interim de ResolveDispute (inc.2). Personne n'a besoin de la gouvernance : la chaîne relit les
// RE-COMMITS d'un comité FRAIS et tranche elle-même.
//
// Mécanisme (cf. docs/DISPUTE-FRAUDPROOF.md §8) :
//   - Après la fenêtre `dispute_window`, un comité FRAIS a re-exécuté le job off-chain et ancré ses résultats
//     sous la clé "<jobId>__redo__<minerId>" (via le CreateCommit existant, signé par chaque opérateur).
//   - On ne compte QUE des mineurs ENREGISTRÉS (stake réel = anti-sybil) et HORS comité d'origine
//     (re-dérivable du beacon → indépendance / anti-collusion).
//   - VERDICT pondéré par le STAKE (splitter son stake ne change pas l'influence) : un mineur slashé dont le
//     commit ORIGINAL recueille la MAJORITÉ STRICTE du stake frais était en fait CORRECT → on INVERSE son
//     slash (restitution depuis la Trésorerie, comme inc.3) et on rembourse+récompense le disputeur (comme
//     inc.2). Sinon → le bond du disputeur part en Trésorerie (anti-grief).
//
// Honnête : la confiance passe de « majorité honnête du comité » à « ≥1 honnête bondé + comité frais non
// colludé » ; si le comité frais collude AUSSI on retombe sur l'hypothèse de majorité (à l'échelle, deux
// comités indépendants colludés = bien plus dur). DORMANT par défaut (`dispute_window=0`).
func (k msgServer) AdjudicateDispute(ctx context.Context, msg *types.MsgAdjudicateDispute) (*types.MsgAdjudicateDisputeResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	p, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
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
	// Fenêtre de re-commit : laisse le temps au comité frais de re-soumettre ses résultats off-chain.
	if p.DisputeWindow > 0 && sdk.UnwrapSDKContext(ctx).BlockHeight() < job.DisputeHeight+int64(p.DisputeWindow) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "fenetre de re-adjudication non ecoulee")
	}

	// Comité d'ORIGINE (re-dérivable du beacon) → EXCLU du tally frais.
	orig, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	// ADR-033 — l'ensemble votant du comité FRAIS doit être ANCRÉ, comme celui du tally de verdicts
	// (ADR-032). Sans cette lecture, « être un mineur enregistré hors comité d'origine » suffisait à
	// voter : 3 identités à `min_stake` détenaient 100 % de `totalRedoStake` et pouvaient AUSSI BIEN
	// faire slasher un honnête QUE faire restituer le slash d'un vrai tricheur depuis la Trésorerie.
	// Absence d'ancre = fail-closed total sur ce chemin (le timeout d'audit reste la voie de sortie).
	allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, msg.JobId)

	// RE-COMMITS du comité frais (clé "<jobId>__redo__<minerId>") : mineurs TIRÉS ET ANCRÉS, HORS origine, pondérés stake.
	redoPrefix := msg.JobId + "__redo__"
	type freshVote struct {
		vec   []int64
		stake uint64
	}
	fresh := []freshVote{}
	var totalRedoStake uint64
	if err := k.Commit.Walk(ctx, commitRange(redoPrefix), func(key string, c types.Commit) (bool, error) {
		if !redoAnchored {
			return true, nil // aucun comité tiré -> aucun vote recevable (fail-closed)
		}
		if !strings.HasPrefix(key, redoPrefix) {
			return false, nil
		}
		mid := strings.TrimPrefix(key, redoPrefix)
		if !allowedRedo[mid] {
			return false, nil // ADR-033 : NON TIRÉ -> ne vote pas (le sybil ne s'invite plus)
		}
		if orig[mid] {
			return false, nil // exclut le comité d'origine (anti-collusion)
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // doit être un mineur enregistré (stake réel = anti-sybil)
		}
		v := parseIntVec(c.ResultCommit)
		if v == nil {
			return false, nil
		}
		fresh = append(fresh, freshVote{vec: v, stake: m.Stake})
		totalRedoStake += m.Stake
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// ADR-026 (J3) + ADR-028 — VERDICTS du comité frais (clé "<jobId>__verdict__<minerId>", LLM-as-juge).
	// PLANCHER requis (≥ effectiveSlashFloor(p) voteurs distincts, pondéré stake) : on ne slashe JAMAIS sous le
	// plancher de participation (anti faux-positif / non-déterminisme cross-GPU / anti-sybil). Sous le plancher :
	// ni re-commits suffisants ni verdicts -> l'adjudication N'aboutit PAS ici (le timeout d'audit tranchera :
	// clawback si primaire muet).
	verdictCheated, _, verdictVoters, verdictInvalid, verdictSeats := k.auditVerdictTally(ctx, msg.JobId, job.MinerId)
	verdictCanSlash := verdictVoters >= effectiveSlashFloor(p) // ADR-028 v2 : plancher de participation EFFECTIF (gouverné si audit_min_quorum>0, sinon repli v1) pour un slash dur

	// HAUT-3 (internal audit 07-20) — LE DÉNOMINATEUR DES RE-COMMITS EST RELATIF AUX SIÈGES ANCRÉS, PAS À 3.
	//
	// Le gate valait `len(fresh) >= CommitteeSize` = 3, quel que soit le nombre de sièges TIRÉS (15).
	// Trois répondants sur quinze — 20 % des sièges — prononçaient donc un slash dur OU une restitution
	// depuis la Trésorerie, et la majorité portait sur `totalRedoStake` = le stake des seuls répondants.
	// C'est EXACTEMENT l'erreur de dénominateur qu'ADR-032 amendée a fermée côté verdicts : un seuil
	// juste dont la base a changé sous lui. On applique ici la même règle — quorum de participation à
	// ⌈2/3 des sièges ANCRÉS⌉ — pour que la décision exige une supermajorité du comité convoqué, pas
	// une poignée de volontaires. Plancher absolu à CommitteeSize pour ne jamais descendre sous 3.
	redoSeats := len(allowedRedo)
	redoQuorum := (2*redoSeats + 2) / 3 // ⌈2/3 × sièges ancrés⌉ (arithmétique entière)
	if redoQuorum < CommitteeSize {
		redoQuorum = CommitteeSize
	}
	if len(fresh) < redoQuorum && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite frais sous le quorum (re-commits < 2/3 des sieges ancres et verdicts < plancher de participation) -> le timeout d'audit tranchera")
	}
	if totalRedoStake == 0 && !verdictCanSlash {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite frais sans stake")
	}

	// INSTRUMENTATION K — ÉMETTRE AUSSI SUR LE SUCCÈS (internal audit 07-20, biais de censure).
	//
	// Le timeout n'émet `redo_participation` que pour les disputes qui n'ont PAS atteint le quorum —
	// c'est la définition même du timeout. Mesurer là SEUL revient à n'observer que la queue gauche :
	// toute mesure montrerait `responders < quorum`, non parce que la participation est mauvaise, mais
	// parce que c'est l'échantillon collecté. On conclurait « effondrer K » sur une donnée censurée.
	// Ici, on a franchi le gate : l'adjudication VA committer, donc l'événement survit (pas de rollback).
	// Les deux points réunis donnent la distribution complète, et K se calibre sans biais.
	// `resolved_by` sépare les deux populations à l'analyse.
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
		"redo_participation",
		sdk.NewAttribute("job_id", msg.JobId),
		sdk.NewAttribute("seats", strconv.Itoa(len(allowedRedo))),
		sdk.NewAttribute("responders", strconv.Itoa(len(fresh))),
		sdk.NewAttribute("resolved_by", "adjudication"),
	))

	pools, perr := k.Pools.Get(ctx)
	if perr != nil {
		pools = types.Pools{}
	}

	// VERDICT pondéré stake : un mineur slashé est VINDIQUÉ si son commit ORIGINAL recueille la MAJORITÉ
	// STRICTE du stake frais (totalRedoStake ≤ offre 1e13 udndr → agree*2 ne déborde pas uint64).
	upheld := false
	for _, rec := range job.SlashRecords {
		oc, e := k.Commit.Get(ctx, msg.JobId+"__"+rec.MinerId)
		if e != nil {
			continue
		}
		ov := parseIntVec(oc.ResultCommit)
		if ov == nil {
			continue
		}
		var agree uint64
		for _, fv := range fresh {
			if cosineGE(ov, fv.vec, semanticThresholdBps) {
				agree += fv.stake
			}
		}
		if agree*2 <= totalRedoStake {
			continue // pas de majorité stricte du stake frais → non vindiqué
		}
		// vindiqué → RESTITUTION (compteur : les coins sont déjà dans le module depuis le slash), bornée.
		amt := rec.Amount
		if amt > pools.Treasury {
			amt = pools.Treasury
		}
		if amt > 0 {
			if mm, me := k.Miner.Get(ctx, rec.MinerId); me == nil {
				mm.Stake += amt
				if err := k.Miner.Set(ctx, mm.MinerId, mm); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
				pools.Treasury -= amt
			}
		}
		upheld = true
	}

	// ADR-025 (M4) — RÉSOLUTION D'AUDIT OPTIMISTE. Le règlement k=1 a PAYÉ un primaire SANS le slasher (pas
	// de SlashRecord). Si ce job optimiste est contesté (audit VRF M3, ou dispute humaine), on compare le
	// commit ORIGINAL du primaire à la MAJORITÉ DE STAKE du comité frais. Divergence -> le primaire a été payé
	// pour un faux résultat -> SLASH DUR (SlashLeakBps) + SlashRecord (restituable s'il re-conteste avec succès)
	// + dispute VALIDE. Concordance -> primaire vindiqué (rien). C'est ce qui rend la triche -EV (Nash, ADR-025 §2.5).
	// fee-hold v2 (internal audit 2026-06-21) : on mémorise le verdict optimiste pour CONSOMMER la rétention en fin de
	// résolution (sinon HeldFee/HeldBurn gelés sur ce chemin permissionless — défaut trouvé par audit 2026-06-21).
	optimisticCheated := false
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		cheated := false
		if verdictCanSlash {
			// VETO N=5 (internal audit 2026-06-22) : slash SSI quasi-unanimité d'invalide (COUNT) si gouverné (audit_min_quorum>0), sinon v1 (majorité-stake).
			cheated = auditSlashDecision(p, verdictCheated, verdictVoters, verdictInvalid, verdictSeats)
		} else {
			// repli (M4) : comparaison cosinus du commit ORIGINAL du primaire vs majorité de stake fraîche
			// (atteint seulement avec ≥ CommitteeSize re-commits, garanti par le gate ci-dessus).
			if oc, e := k.Commit.Get(ctx, msg.JobId+"__"+job.MinerId); e == nil {
				if ov := parseIntVec(oc.ResultCommit); ov != nil {
					var agree uint64
					for _, fv := range fresh {
						if cosineGE(ov, fv.vec, semanticThresholdBps) {
							agree += fv.stake
						}
					}
					// ADR-033 — la garde `totalRedoStake > 0` n'est PAS cosmétique : sans elle,
					// `0*2 <= 0` est VRAI, donc un corps électoral VIDE (comité non ancré, ou
					// tiré mais muet) prononçait « le primaire a triché ». Le silence ne
					// condamne pas ; c'est le timeout d'audit qui traite l'absence de réponse.
					cheated = totalRedoStake > 0 && agree*2 <= totalRedoStake // EN DÉSACCORD avec la majorité de stake fraîche
				}
			}
		}
		optimisticCheated = cheated
		if cheated {
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				amt := m.Stake * p.SlashLeakBps / 10000
				m.Stake -= amt
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
				pools.Treasury += amt
				if amt > 0 {
					job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
				}
				upheld = true // l'audit a CONFIRMÉ une triche -> dispute valide
			}
		}
	}

	if upheld {
		// dispute VALIDE : rembourse le bond + récompense le disputeur (lanceur d'alerte), borné Trésorerie restante.
		bond := job.DisputeBond
		reward := bond
		if reward > pools.Treasury {
			reward = pools.Treasury
		}
		if payout := bond + reward; payout > 0 {
			if dBz, de := k.addressCodec.StringToBytes(job.Disputer); de == nil {
				coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(payout)))
				if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(dBz), coins); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
				}
			}
		}
		pools.Treasury -= reward
	} else {
		// dispute REJETÉE par le comité frais : bond → Trésorerie (anti-grief ; coins restent dans le module).
		pools.Treasury += job.DisputeBond
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// fee-hold v2 (internal audit 2026-06-21) — CONSOMMER la rétention sur CE chemin (permissionless) aussi, sinon
	// HeldFee/HeldBurn restent GELÉS après +resolved. Tricheur confirmé -> client remboursé depuis la rétention
	// (held+cut+burn) + Demand reversé (comme slashCheatedPrimary) ; primaire vindiqué -> rétention libérée au
	// mineur + burn brûlé à finalité (comme le timeout). No-op si rien retenu (hold_bps=0).
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		if optimisticCheated {
			if m, me := k.Miner.Get(ctx, job.MinerId); me == nil {
				k.refundRetainedToClient(ctx, &job, p, &m)
				if err := k.Miner.Set(ctx, m.MinerId, m); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
				}
			}
		} else {
			k.releaseHeld(ctx, &job)
		}
	}
	job.State = job.State + "+resolved"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgAdjudicateDisputeResponse{}, nil
}
