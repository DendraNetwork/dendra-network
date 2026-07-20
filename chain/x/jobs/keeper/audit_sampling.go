package keeper

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"strconv"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// auditDomain — séparation de domaine du tirage d'audit (ne collisionne pas avec la graine de comité).
const auditDomain = "dendra/audit/v1|"

// auditDeferStride — de combien de blocs on repousse un tirage d'audit quand aucune graine
// décentralisée n'existe. Compromis entre deux coûts opposés : un pas trop court réécrit l'état à
// chaque bloc pour rien (l'absence de graine dure typiquement des dizaines de blocs au démarrage,
// et potentiellement indéfiniment si personne n'ancre sa clé VRF) ; un pas trop long retarde
// l'audit après que la graine est revenue. 20 blocs ≈ 20 s au block-time visé : invisible à
// l'échelle de `audit_resolve_timeout` (120), et 20× moins d'écritures pendant la panne.
const auditDeferStride = 20

// runOptimisticAudit (ADR-025 M3) — à la hauteur `h`, pour chaque job réglé OPTIMISTE programmé à cette
// hauteur, tire un audit avec probabilité EFFECTIVE (cf. effectiveAuditBps : base + adaptatif + probation) ;
// si tiré, OUVRE une dispute protocolaire (`+disputed`, disputeur = compte de gouvernance, bond 0) que le
// comité frais tranchera (M4), et programme un TIMEOUT d'auto-résolution (liveness). La graine `seed`
// (AppHash / VRF du bloc courant) est POSTÉRIEURE au commit -> tirage imprévisible (anti-grinding).
// DORMANT : no-op si verification_mode != 1 ; PendingAudit n'est alimenté que par settleOptimistic (mode 1).
func (k Keeper) runOptimisticAudit(ctx context.Context, h int64, seed string) error {
	p, err := k.Params.Get(ctx)
	if err != nil || p.VerificationMode != 1 {
		return nil // mode redondant (ou params absents) -> dormant, ne bloque jamais l'EndBlock
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAudit.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	if len(due) == 0 {
		return nil
	}
	// ANTI-GRINDING DU PROPOSANT — on ne tire QUE sur une graine décentralisée.
	//
	// `ProcessProposal` accepte une proposition sans injection de vote-extensions. Sans injection, pas
	// de graine décentralisée à cette hauteur, et `committeeBaseSeed` replie sur `AppHash(H-1):h` —
	// que le proposant du bloc h connaît AVANT de proposer. Il pouvait donc calculer hors chaîne le
	// tirage qui l'arrange, puis choisir d'injecter ou non : deux graines au choix à chaque bloc qu'il
	// propose, et davantage s'il retire des votes au-delà des 2/3 requis. C'est exactement la
	// prévisibilité qu'ADR-032 ferme côté sybil, rouverte côté consensus.
	//
	// Rejeter le bloc serait la réponse intuitive ; ce serait aussi remettre à l'attaquant un
	// interrupteur d'arrêt de la chaîne (proposer sans injection en boucle = rejet en boucle). On
	// préfère lui retirer le BÉNÉFICE : sans graine décentralisée, aucun tirage n'a lieu, les jobs dus
	// sont simplement REPORTÉS au bloc suivant. Omettre l'injection ne déplace donc plus rien, et le
	// report se résorbe dès qu'un proposant honnête injecte.
	if _, decentralized := k.committeeBaseSeedSourced(ctx, seed); !decentralized && p.CommitteeSeedSource == 1 {
		sdkCtx := sdk.UnwrapSDKContext(ctx)
		// REPORT PAR PAS LARGE, ET NON D'UN BLOC.
		//
		// Le report est la bonne réponse (sans graine décentralisée, tirer serait laisser le proposant
		// choisir), mais reporter d'UN bloc le rejoue à CHAQUE bloc : 2 écritures KV par job et par
		// bloc, pour une durée que rien ne borne. Or l'absence de graine n'est pas un incident bref —
		// c'est l'état NORMAL entre le bloc 1 et l'ancrage des clés VRF par les validateurs, et il
		// peut durer indéfiniment si personne ne les ancre. Le coût grandissait donc avec le trafic
		// pendant précisément la phase où le réseau est le plus fragile.
		//
		// Un pas de `auditDeferStride` blocs divise l'amplification d'écriture d'autant, sans rien
		// changer à la propriété : la rétention reste RETENUE (fail-closed, les fonds ne bougent pas)
		// et le report se résorbe dès qu'une graine existe. On ne « rend » jamais la main après N
		// tentatives : libérer faute d'avoir pu auditer redonnerait à un validateur qui s'abstient le
		// pouvoir de faire passer des jobs sans audit — le grinding, repoussé d'un cran.
		for _, jobId := range due {
			if err := k.PendingAudit.Remove(ctx, collections.Join(h, jobId)); err != nil {
				return err
			}
			if err := k.PendingAudit.Set(ctx, collections.Join(h+auditDeferStride, jobId)); err != nil {
				return err
			}
		}
		sdkCtx.Logger().Error("SECURITE: aucune graine VRF decentralisee a cette hauteur -> AUCUN tirage d'audit. Jobs reportes ; la retention reste RETENUE. Cause usuelle : aucun validateur n'a encore ancre sa cle VRF, ou les vote-extensions ne sont pas actives. Si ce message persiste, le reseau ne verifie RIEN.",
			"height", h, "jobs_reportes", len(due), "prochain_essai", h+auditDeferStride)
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"audit_draw_deferred",
			sdk.NewAttribute("height", strconv.FormatInt(h, 10)),
			sdk.NewAttribute("jobs", strconv.Itoa(len(due))),
			sdk.NewAttribute("reason", "graine non decentralisee"),
		))
		return nil
	}

	disputer, _ := k.addressCodec.BytesToString(k.GetAuthority()) // compte de module gouvernance (bond nul)
	for _, jobId := range due {
		// ⚠️ LA CONDITION EST `!jobIsResolved`, PAS `!jobIsDisputed`, ET C'EST TOUT L'ENJEU.
		//
		// Avec `!jobIsDisputed`, un job déjà contesté était SAUTÉ — mais son échéance était retirée
		// quelques lignes plus bas, pour tous les jobs dus sans distinction. L'audit n'avait donc
		// jamais lieu. Or `DisputeVerdict` n'exige que « payé et pas déjà contesté » : il n'interdit
		// pas au PRIMAIRE de contester son propre job, et l'audit est programmé à h+1 du règlement,
		// soit une fenêtre d'un bloc parfaitement observable.
		//
		// Un mineur pouvait donc ACHETER SA SORTIE D'AUDIT au prix de `dispute_bond`. Le calcul est
		// structurellement défavorable : le bond est un coût FIXE, le slash est PROPORTIONNEL au
		// stake. Au-delà de `dispute_bond / slash_leak_bps` de stake, l'évasion devient rentable, et
		// elle le devient d'autant plus que le mineur est gros — exactement l'inverse de ce qu'une
		// garde anti-triche doit produire. Le job retombait ensuite dans la branche « dispute
		// infondée », qui libère la rétention : le paiement non vérifié était finalisé.
		//
		// Désormais un job contesté par un humain est AUDITÉ QUAND MÊME. Son comité est ancré, donc
		// `resolveDisputedAudit` prend le vrai chemin d'audit au lieu de la branche infondée, et la
		// contestation ne protège plus de rien.
		if job, jErr := k.Job.Get(ctx, jobId); jErr == nil && jobIsPaid(job.State) && !jobIsResolved(job.State) {
			if auditDraw(seed, jobId) < k.effectiveAuditBps(ctx, p, job) {
				// NE PAS ÉCRASER UNE DISPUTE HUMAINE EN COURS. Le job peut déjà être `+disputed` par
				// quelqu'un qui a escrowé un bond réel. Réécrire `Disputer` et remettre `DisputeBond`
				// à 0 laisserait ses coins au compte de module sans propriétaire — on créerait ici les
				// fonds orphelins qu'on a passé la journée à fermer ailleurs. L'audit s'ajoute à la
				// dispute humaine ; il ne la remplace pas.
				if !jobIsDisputed(job.State) {
					job.Disputer = disputer
					job.DisputeBond = 0
					job.DisputeHeight = h
					job.State = job.State + "+disputed"
				}
				// ADR-032 — ANCRAGE DU COMITÉ D'AUDIT, au moment du tirage et pas après.
				// La graine `seed` est POSTÉRIEURE au commit du primaire : ni lui ni un attaquant ne
				// peuvent la prévoir, donc personne ne peut se placer dans le comité d'un job choisi.
				// C'est ce qui transforme « n'importe quel mineur enregistré peut voter » en « seuls
				// les convoqués votent » — et le tirage étant pondéré stake, se multiplier en
				// identités à min_stake n'achète plus de sièges.
				members, mErr := k.drawAuditCommittee(ctx, seed, jobId, job.MinerId, disputer)
				if mErr != nil {
					return mErr
				}
				if err := k.AuditCommittee.Set(ctx, jobId, strings.Join(members, ",")); err != nil {
					return err
				}
				if err := k.Job.Set(ctx, jobId, job); err != nil {
					return err
				}
				// liveness : programme l'auto-résolution si aucun comité frais ne ré-adjuge à temps.
				if p.AuditResolveTimeout > 0 {
					if err := k.PendingAuditResolve.Set(ctx, collections.Join(h+int64(p.AuditResolveTimeout), jobId)); err != nil {
						return err
					}
				}
				// Les membres sont émis dans l'événement : transparence (n'importe qui peut rejouer le
				// tirage) et le juge off-chain sait s'il est convoqué au lieu de le deviner.
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
					sdk.NewEvent("audit_requested",
						sdk.NewAttribute("job_id", jobId),
						sdk.NewAttribute("committee", strings.Join(members, ",")),
					),
				)
			} else {
				// PLAN-V2-FEE-HOLD §A : NON audité = paiement optimiste FINAL -> libère la fee retenue au primaire.
				k.releaseHeld(ctx, &job)
			}
		}
		if err := k.PendingAudit.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// effectiveAuditBps (ADR-025 M6) — taux d'audit EFFECTIF d'un job optimiste. Tout DORMANT par défaut :
//   - PROBATION (audit_probation_jobs>0) : 100% tant que le primaire a servi <= N jobs (anti-Sybil + cold-start).
//   - ADAPTATIF (audit_adaptive>0)        : sinon base·(1 + fee/ref) capé 10000 (gros job audité plus souvent).
//   - sinon -> audit_sample_bps inchangé (comportement M3).
func (k Keeper) effectiveAuditBps(ctx context.Context, p types.Params, job types.Job) uint64 {
	if p.AuditProbationJobs > 0 && job.MinerId != "" {
		if cnt, err := k.MinerOptimisticCount.Get(ctx, job.MinerId); err == nil && cnt <= p.AuditProbationJobs {
			return 10000
		}
	}
	eff := p.AuditSampleBps
	if p.AuditAdaptive > 0 {
		eff += p.AuditSampleBps * job.Fee / p.AuditAdaptive // fee<=1e13, bps<=1e4 -> produit <2^57, pas d'overflow
		if eff > 10000 {
			eff = 10000
		}
	}
	return eff
}

// runAuditResolveTimeout (ADR-025 liveness + ADR-028 anti-évasion) — à `h`, RÉSOUT les audits optimistes non
// adjugés arrivés à échéance. Plus d'« innocence par défaut » aveugle : le primaire optimiste a été PAYÉ, c'est
// le SUSPECT. On tranche SUR LES VERDICTS du comité frais (`<jobId>__verdict__<minerId>`, "0"=invalide, tally
// pondéré stake), AU PLANCHER DE PARTICIPATION SYMÉTRIQUE (effectiveSlashFloor, F1) :
//   - voters ≥ plancher ET majorité "invalide" -> TRICHE -> slash dur + clawback. Le primaire SILENCIEUX arrive
//     ici SANS marqueur on-chain forgeable : le comité poste lui-même "0" quand il ne peut pas obtenir de
//     révélation exploitable (signal NON falsifiable, judge_worker), ce qui le convertit en quorum-triche.
//   - voters ≥ plancher ET majorité "valide" -> VINDIQUÉ (même plancher que le slash : 2 sybils ne peuvent pas
//     vindiquer un tricheur — dual du grief).
//   - en deçà du plancher OU pas de majorité -> CLAWBACK LÉGER restituable (paiement non vérifié), NI slash dur
//     NI vindication. (Il n'y a PAS de branche `<jobId>__reveal__` : la décision repose UNIQUEMENT sur le verdict
//     "0" du comité et le tally au plancher — la révélation reste un échange OFF-CHAIN entre primaire et comité.)
// Une dispute NON-optimiste (humaine) non honorée garde l'innocence par défaut (le disputeur est l'accusateur).
// Libère toujours la liveness (plus de job bloqué). DORMANT : rien n'est programmé si audit_resolve_timeout==0.
func (k Keeper) runAuditResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil // params absents -> ne bloque jamais l'EndBlock
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAuditResolve.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if err := k.resolveDisputedAudit(ctx, h, jobId, p, true); err != nil { // allowAppeal=true : peut DÉFÉRER vers un appel
			return err
		}
		if err := k.PendingAuditResolve.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// runAppealResolveTimeout (PLAN-V2-FEE-HOLD §B) — 2e ÉCHÉANCE d'appel. Un primaire muet DÉFÉRÉ au 1er timeout
// (appeal_window>0) est finalisé ici s'il n'a pas déjà été résolu entre-temps (par AdjudicateDispute pendant la
// fenêtre). Re-tally : une révélation TARDIVE a pu produire un quorum VALIDE -> vindiqué (rétention libérée) ;
// sinon clawback FINAL (allowAppeal=false -> pas de nouveau report). Liveness : aucun job ne reste +disputed
// indéfiniment. DORMANT : PendingAppealResolve n'est alimenté que si appeal_window>0.
func (k Keeper) runAppealResolveTimeout(ctx context.Context, h int64) error {
	p, perr := k.Params.Get(ctx)
	if perr != nil {
		return nil
	}
	var due []string
	rng := collections.NewPrefixedPairRange[int64, string](h)
	if err := k.PendingAppealResolve.Walk(ctx, rng, func(key collections.Pair[int64, string]) (bool, error) {
		due = append(due, key.K2())
		return false, nil
	}); err != nil {
		return err
	}
	for _, jobId := range due {
		if err := k.resolveDisputedAudit(ctx, h, jobId, p, false); err != nil { // allowAppeal=false : finalise (clawback)
			return err
		}
		if err := k.PendingAppealResolve.Remove(ctx, collections.Join(h, jobId)); err != nil {
			return err
		}
	}
	return nil
}

// resolveDisputedAudit — résout UN job d'audit optimiste à échéance (timeout d'audit OU d'appel), sur les verdicts
// du comité frais au PLANCHER de participation symétrique (F1). Partagé par les deux timeouts pour qu'ils ne
// divergent JAMAIS. Si `allowAppeal` et `appeal_window>0` et pas de quorum -> DIFFÈRE (2e échéance) au lieu de
// clawback : on NE touche à RIEN (rétention/cut/burn restent au module) pour laisser le primaire honnête-hors-ligne
// révéler en retard, sans risque de double-paiement à la restitution. Au timeout d'appel (`allowAppeal=false`),
// le default FINALISE par clawback. Mute+persiste le job (sauf déférement : job inchangé, +disputed & !resolved).
func (k Keeper) resolveDisputedAudit(ctx context.Context, h int64, jobId string, p types.Params, allowAppeal bool) error {
	job, jErr := k.Job.Get(ctx, jobId)
	if jErr != nil || !jobIsDisputed(job.State) || jobIsResolved(job.State) {
		return nil // déjà résolu (p.ex. par AdjudicateDispute pendant la fenêtre) ou inexistant -> rien à faire
	}
	if jobIsOptimistic(job.State) && job.MinerId != "" {
		// DEUX SILENCES QU'IL NE FAUT PAS CONFONDRE.
		//
		// Ce chemin a été conçu pour les jobs ÉCHANTILLONNÉS : un comité d'audit y a été tiré et
		// ANCRÉ, donc l'absence de verdicts est une PREUVE NÉGATIVE (« le paiement n'a pas pu être
		// vérifié ») et le clawback du prix est la conclusion voulue par ADR-028.
		//
		// Depuis que les disputes HUMAINES ont une échéance, des jobs JAMAIS échantillonnés arrivent
		// ici. Pour eux, aucun comité n'a jamais été convoqué : `auditVerdictTally` lit
		// `AuditCommittee[jobId]`, que seul `runOptimisticAudit` écrit — le tally renvoie donc
		// STRUCTURELLEMENT 0 votant, et la branche `default` reprenait la fee d'un mineur honnête
		// déjà payé. Coût pour l'attaquant : le gas (`dispute_bond` vaut 0). Le primaire n'avait
		// aucun moyen de se défendre, puisque aucun verdict ne pouvait compter.
		//
		// Le silence de personne n'est pas le silence d'un comité. Sans corps convoqué, la dispute
		// n'est pas honorée : rétention libérée au mineur, et le bond part en Trésorerie — c'est
		// exactement le rôle anti-grief du bond, et la seule différence avec un comité tiré resté
		// muet, où la panne est celle du réseau et où le bond est rendu.
		//
		// ⚠️ LA CONDITION EXIGE LES DEUX TERMES. « Pas de comité ancré » ne suffit PAS : un job
		// RÉELLEMENT échantillonné dont le tirage n'a rendu personne (réseau trop petit) n'a pas
		// d'ancre non plus, et son paiement est bel et bien non vérifié — le dispenser du clawback
		// désarmerait ADR-028. Seule la conjonction « ouvert par un humain ET jamais convoqué »
		// décrit la dispute infondée.
		// Deux comités peuvent avoir été convoqués, sous DEUX clés : `<jobId>` (échantillonnage) et
		// `<jobId>__redo` (dispute humaine). Ce timeout ne lisait que la première (HAUT-2, internal audit
		// 07-20) : une dispute humaine dont le comité redo était ANCRÉ mais MUET voyait « pas de
		// comité » et tombait dans « infondée », confisquant le bond d'un disputeur honnête parce que
		// les juges TIRÉS ne répondaient pas — l'invariant ① retourné contre le disputeur.
		_, auditAnchored := k.auditCommitteeAllowed(ctx, jobId)
		allowedRedo, redoAnchored := k.redoCommitteeAllowed(ctx, jobId)
		if !auditAnchored && redoAnchored {
			// (i) COMITÉ CONVOQUÉ MAIS MUET (ou sous-quorum). Un comité redo existe, l'adjudication n'a
			// pas abouti à l'échéance : panne du réseau, pas faute du disputeur. Rétention libérée au
			// primaire (aucune preuve de triche) ET bond RENDU — ce que `refundDisputeBond` faisait,
			// devenu inatteignable.
			//
			// INSTRUMENTATION K (internal audit 07-20) : on émet `répondants / sièges` avant de résoudre.
			// C'est LE point de mesure — non-rollback, et il capture précisément les disputes dont
			// l'adjudication permissionless a échoué faute de quorum, qui sont celles qui informent K.
			seats := len(allowedRedo)
			responders := k.countRedoResponders(ctx, jobId, allowedRedo)
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"redo_participation",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("seats", strconv.Itoa(seats)),
				sdk.NewAttribute("responders", strconv.Itoa(responders)),
				sdk.NewAttribute("resolved_by", "timeout"), // population « sous-quorum » — cf. le pendant « adjudication » dans msg_server_adjudicate
			))
			k.releaseHeld(ctx, &job)
			if err := k.refundDisputeBond(ctx, &job); err != nil {
				return err
			}
			job.State = job.State + "+resolved"
			if err := k.Job.Set(ctx, jobId, job); err != nil {
				return err
			}
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"dispute_committee_silent",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("reason", "comite de re-adjudication convoque mais muet -> bond rendu (panne reseau)"),
			))
			return nil
		}
		if !auditAnchored && k.isHumanDispute(ctx, jobId) {
			// (ii) DISPUTE INFONDÉE. NI comité d'audit NI comité redo : personne n'a jamais été
			// convoqué. Le disputeur n'a mobilisé aucun juge -> bond en Trésorerie (anti-grief).
			k.releaseHeld(ctx, &job)
			if pools, pErr := k.Pools.Get(ctx); pErr == nil {
				pools.Treasury += job.DisputeBond
				if err := k.Pools.Set(ctx, pools); err != nil {
					return err
				}
			}
			job.DisputeBond = 0 // consommé : ni re-confisqué, ni remboursé plus bas
			job.State = job.State + "+resolved"
			if err := k.Job.Set(ctx, jobId, job); err != nil {
				return err
			}
			sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
				"dispute_unfounded",
				sdk.NewAttribute("job_id", jobId),
				sdk.NewAttribute("reason", "aucun comite (ni audit ni redo) n'a jamais ete convoque"),
			))
			return nil
		}
		// (iii) sinon : comité d'audit ancré, ou tirage d'échantillonnage vide sans dispute humaine
		// -> le tally de verdicts décide (clawback si le paiement n'est pas vérifié).
		cheated, valid, voters, invalidVotes, seats := k.auditVerdictTally(ctx, jobId, job.MinerId)
		switch {
		case auditSlashDecision(p, cheated, voters, invalidVotes, seats):
			// triche au PLANCHER -> slash DUR + clawback (le primaire MUET arrive ici : le comité poste "0" sur
			// révélation manquante => quorum-triche). Signal non falsifiable.
			if err := k.slashCheatedPrimary(ctx, &job, p); err != nil {
				return err
			}
			job.State = job.State + "+resolved+clawed"
		case voters >= effectiveSlashFloor(p) && valid:
			// SYMÉTRIE F1 : la VINDICATION exige le MÊME plancher que le slash. Quorum valide -> vindiqué (la
			// révélation tardive de l'appel passe ICI à la 2e échéance -> l'honnête-hors-ligne est restitué).
			k.releaseHeld(ctx, &job) // rétention libérée + burn brûlé à finalité
			job.State = job.State + "+resolved"
		default:
			// pas de quorum -> paiement NON vérifié. PLAN-V2-FEE-HOLD §B : au 1er timeout, si une fenêtre d'appel
			// est gouvernée, on DIFFÈRE (2e échéance) AU LIEU de clawback -> le primaire honnête-hors-ligne peut
			// révéler en retard. On NE clawback PAS encore (rétention/cut/burn gardés -> pas de double-paiement à
			// la restitution). Au timeout d'appel (allowAppeal=false), on FINALISE par clawback.
			if allowAppeal && p.AppealWindow > 0 {
				if err := k.PendingAppealResolve.Set(ctx, collections.Join(h+int64(p.AppealWindow), jobId)); err != nil {
					return err
				}
				sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
					sdk.NewEvent("audit_appeal_scheduled", sdk.NewAttribute("job_id", jobId)),
				)
				return nil // job INCHANGÉ (+disputed, !resolved) ; résolu à la 2e échéance ou par AdjudicateDispute
			}
			// ADR-028 v2 : clawbackPayment applique en plus la pénalité silence_slash_bps si gouvernée (>0).
			// α-(b) internal audit 2026-07-04 : la pénalité exige le SIGNAL muet (≥1 verdict « 0 » posté) — un
			// no-quorum d'abstentions pures (0 verdict) ne coûte que le prix, jamais le bond de l'honnête.
			if err := k.clawbackPayment(ctx, &job, p, invalidVotes > 0); err != nil {
				return err
			}
			job.State = job.State + "+resolved+clawed"
		}
	} else {
		job.State = job.State + "+resolved" // dispute humaine non honorée -> innocence par défaut (inchangé)
	}
	// BOND DE DISPUTE À L'ÉCHÉANCE. Ce chemin marquait `+resolved` sans jamais disposer du bond : il
	// restait au compte de module, sans ligne comptable disant à qui il revient — des fonds orphelins,
	// exactement le défaut de l'export genesis. C'était dormant tant que seuls les jobs ÉCHANTILLONNÉS
	// (sans disputeur ni bond) arrivaient ici ; router les disputes humaines vers cette échéance le
	// réveillerait. Règle : **seule une adjudication peut confisquer un bond** ; un timeout signifie
	// qu'aucun comité n'a tranché, donc que le disputeur n'a rien à se reprocher — la panne est celle
	// du réseau, et on ne la facture pas à l'honnête (invariant ①). Restitution, jamais Trésorerie.
	if err := k.refundDisputeBond(ctx, &job); err != nil {
		return err
	}
	if err := k.Job.Set(ctx, jobId, job); err != nil {
		return err
	}
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
		sdk.NewEvent("audit_expired", sdk.NewAttribute("job_id", jobId)),
	)
	return nil
}

// auditDraw — tirage déterministe dans [0,10000) : 8 premiers octets de H(domaine‖seed‖jobId), mod 10000.
// Pur (testable hors keeper). `seed` postérieur au commit -> imprévisible par le mineur (anti-grinding).
func auditDraw(seed, jobId string) uint64 {
	sum := sha256.Sum256([]byte(auditDomain + seed + "|" + jobId))
	return binary.BigEndian.Uint64(sum[:8]) % 10000
}
