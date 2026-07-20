package keeper

import (
	"context"
	"errors"
	"sort"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// SettleSemantic -- reglement SEMANTIQUE free-form depuis l'ESCROW + slash des outliers, en UN handler.
//
// CORRIGE (audit 2026-06-10) :
//
//	GO-03 : seuil = constante gouvernee (semanticThresholdBps) ; reward DERIVE de job.Fee
//	        (plus de msg.ThresholdBps / msg.Reward libres aux mains de l'appelant).
//	GO-06 : comite COMPLET + majorite STRICTE ; sans majorite stricte -> ni paiement ni slash.
//	GO-10 : erreur de Set verifiee. PY-05/TK-06 : surplus d'escrow rembourse au client.
//	(cosineGE/parseIntVec partages avec verify_semantic : deja big.Int + bornes -> GO-05/GO-15.)
//	(Anti-rejeu existant conserve : job ouvert + non "paid".)
func (k msgServer) SettleSemantic(ctx context.Context, msg *types.MsgSettleSemantic) (*types.MsgSettleSemanticResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job non ouvert (pas d'escrow)")
	}
	if jobIsPaid(job.State) { // GO-08 : anti-rejeu de règlement UNIFIÉ (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja regle")
	}
	// F1 (audit A→Z 2026-07-10, arbitrage internal audit) — ANTI-DOUBLE-SLASH : un job déjà FINALISÉ
	// (FinalizeJob a rendu le verdict k=3 et slashé les divergents) ne repasse PAS par SettleSemantic :
	// les commits ancrés sont IMMUABLES, donc toujours divergents -> il re-slasherait les MÊMES outliers
	// (double peine composée, les 2 handlers étant permissionless). Garde CIBLÉE sur CE handler SEUL :
	// le flux documenté FinalizeJob -> Payout (verdict puis paiement, cf. job_state.go:13-14 — Payout ne
	// slashe pas) reste OUVERT. Couvre les deux modes (posée avant le branchement verification_mode).
	if strings.Contains(job.State, "finalized") {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "verdict deja rendu (FinalizeJob) ; paiement via Payout")
	}

	// M2 (ADR-025) — RÈGLEMENT OPTIMISTE k=1. DORMANT : atteint UNIQUEMENT si la gouvernance a basculé
	// verification_mode=1. En mode 0 (défaut) on poursuit directement le chemin redondant k=3 ci-dessous,
	// STRICTEMENT inchangé (mêmes tests qu'avant).
	if params.VerificationMode == 1 {
		return k.settleOptimistic(ctx, job, params)
	}

	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	vecs := map[string][]int64{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				if v := parseIntVec(c.ResultCommit); v != nil {
					vecs[mid] = v
				}
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(vecs) < CommitteeSize { // GO-06 : comite complet requis
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite incomplet -> reglement refuse")
	}

	mids := make([]string, 0, len(vecs))
	for mid := range vecs {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	required := len(vecs)/2 + 1 // GO-06 : majorite STRICTE

	neighbors := map[string]int{}
	nWin := 0
	for _, mid := range mids {
		c := 0
		for _, other := range mids {
			if other != mid && cosineGE(vecs[mid], vecs[other], semanticThresholdBps) {
				c++
			}
		}
		neighbors[mid] = c
		if c+1 >= required {
			nWin++
		}
	}
	if nWin == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de majorite semantique stricte")
	}
	// v5 : BURN DOUX réel (déflation) — brûle FeeBurnBps de l'escrow puis répartit le RESTE aux gagnants.
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	distributable := job.Fee - burnAmt
	per := distributable / uint64(nWin) // GO-03 : reward DERIVE de l'escrow (Σ <= distributable)
	if per == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow insuffisant pour le nombre de gagnants")
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(per)))

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	for _, mid := range mids {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		if neighbors[mid]+1 >= required { // majorite -> PAYE depuis l'escrow
			if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
				if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
					return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
				}
			}
		} else { // OUTLIER -> slash
			amt := miner.Stake * params.SlashLeakBps / 10000
			miner.Stake -= amt
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
			}
			pools.Treasury += amt
			// INT-1 v0 inc.3 : ENREGISTRE le slash (job, mineur, montant) -> rend la RESTITUTION possible
			// si ce verdict est conteste avec succes (ResolveDispute upheld re-credite ce stake). Additif :
			// n'altere ni le paiement ni le slash ; persiste via le k.Job.Set final.
			if amt > 0 {
				job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: mid, Amount: amt})
			}
		}
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// BURN réel depuis l'escrow du module (déflation v5 ; supply globale ↓).
	if burnAmt > 0 {
		if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}
	// PY-05/TK-06 : surplus d'escrow rembourse au client.
	if surplus := distributable - per*uint64(nWin); surplus > 0 {
		if cliBz, e := k.addressCodec.StringToBytes(job.Client); e == nil {
			refund := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(surplus)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), refund); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "refund surplus: "+err.Error())
			}
		}
	}
	// GO-10 : marque regle (erreur verifiee).
	job.State = job.State + "+paid+finalized"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage : "+err.Error())
	}
	return &types.MsgSettleSemanticResponse{}, nil
}

// settleOptimistic (ADR-025 M2) — RÈGLEMENT OPTIMISTE k=1. Paie le SEUL mineur PRIMAIRE (tiré par
// assignedCommittee(.,1), pondéré stake) sur son commit unique, applique le burn doux v5, et marque le job
// `+paid+optimistic`. Plus de majorité k=3 exigée : la correction n'est plus garantie par la REDONDANCE mais
// A POSTERIORI par l'audit échantillonné VRF (M3) + le slash dur via AdjudicateDispute (M4). Le marqueur
// `optimistic` est vu comme `paid` par jobIsPaid (anti-rejeu) tout en restant repérable par l'audit.
// DORMANT : inatteignable tant que verification_mode==0 (défaut).
func (k msgServer) settleOptimistic(ctx context.Context, job types.Job, params types.Params) (*types.MsgSettleSemanticResponse, error) {
	prim, err := k.assignedCommittee(ctx, job.JobId, 1)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	primId := ""
	for id := range prim {
		primId = id // assignedCommittee(.,1) -> un seul élément
	}
	if primId == "" {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "aucun primaire assigné")
	}
	// Preuve de travail : le primaire DOIT avoir ancré un commit valide (sinon rien à régler).
	pc, cErr := k.Commit.Get(ctx, job.JobId+"__"+primId)
	if cErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "commit du primaire absent -> règlement optimiste impossible")
	}
	if parseIntVec(pc.ResultCommit) == nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "commit du primaire invalide")
	}
	// ADR-017 + tokenomics v5 — décision internal audit 2026-06-21 (B0.4) : l'inférence prend désormais le MÊME cut
	// protocole que SettleJob. Sans ça, l'inférence ne finançait NI treasury/validators/team NI le compteur
	// `Demand`, donc la subvention d'émission (travail ET dispo) ne récompensait JAMAIS le travail d'inférence.
	// Ordre : burn doux v5, cut protocole (split validators/team/treasury comme SettleJob), le RESTE au primaire.
	burnAmt := job.Fee * params.FeeBurnBps / 10000
	cut := job.Fee * params.ProtocolFeeBps / 10000
	if job.Fee < burnAmt+cut {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow insuffisant (burn+cut)")
	}
	validators := cut * params.ValidatorRewardBps / 10000
	team := cut * params.TeamFeeBps / 10000
	treasury := cut - validators - team
	minerNet := job.Fee - burnAmt - cut
	if minerNet == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "escrow insuffisant")
	}
	// PLAN-V2-FEE-HOLD §A — rétention fenêtrée : si hold_bps>0, on RETIENT `held` au module jusqu'à finalité
	// d'audit (libéré au primaire si vindiqué/non-audité, sinon rembourse le client). Dormant à 0 = paiement intégral.
	held := minerNet * params.HoldBps / 10000
	immediate := minerNet - held
	miner, mErr := k.Miner.Get(ctx, primId)
	if mErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "primaire non enregistré")
	}
	if immediate > 0 {
		if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
			coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(immediate)))
			if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
			}
		}
	}
	if held > 0 { // la rétention RESTE au module ; libérée/remboursée à la résolution (releaseHeld / refundRetainedToClient)
		if err := k.HeldFee.Set(ctx, job.JobId, held); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// Le cut RESTE au module (escrow), compté dans Pools (validators/team/treasury) comme SettleJob.
	pools, pErr := k.Pools.Get(ctx)
	if pErr != nil {
		if errors.Is(pErr, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, pErr.Error())
		}
	}
	pools.Validators += validators
	pools.Team += team
	pools.Treasury += treasury
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// `Demand += treasury+team` (signal de demande RÉELLE servie) SAUF self-dealing (client==operateur), comme
	// SettleJob. Crédité au règlement ; un primaire audité PUIS slashé voit ce Demand REVERSÉ par
	// slashCheatedPrimary (anti-farming) — la fenêtre pré-résolution est -EV (slash 80 % ≫ subvention possible).
	// Strict-finalité (créditer seulement sur non-audit / vindication) = raffinement v2.
	if job.Client != miner.Operator {
		miner.Demand += treasury + team
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	// internal audit 2026-06-21 (ii) — burn DIFFÉRÉ à finalité : avec rétention (hold_bps>0) on NE brûle PAS au settle
	// (BurnCoins est IRRÉVERSIBLE -> un job clawé ferait perdre 5 % au client honnête OU toucherait le bond, exclu
	// par (C)). On RETIENT le burn (HeldBurn) : brûlé à la finalité (release/vindication, releaseHeld) = taxe sur le
	// travail RÉUSSI ; RENDU au client sur clawback/slash. hold_bps=0 -> burn immédiat (v1 strict).
	if burnAmt > 0 {
		if params.HoldBps > 0 {
			if err := k.HeldBurn.Set(ctx, job.JobId, burnAmt); err != nil {
				return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "held_burn: "+err.Error())
			}
		} else if err := k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(burnAmt)))); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, "burn: "+err.Error())
		}
	}
	job.MinerId = primId
	job.State = job.State + "+paid+optimistic"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage : "+err.Error())
	}
	// M6 (ADR-025 probation) : incrémente le compteur de jobs optimistes du primaire (force 100% d'audit sur
	// ses N premiers jobs ; dormant si audit_probation_jobs==0). Get absent -> 0.
	cnt, _ := k.MinerOptimisticCount.Get(ctx, primId)
	if err := k.MinerOptimisticCount.Set(ctx, primId, cnt+1); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// M3 (ADR-025) : programme le TIRAGE D'AUDIT au bloc SUIVANT. La graine de tirage (AppHash / VRF
	// décentralisée du bloc d'audit) est POSTÉRIEURE au commit -> le mineur ne peut pas savoir, en
	// répondant, s'il sera audité (anti-grinding). L'EndBlocker tranche (no-op si audit_sample_bps==0).
	auditH := sdk.UnwrapSDKContext(ctx).BlockHeight() + 1
	if err := k.PendingAudit.Set(ctx, collections.Join(auditH, job.JobId)); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	return &types.MsgSettleSemanticResponse{}, nil
}
