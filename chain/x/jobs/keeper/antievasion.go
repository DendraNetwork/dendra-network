package keeper

import (
	"context"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// ADR-028 — ANTI-ÉVASION DU SLASH OPTIMISTE.
//
// Problème (internal-notes 2026-06-19) : en vérification optimiste k=1, le primaire est PAYÉ d'abord.
// La révélation J1 (re-sceller prompt+réponse vers le comité frais) est COOPÉRATIVE : seul le primaire peut
// la produire. Un primaire qui TRICHE puis SE TAIT n'était NI jugé NI slashé (le timeout liveness vindiquait
// « innocence par défaut »). => stratégie dominante du tricheur = se taire ; l'inégalité de Nash ne tenait plus.
//
// Correctif (design internal audit, raffiné par l'architecte) : le primaire optimiste est le SUSPECT ; il ne garde
// JAMAIS un paiement non vérifié. Deux leviers + un plancher :
//   1. OFF-CHAIN (signal NON falsifiable) : `judge_worker.py` poste un verdict "0" quand il ne peut pas obtenir
//      de révélation EXPLOITABLE après grâce. => un primaire muet/qui révèle du vide est converti en
//      QUORUM-TRICHE par le comité lui-même (et non par un marqueur on-chain forgeable). Répond à la Q2 (relais).
//   2. ON-CHAIN (runAuditResolveTimeout) : à l'échéance, AU PLANCHER de participation : quorum-triche -> slash
//      dur+clawback ; quorum-valide -> vindiqué ; en deçà du plancher / pas de majorité -> clawback LÉGER
//      restituable (ni slash dur, ni vindication).
//   3. PLANCHER DE PARTICIPATION SYMÉTRIQUE (auditSlashFloor, demande internal audit ; symétrie F1 2026-06-20) : le tally
//      est calculé sur le stake des PRÉSENTS ; sans plancher, 2 juges/sybils « majorité des présents »
//      slasheraient un honnête (grief) OU vindiqueraient un tricheur (dual du grief, F1) si les juges honnêtes
//      votent en retard. => slash dur ET vindication SEULEMENT si assez de juges distincts ont voté
//      (⌈AuditCommitteeSize/2⌉+1 = 4 à N=5, DÉCOUPLÉ de CommitteeSize=3).

// AuditCommitteeSize (ADR-028 F1 — DÉCOUPLAGE, internal audit 2026-06-20) — taille du comité d'AUDIT optimiste
// (juges frais re-sollicités à l'échéance). DÉLIBÉRÉMENT DÉCOUPLÉE de `CommitteeSize`=3 (committee.go) qui reste
// la taille du comité REDONDANT/mode-0 (payout, verify, finalize, settle_semantic). On NE bumpe PAS CommitteeSize
// (cela casserait le règlement mode-0 sur un devnet à <5 mineurs : settle_semantic exige len(vecs)≥CommitteeSize).
// À N=5, le plancher d'audit devient 4 (⌈5/2⌉+1) : il faut 4 juges distincts pour qu'un slash OU une vindication
// se produise, ce qui FERME F1 (2 sybils ne sont plus « majorité des présents » ni pour slasher ni pour vindiquer).
const AuditCommitteeSize = 5

// auditSlashFloor (ADR-028, plancher de participation — demande du internal audit 2026-06-19 ; F1 2026-06-20) — nb MINIMAL
// de juges frais distincts PRÉSENTS pour qu'une DÉCISION DURE (slash OU vindication) soit déclenchée :
// ⌈AuditCommitteeSize/2⌉+1 (= 4 pour AuditCommitteeSize=5). Le tally se calcule sur le stake des PRÉSENTS ; sans ce
// plancher, 2 juges/sybils « majorité des présents » pourraient slasher un honnête (grief) OU vindiquer un tricheur
// (dual du grief, F1) quand les juges honnêtes votent en retard/offline. En deçà -> clawback léger (non vérifié),
// JAMAIS slash dur NI vindication. (Pur Go, pas de régen : AuditCommitteeSize est une const du keeper.)
// v2 = fraction gouvernée via effectiveSlashFloor/AuditMinQuorum.
func auditSlashFloor() int { return (AuditCommitteeSize+1)/2 + 1 }

// effectiveSlashFloor (ADR-028 v2) — plancher de participation EFFECTIF : `AuditMinQuorum` (gouverné, nb ENTIER
// de juges frais distincts) s'il est > 0, SINON repli sur le plancher `auditSlashFloor()` (⌈AuditCommitteeSize/2⌉+1).
// DORMANT : tant que `audit_min_quorum == 0` (défaut), retourne EXACTEMENT `auditSlashFloor()` -> comportement v1
// strictement inchangé. Tous les sites de décision « slash dur » (timeout + AdjudicateDispute) passent par ici.
func effectiveSlashFloor(p types.Params) int {
	if p.AuditMinQuorum > 0 {
		return int(p.AuditMinQuorum)
	}
	return auditSlashFloor()
}

// auditVerdictTally — tally pondéré STAKE des verdicts du comité frais (`<jobId>__verdict__<minerId>`,
// "0"=invalide). Exclut le primaire (en k=1 il est le seul « origine ») ; n'accepte QUE les membres du
// **comité ANCRÉ au tirage** (ADR-032).
//
// ⚠️ HISTOIRE DE CETTE LIGNE — à ne pas ré-assouplir. Elle disait « n'accepte que des mineurs ENREGISTRÉS
// (stake réel = anti-sybil) ». L'hypothèse était QUALITATIVE (« s'enregistrer coûte quelque chose ») et
// s'est révélée QUANTITATIVEMENT fausse : à `min_stake`, 4 identités coûtaient 0,2 DNDR et suffisaient à
// slasher n'importe quel honnête de 80 %. Notre propre test l'exigeait, vert à chaque run. L'appartenance
// au comité tiré au sort est désormais la seule autorisation de vote.
//
// Renvoie (cheated, valid, voters) : `voters` = nb de juges convoqués ayant voté ;
// cheated = majorité STRICTE de stake "invalide" ; valid = majorité STRICTE "valide" (égalité => ni l'un ni
// l'autre). `invalidVotes` = COUNT de juges "invalide" (≠ stake) -> consommé par le VETO (auditSlashDecision),
// dont le seuil est RELATIF au comité ancré (⌈2/3⌉ des sièges) depuis ADR-032 amendée — plus « 4 sur 5 ».
// L'APPELANT applique effectiveSlashFloor() À LA VINDICATION et auditSlashDecision() AU SLASH (veto count si gouverné,
// sinon majorité-stake au plancher v1). En deçà -> ni slash ni vindication, clawback léger restituable.
// (totalStake ≤ offre 1e13 udndr -> *2 sans overflow uint64.)
func (k Keeper) auditVerdictTally(ctx context.Context, jobId, primId string) (cheated, valid bool, voters, invalidVotes, seats int) {
	// ADR-032 — SEULS LES CONVOQUÉS VOTENT. Sans cette barrière, « mineur enregistré » suffisait pour
	// peser sur un slash : 4 identités à min_stake faisaient perdre 80 % de son stake à un honnête.
	// FAIL-CLOSED : pas de comité ancré (jobs antérieurs, chemins hors mode-1) -> tally VIDE -> aucun
	// slash dur possible, on retombe sur le clawback léger. Asymétrie voulue : rater un tricheur est
	// borné et -EV (ADR-025), slasher un honnête ne se rattrape pas.
	allowed, anchored := k.auditCommitteeAllowed(ctx, jobId)
	if !anchored {
		return false, false, 0, 0, 0
	}
	seats = len(allowed) // taille du comité ANCRÉ : c'est le dénominateur du quorum (ADR-032 amendée)
	prefix := jobId + "__verdict__"
	var invalidStake, totalStake uint64
	_ = k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if !strings.HasPrefix(key, prefix) {
			return false, nil
		}
		mid := strings.TrimPrefix(key, prefix)
		if mid == primId {
			return false, nil // exclut le primaire (seul « origine » en k=1)
		}
		if !allowed[mid] {
			return false, nil // NON convoqué -> son verdict ne compte pas (ADR-032)
		}
		m, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			return false, nil // mineur enregistré requis
		}
		voters++
		totalStake += m.Stake
		if strings.TrimSpace(c.ResultCommit) == "0" {
			invalidStake += m.Stake
			invalidVotes++ // VETO : on COMPTE les "invalide" (seuil relatif aux sièges ancrés), pas seulement leur stake
		}
		return false, nil
	})
	if voters == 0 || totalStake == 0 {
		return false, false, 0, 0, seats
	}
	cheated = invalidStake*2 > totalStake
	valid = invalidStake*2 < totalStake
	return cheated, valid, voters, invalidVotes, seats
}

// auditSlashDecision (ADR-028 v2, VETO pro-honnête ; ADR-032 amendée) — décide le SLASH DUR.
//
// ⚠️ CE QUI A ÉTÉ CORRIGÉ ICI, ET POURQUOI ÇA COMPTE. Le veto valait « `audit_min_quorum`=4 sur un comité
// de 5 » = 4/5 = QUASI-UNANIMITÉ : c'était toute sa raison d'être — *un honnête jugé invalide par une
// MINORITÉ n'est jamais slashé*. ADR-032 a porté le comité tiré à 15 sièges pour la liveness en gardant
// « quorum inchangé » — mais 4 sur 15 fait **27 %**, et ce quorum était un COUNT ABSOLU : 4 verdicts
// « invalide » slashaient **même si 11 jurés votaient « valide »**. La quasi-unanimité était devenue une
// minorité de blocage. Le seuil était juste ; c'est son DÉNOMINATEUR qui avait changé sous lui.
//
// Correctif : le quorum devient RELATIF au comité réellement ancré (⌈2/3⌉ des sièges), avec le plancher
// gouverné comme borne basse, ET la majorité de stake exigée EN PLUS. Deux verrous indépendants, tous
// deux tarifés en capital : il faut à la fois assez de SIÈGES (tirés, donc achetés en stake) et la
// majorité du STAKE votant. Exiger les deux ne peut que RÉDUIRE les slashs — donc c'est strictement
// pro-honnête, cohérent avec l'asymétrie assumée depuis ADR-025 (rater un tricheur est borné et -EV ;
// slasher un honnête est irréversible).
//
// `seats` = taille du comité ancré. À 0 (jobs sans ancrage) on retombe sur le plancher gouverné seul —
// mais le tally est déjà fail-closed en amont, donc ce chemin ne slashe rien. Pur Go, testable hors keeper.
func auditSlashDecision(p types.Params, cheated bool, voters, invalidVotes, seats int) bool {
	if p.AuditMinQuorum > 0 {
		need := int(p.AuditMinQuorum)
		if q := (2*seats + 2) / 3; q > need { // ⌈2/3 × seats⌉ en arithmétique entière
			need = q
		}
		return invalidVotes >= need && cheated // SIÈGES (⌈2/3⌉) **ET** majorité de STAKE
	}
	return voters >= auditSlashFloor() && cheated // v1 strict : participation + majorité de stake
}

// slashCheatedPrimary — TRICHE PROUVÉE (quorum de verdicts "invalide" AU-DESSUS du plancher) : slash DUR
// `SlashLeakBps` du stake du primaire (compteur ; coins déjà au module via le bond), REMBOURSE au client le prix
// payé (min(fee, slashé)), verse le RESTE à la Trésorerie, ENREGISTRE un SlashRecord RESTITUABLE. Borné ; envoi
// best-effort (un échec d'adresse ne bloque pas l'EndBlock). Mute `job` (persisté par l'appelant).
func (k Keeper) slashCheatedPrimary(ctx context.Context, job *types.Job, params types.Params) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		return nil // primaire disparu -> rien à slasher
	}
	// internal audit 2026-06-21 (1)+(ii) : le client est remboursé d'abord depuis TOUTE la fee RETENUE (rétention minerNet
	// + cut reversé de Pools + burn différé rendu) et le Demand de ce job est reversé. Le slash dur du bond reste
	// PUNITIF (-> Trésorerie) ; il ne rembourse le client QUE de l'éventuel immédiat (hold partiel ; nul au plein-hold).
	refundedRetained := k.refundRetainedToClient(ctx, job, params, &miner)
	amt := miner.Stake * params.SlashLeakBps / 10000
	if amt > 0 {
		miner.Stake -= amt
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil { // persiste le Demand reversé (+ slash éventuel)
		return err
	}
	fromBond := uint64(0)
	if amt > 0 {
		owed := uint64(0)
		if job.Fee > refundedRetained { // solde = immédiat déjà payé au mineur (0 au plein-hold)
			owed = job.Fee - refundedRetained
		}
		fromBond = k.refundClient(ctx, job, owed, amt)
		k.addTreasury(ctx, amt-fromBond) // le RESTE du slash (punitif) -> Trésorerie
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "cheated")
	return nil
}

// clawbackPayment — PAS DE QUORUM / PARTICIPATION INSUFFISANTE : le primaire ne garde pas un paiement NON
// VÉRIFIÉ, mais on NE le slashe PAS dur (aucune preuve de triche au plancher requis). On reprend SEULEMENT le
// prix du job depuis son stake et on le rembourse au client ; SlashRecord RESTITUABLE (honnête-hors-ligne
// récupérable via gouvernance). Mute `job` (persisté par l'appelant).
//
// ADR-028 v2 — `params.SilenceSlashBps` : DORMANT (0 = comportement v1 ci-dessus, clawback du prix seul). Si > 0,
// on ajoute une pénalité de stake DÉDIÉE `stake·SilenceSlashBps/10000` (calibrée ≥ compute économisé par la triche
// paresseuse « se taire »), versée à la Trésorerie, ENREGISTRÉE en SlashRecord RESTITUABLE (l'honnête hors-ligne
// la récupère via gouvernance / futur appel permissionless). Cette pénalité s'ajoute AU clawback du prix ; elle
// est calculée sur le stake RESTANT (post-clawback) et bornée, donc jamais > stake disponible.
//
// α-(b) — ARBITRAGE internal audit 2026-07-04 : la pénalité de silence exige `muteSignal` = AU MOINS UN verdict « 0 »
// posté (le signal ADR-028 non falsifiable : révélation absente/inexploitable ou suspicion réelle). Un no-quorum
// d'ABSTENTIONS PURES (aucun verdict posté — le juge incertain s'abstient) ne déclenche QUE le clawback du prix :
// l'ancien comportement punissait l'HONNÊTE pour l'incertitude du JUGE (mesuré runs 5-11 : −20 % de bond composé
// par incident sans AUCUN slash dur — « la même bombe de confiance que le faux-slash, en version économique »).
// Le muet RÉEL provoque des « 0 » massifs (postés sur révélation absente) -> muteSignal vrai -> -EV intact.
func (k Keeper) clawbackPayment(ctx context.Context, job *types.Job, params types.Params, muteSignal bool) error {
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		return nil
	}
	// PLAN-V2-FEE-HOLD §A (caveat internal audit C) : rembourser le client depuis la FEE RETENUE d'abord ; ne ponctionner
	// le BOND que pour le MANQUE (held < fee). Avec hold_bps=10000, held≈fee -> bond INTACT (un honnête sous-
	// plancher = défaut de LIVENESS du comité, pas du mineur -> sa caution de sécurité reste intacte).
	// internal audit 2026-06-21 (1)+(ii) : rembourser le client depuis TOUTE la fee RETENUE (rétention minerNet + cut
	// reversé de Pools + burn différé rendu) et reverser le Demand AVANT tout recours au bond. Au PLEIN-HOLD le
	// retenu = la fee entière -> `owed`=0 -> BOND INTACT (décision C « jamais le bond » strictement tenue). En hold
	// partiel, le bond ne couvre que l'immédiat déjà payé au mineur. (hold_bps=0 -> rien de retenu -> v1 : prix sur le bond.)
	refundedRetained := k.refundRetainedToClient(ctx, job, params, &miner)
	owed := uint64(0)
	if job.Fee > refundedRetained {
		owed = job.Fee - refundedRetained
	}
	amt := owed
	if amt > miner.Stake {
		amt = miner.Stake
	}
	if amt > 0 {
		miner.Stake -= amt
	}
	if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil { // persiste le Demand reversé (+ clawback éventuel)
		return err
	}
	fromBond := uint64(0)
	if amt > 0 {
		fromBond = k.refundClient(ctx, job, owed, amt)
		k.addTreasury(ctx, amt-fromBond) // adresse client invalide -> résidu en Trésorerie (jamais perdu)
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: amt})
	}
	if refundedRetained+fromBond > 0 || amt > 0 {
		k.emitClawback(ctx, job.JobId, job.MinerId, amt, refundedRetained+fromBond, "no_quorum")
	}

	// ADR-028 v2 — pénalité de silence DÉDIÉE (dormante à 0). Stake RESTANT après le clawback du prix.
	// α-(b) : SEULEMENT sur signal muet réel (≥1 verdict « 0 » posté), jamais sur abstentions pures.
	if params.SilenceSlashBps > 0 && muteSignal {
		penalty := miner.Stake * params.SilenceSlashBps / 10000
		if penalty > miner.Stake {
			penalty = miner.Stake // borne de sûreté (inatteignable pour bps<=10000, défensif)
		}
		if penalty > 0 {
			miner.Stake -= penalty
			if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
				return err
			}
			k.addTreasury(ctx, penalty) // coins déjà au module (bond) -> compteur Trésorerie
			job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: job.MinerId, Amount: penalty})
			k.emitClawback(ctx, job.JobId, job.MinerId, penalty, 0, "silence_slash")
		}
	}
	return nil
}

// refundClient — rembourse au client min(`owed`, `dispo`) depuis le module : `owed` = ce qui RESTE dû au client,
// `dispo` = la source disponible (p.ex. le montant retiré du stake). Renvoie le montant RÉELLEMENT remboursé (0 si
// adresse invalide). PLAN-V2-FEE-HOLD : `owed` explicite permet de rembourser le SOLDE après la fee retenue.
func (k Keeper) refundClient(ctx context.Context, job *types.Job, owed, dispo uint64) uint64 {
	refund := owed
	if refund > dispo {
		refund = dispo
	}
	if refund == 0 {
		return 0
	}
	cliBz, e := k.addressCodec.StringToBytes(job.Client)
	if e != nil {
		return 0
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(refund)))
	if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(cliBz), coins); err != nil {
		return 0
	}
	return refund
}

// addTreasury — incrémente le compteur Trésorerie (coins déjà au module). Best-effort (ne bloque pas l'EndBlock).
func (k Keeper) addTreasury(ctx context.Context, amt uint64) {
	if amt == 0 {
		return
	}
	pools, err := k.Pools.Get(ctx)
	if err != nil {
		pools = types.Pools{}
	}
	pools.Treasury += amt
	_ = k.Pools.Set(ctx, pools)
}

// refundRetainedToClient (décision internal audit 2026-06-21 : reverse cut+Demand aux 2 clawbacks + burn différé (ii)) —
// rembourse le client depuis TOUTE la fee RETENUE au module, dans l'ordre, AVANT tout recours au bond :
//
//	(1) rétention minerNet (HeldFee) ;
//	(2) cut resté dans Pools (jamais distribué -> reversé au client) + reverse le Demand de CE job (treasury+team,
//	    s'il a été crédité au settle = client≠operator), borné ;
//	(3) burn DIFFÉRÉ (HeldBurn -> RENDU au client, JAMAIS brûlé sur un job non livré).
//
// Mute `miner.Demand` (l'appelant Set le miner). Renvoie le total remboursé au client : au PLEIN-HOLD = la fee
// entière -> le bond n'est PAS touché par l'appelant ; en hold partiel, le solde (= l'immédiat déjà payé au mineur)
// reste dû, couvert par le bond. Consomme HeldFee + HeldBurn ; décrément Pools borné. Best-effort (client invalide
// -> ce qui ne part pas va en Trésorerie, jamais perdu).
func (k Keeper) refundRetainedToClient(ctx context.Context, job *types.Job, params types.Params, miner *types.Miner) uint64 {
	sent := uint64(0)
	// (1) rétention minerNet
	if held, err := k.HeldFee.Get(ctx, job.JobId); err == nil && held > 0 {
		_ = k.HeldFee.Remove(ctx, job.JobId)
		s := k.refundClient(ctx, job, held, held)
		sent += s
		k.addTreasury(ctx, held-s) // client invalide -> résidu en Trésorerie
	}
	// (2) cut resté dans Pools (jamais distribué = CLAIM 2 internal audit) -> reversé au client ; + reverse le Demand
	cut := job.Fee * params.ProtocolFeeBps / 10000
	if cut > 0 {
		pools, perr := k.Pools.Get(ctx)
		if perr != nil {
			pools = types.Pools{}
		}
		rev := cut
		if avail := pools.Validators + pools.Team + pools.Treasury; rev > avail {
			rev = avail // ne reverse jamais plus que le solde présent des sous-pools de cut
		}
		if rev > 0 {
			if s := k.refundClient(ctx, job, rev, rev); s > 0 {
				rem := s // décrément borné des sous-pools (treasury, puis team, puis validators)
				for _, fld := range []*uint64{&pools.Treasury, &pools.Team, &pools.Validators} {
					take := *fld
					if take > rem {
						take = rem
					}
					*fld -= take
					rem -= take
				}
				_ = k.Pools.Set(ctx, pools)
				sent += s
			}
		}
		// reverse le Demand de ce job (treasury+team = cut − validators), SEULEMENT s'il a été crédité au settle.
		if job.Client != miner.Operator {
			demR := cut - cut*params.ValidatorRewardBps/10000
			if demR > miner.Demand {
				demR = miner.Demand
			}
			miner.Demand -= demR
		}
	}
	// (3) burn DIFFÉRÉ -> RENDU au client (PAS de burn sur un job non livré)
	if hb, err := k.HeldBurn.Get(ctx, job.JobId); err == nil && hb > 0 {
		_ = k.HeldBurn.Remove(ctx, job.JobId)
		s := k.refundClient(ctx, job, hb, hb)
		sent += s
		k.addTreasury(ctx, hb-s)
	}
	return sent
}

func (k Keeper) emitClawback(ctx context.Context, jobId, minerId string, slashed, refunded uint64, reason string) {
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(
		sdk.NewEvent("audit_clawback",
			sdk.NewAttribute("job_id", jobId),
			sdk.NewAttribute("miner_id", minerId),
			sdk.NewAttribute("reason", reason),
			sdk.NewAttribute("slashed", math.NewIntFromUint64(slashed).String()),
			sdk.NewAttribute("refunded", math.NewIntFromUint64(refunded).String()),
		),
	)
}

// releaseHeld (PLAN-V2-FEE-HOLD §A) — libère la fee RETENUE (HeldFee[jobId]) à l'OPÉRATEUR du primaire quand le
// job est VINDIQUÉ ou NON-AUDITÉ (paiement optimiste confirmé). No-op si rien retenu (hold_bps==0 au règlement).
// Best-effort : un échec d'adresse ne bloque pas l'EndBlock (les coins restent au module). Idempotent (Remove avant).
func (k Keeper) releaseHeld(ctx context.Context, job *types.Job) {
	// internal audit 2026-06-21 (ii) — FINALITÉ atteinte (vindiqué/non-audité) : le burn DIFFÉRÉ est brûlé MAINTENANT
	// (taxe sur le travail RÉUSSI). Indépendant de la rétention minerNet ci-dessous (peut exister même si held==0).
	if hb, e := k.HeldBurn.Get(ctx, job.JobId); e == nil && hb > 0 {
		if r := k.HeldBurn.Remove(ctx, job.JobId); r == nil {
			_ = k.bankKeeper.BurnCoins(ctx, types.ModuleName, sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(hb))))
		}
	}
	held, err := k.HeldFee.Get(ctx, job.JobId)
	if err != nil || held == 0 {
		return
	}
	if e := k.HeldFee.Remove(ctx, job.JobId); e != nil {
		return
	}
	miner, mErr := k.Miner.Get(ctx, job.MinerId)
	if mErr != nil {
		return
	}
	if toBz, aErr := k.addressCodec.StringToBytes(miner.Operator); aErr == nil {
		coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(held)))
		_ = k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(toBz), coins)
	}
}
