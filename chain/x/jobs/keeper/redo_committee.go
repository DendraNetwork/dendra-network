package keeper

import (
	"context"
	"encoding/hex"
	"strconv"
	"strings"

	"dendra/x/jobs/types"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// ADR-033 — COMITÉ DE RE-ADJUDICATION ANCRÉ (récidive de la faille ADR-032 sur un AUTRE chemin).
//
// Ce que ce fichier ferme. ADR-032 a authentifié l'ensemble votant du TALLY DE
// VERDICTS. Il n'a pas touché au « comité FRAIS » d'`AdjudicateDispute`, qui restait AUTO-DÉSIGNÉ :
// tout mineur enregistré hors comité d'origine pouvait poster un re-commit `<jobId>__redo__<id>` et
// entrer dans le tally. Le gate valait `len(fresh) >= CommitteeSize`, soit **3 IDENTITÉS**, et la
// majorité était calculée sur `totalRedoStake` = le stake des seuls AUTO-SÉLECTIONNÉS. Trois
// identités à `min_stake` détenaient donc 100 % du corps électoral, dans les deux sens :
//
//   - FAUX SLASH : re-commits divergents -> le primaire honnête perd `slash_leak_bps` de son bond.
//   - AUTO-VINDICATION : un tricheur RÉELLEMENT slashé recopie son propre commit sur 3 sybils ->
//     majorité stricte -> RESTITUTION depuis la Trésorerie + bond + récompense au « disputeur »
//     (lui-même). Ce second sens est un VOL, pas un grief : ADR-032 était un plafond de dégâts,
//     ici l'attaquant encaisse.
//
// Le correctif est le même invariant, appliqué au bon endroit : *voter exige d'avoir été TIRÉ*.
// Le comité de re-adjudication est tiré et ANCRÉ à l'OUVERTURE de la dispute (graine postérieure
// aux commits d'origine, donc non grindable par le contestataire), pondéré stake, et seuls ses
// membres comptent. Sans ancrage : fail-closed — ni slash, ni restitution.
//
// Note d'implémentation : l'ancre réutilise la collection `AuditCommittee` sous la clé
// `<jobId>__redo`. Les deux espaces de noms ne peuvent pas se télescoper (un jobId réel ne finit
// pas par `__redo`), et cela évite une régénération proto que l'ADR-032 avait déjà dû reporter.

// redoCommitteeDomain — séparation de domaine. DOIT différer de `auditCommitteeDomain` : sinon,
// à graine et job égaux, connaître le comité d'audit donnerait le comité de re-adjudication.
const redoCommitteeDomain = "dendra/redo-committee/v1|"

// redoAnchorKey — clé d'ancrage du comité frais.
func redoAnchorKey(jobId string) string { return jobId + "__redo" }

// humanDisputeKey — marque qu'une dispute HUMAINE (et non l'échantillonnage VRF) a ouvert ce job.
//
// Pourquoi une marque explicite plutôt que « pas de comité ancré ». Ce dernier test confond deux
// situations que rien ne distingue autrement une fois le job en `+disputed` : (a) le job n'a JAMAIS
// été mis en doute, quelqu'un l'a simplement contesté ; (b) le job A ÉTÉ échantillonné mais le
// tirage n'a rendu personne (réseau trop petit). Dans (b) le paiement est réellement non vérifié et
// ADR-028 veut en reprendre le prix ; dans (a) il n'y a rien à reprendre. Se fier à la seule absence
// d'ancre traiterait (b) comme (a) et désarmerait cette garde.
func humanDisputeKey(jobId string) string { return jobId + "__humandispute" }

// isHumanDispute — ce job a-t-il été ouvert par une dispute humaine ? (marque posée à l'ouverture)
func (k Keeper) isHumanDispute(ctx context.Context, jobId string) bool {
	raw, err := k.AuditCommittee.Get(ctx, humanDisputeKey(jobId))
	return err == nil && strings.TrimSpace(raw) != ""
}

// anchorRedoCommittee — tire et ANCRE le comité de re-adjudication d'un job contesté.
// Éligibles : mineurs enregistrés SAUF le primaire (il ne se juge pas), SAUF le disputeur (il ne
// juge pas sa propre accusation) et SAUF le comité d'ORIGINE (indépendance / anti-collusion —
// c'est l'exclusion que le code d'origine faisait déjà, la seule qu'il faisait).
func (k Keeper) anchorRedoCommittee(ctx context.Context, jobId, primaryId, disputerId string) error {
	orig, err := k.assignedCommittee(ctx, jobId, CommitteeSize)
	if err != nil {
		// Comité d'origine non re-dérivable -> on n'ancre pas. Fail-closed en aval.
		return err
	}
	var cands []minerWeight
	if err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if key == primaryId || (disputerId != "" && key == disputerId) || orig[key] {
			return false, nil
		}
		cands = append(cands, minerWeight{id: key, stake: m.Stake})
		return false, nil
	}); err != nil {
		return err
	}
	sdkCtx := sdk.UnwrapSDKContext(ctx)
	h := sdkCtx.BlockHeight()
	// GRAINE — elle doit être IMPRÉVISIBLE POUR LE DISPUTEUR, et ce n'est pas automatique.
	// En configuration par défaut (`committee_seed_source=0`), `committeeBaseSeed` rend le repli tel
	// quel ; un repli dérivé de la seule hauteur serait entièrement prévisible. Or le disputeur choisit
	// ET le job ET la hauteur à laquelle il diffuse sa tx : il pourrait énumérer les hauteurs hors
	// chaîne jusqu'à ce que le tirage asseye ses complices, puis diffuser à la bonne. Le chemin d'audit
	// VRF n'a pas ce degré de liberté — sa hauteur lui est imposée par l'échantillonnage.
	//
	// Correctif : lier le repli au HASH DU BLOC de la hauteur d'ouverture, que le disputeur ne connaît
	// pas au moment de signer. À défaut (vote-extensions inactives, donc pas de BlockHash posé), on
	// n'ancre PAS : mieux vaut une adjudication fail-closed — dont l'issue est désormais bornée et
	// non punitive — qu'un comité que l'accusateur a choisi.
	fallback := ""
	if bh, ok := k.GetBlockHash(ctx, h); ok && len(bh) > 0 {
		fallback = "dispute:" + hex.EncodeToString(bh)
	}
	seed := k.committeeBaseSeed(ctx, fallback)
	if seed == "" {
		sdkCtx.Logger().Error("SECURITE: aucune graine non predictible a l'ouverture de la dispute (ni VRF decentralisee, ni hash de bloc) -> comite de re-adjudication NON ancre. Un tirage sur graine devinable laisserait l'accusateur choisir ses juges.", "job_id", jobId, "height", h)
		return nil
	}

	size := auditCommitteeDrawSize
	// Un comité plus petit que le seuil de décision rendrait l'adjudication inerte.
	if size < CommitteeSize {
		size = CommitteeSize
	}
	members := drawMembersWithDomain(redoCommitteeDomain, seed, jobId, cands, size)
	if len(members) == 0 {
		// Aucun éligible (réseau trop petit) : on n'ancre rien plutôt qu'un ensemble vide qui
		// se lirait comme « tout le monde ». Fail-closed en aval. Mais SILENCIEUX serait un piège
		// (internal audit 07-20) : sur un petit réseau, l'adjudication permissionless est alors durablement
		// indisponible, et rien ne le dirait — l'opérateur croirait le mécanisme actif. Le vivier
		// vaut N−(primaire+disputeur+comité d'origine), donc au moins 6 mineurs sont nécessaires.
		sdkCtx.Logger().Error("SECURITE: vivier trop petit pour tirer un comite de re-adjudication -> adjudication permissionless INDISPONIBLE sur ce job (le timeout d'audit reste la voie de sortie). Il faut plus de mineurs enregistres.",
			"job_id", jobId, "candidats", len(cands), "sieges_vises", size)
		// Un log est visible pour l'opérateur du nœud, mais ni requêtable ni remontable dans
		// l'exporter/Grafana — or « l'adjudication permissionless est indisponible » est précisément
		// une condition qu'on veut voir en supervision (internal audit 07-20). D'où l'événement à côté du log.
		sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
			"redo_committee_unavailable",
			sdk.NewAttribute("job_id", jobId),
			sdk.NewAttribute("candidates", strconv.Itoa(len(cands))),
			sdk.NewAttribute("seats_wanted", strconv.Itoa(size)),
		))
		return nil
	}
	if err := k.AuditCommittee.Set(ctx, redoAnchorKey(jobId), strings.Join(members, ",")); err != nil {
		return err
	}
	sdkCtx.EventManager().EmitEvent(sdk.NewEvent(
		"redo_committee_anchored",
		sdk.NewAttribute("job_id", jobId),
		sdk.NewAttribute("seats", strconv.Itoa(len(members))),
		sdk.NewAttribute("committee", strings.Join(members, ",")),
	))
	return nil
}

// refundDisputeBond — rend son bond au disputeur quand un comité A ÉTÉ CONVOQUÉ mais n'a pas rendu
// de verdict : la panne est alors celle du réseau, et l'invariant ① (« ne jamais facturer une panne
// d'infra à l'honnête ») vaut aussi pour un disputeur.
//
// ⚠️ NE COUVRE PAS la dispute sans comité (job jamais échantillonné) : là, personne n'a été empêché
// de répondre, la dispute est simplement infondée et son bond part en Trésorerie — traité en amont
// dans `resolveDisputedAudit`. Confondre les deux ferait du bond un dépôt remboursable, donc du
// grief une action gratuite.
//
// Mute `job.DisputeBond` à 0 : c'est ce qui rend l'opération non rejouable, y compris si
// `AdjudicateDispute` passait ensuite (il ne verrait plus de bond à rembourser ni à confisquer).
// No-op si `dispute_bond=0` (le réglage courant) ou si le disputeur est inconnu.
func (k Keeper) refundDisputeBond(ctx context.Context, job *types.Job) error {
	if job.DisputeBond == 0 || job.Disputer == "" {
		return nil
	}
	bz, err := k.addressCodec.StringToBytes(job.Disputer)
	if err != nil {
		return nil // adresse illisible : on ne brûle pas, on laisse la trace au module
	}
	coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(job.DisputeBond)))
	if err := k.bankKeeper.SendCoinsFromModuleToAccount(ctx, types.ModuleName, sdk.AccAddress(bz), coins); err != nil {
		return err
	}
	job.DisputeBond = 0
	sdk.UnwrapSDKContext(ctx).EventManager().EmitEvent(sdk.NewEvent(
		"dispute_bond_refunded",
		sdk.NewAttribute("job_id", job.JobId),
		sdk.NewAttribute("disputer", job.Disputer),
	))
	return nil
}

// countRedoResponders — combien de MEMBRES ANCRÉS du comité redo ont réellement re-commité pour ce
// job. Sert l'instrumentation demandée par le internal audit (07-20) : la propriété de sécurité est le RATIO
// ⌈2/3⌉, donc K=15 est un pur réglage de LIVENESS qui doit se calibrer sur une donnée, pas se deviner.
// Émettre `répondants / sièges` à chaque dispute qui atteint son échéance donne exactement cette donnée.
func (k Keeper) countRedoResponders(ctx context.Context, jobId string, allowed map[string]bool) int {
	prefix := jobId + "__redo__"
	n := 0
	_ = k.Commit.Walk(ctx, commitRange(prefix), func(key string, _ types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) && allowed[strings.TrimPrefix(key, prefix)] {
			n++
		}
		return false, nil
	})
	return n
}

// redoCommitteeAllowed — lit l'ancre. `ok=false` = AUCUN ancrage -> l'appelant DOIT refuser toute
// décision qui déplace du capital (slash dur comme restitution).
func (k Keeper) redoCommitteeAllowed(ctx context.Context, jobId string) (map[string]bool, bool) {
	raw, err := k.AuditCommittee.Get(ctx, redoAnchorKey(jobId))
	if err != nil || strings.TrimSpace(raw) == "" {
		return nil, false
	}
	allowed := make(map[string]bool, 16)
	for _, id := range strings.Split(raw, ",") {
		if id = strings.TrimSpace(id); id != "" {
			allowed[id] = true
		}
	}
	if len(allowed) == 0 {
		return nil, false
	}
	return allowed, true
}
