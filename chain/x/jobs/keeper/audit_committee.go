package keeper

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"

	"dendra/x/jobs/types"
)

// ADR-032 — COMITÉ D'AUDIT ANCRÉ.
//
// Le problème que ce fichier ferme (faille CRITIQUE) : le tally des verdicts
// n'authentifiait l'ensemble votant que par « être un mineur enregistré ». Or `CreateMiner` est
// permissionless et le bond est remboursé à l'exit -> 4 identités à `min_stake` (0,2 DNDR) suffisaient
// à faire slasher 80 % du stake d'un mineur HONNÊTE, pour le coût du gas. La garde anti-faux-slash
// (le veto N=5) était devenue le VECTEUR du faux-slash.
//
// Le principe violé : *une action qui ne coûte rien et ne risque rien n'est pas une primitive de
// sécurité.* Le correctif ne durcit pas la règle de décision — il authentifie l'ENSEMBLE VOTANT :
// voter exige d'avoir été TIRÉ AU SORT, et le tirage est PONDÉRÉ PAR LE STAKE. L'attaque par
// multiplication d'identités devient une attaque par capital, correctement tarifée.

// auditCommitteeDomain — séparation de domaine du tirage de comité d'audit. Distinct de `auditDomain`
// (tirage OUI/NON de l'audit) et de la graine d'assignation : deux tirages sur la même graine de bloc
// ne doivent jamais être corrélés.
const auditCommitteeDomain = "dendra/audit-committee/v1|"

// auditCommitteeDrawSize — nombre de membres-juges tirés et ancrés par job audité.
//
// SUR-ÉCHANTILLONNAGE ASSUMÉ (ADR-032 §e). Tirer exactement `audit_min_quorum` membres rendrait le
// quorum inatteignable en pratique : seuls les mineurs qui font effectivement tourner un `judge_worker`
// répondent, et rien on-chain ne les distingue aujourd'hui. On tire donc large (15) en gardant le quorum
// inchangé : la borne anti-sybil tient (il faut être tiré, et le tirage est pondéré stake) pendant que la
// participation redevient atteignable.
//
// ⚠️ ÉCART ASSUMÉ À LA SPEC, signalé au canal : l'ADR demandait un param gouverné
// `audit_committee_draw_size`. `Params` est un message PROTO (`params.pb.go`) — l'ajouter exigerait une
// régénération proto, que la même ADR interdit par ailleurs pour le stockage. Constante Go en attendant
// la prochaine fenêtre de régén, comme `CommitteeSize` (committee.go) l'est déjà. La propriété de
// sécurité ne dépend pas de la gouvernabilité de cette valeur ; seule la liveness en dépend.
const auditCommitteeDrawSize = 15

// drawAuditMembers — cœur PUR du tirage (testable sans keeper), déterministe et SANS FLOTTANT.
//
// Réutilise volontairement le scoring éprouvé de `selectCommittee` : score = H(graine|id) / stake,
// comparé par PRODUIT CROISÉ en entiers -> identique sur tous les validateurs. Les `k` plus PETITS
// scores sont retenus : plus le stake est grand, plus le score est petit, plus le mineur est tiré.
// Splitter son stake en N identités ne change donc PAS l'espérance de sièges — c'est exactement la
// propriété anti-sybil qui manquait.
//
// Renvoie une liste TRIÉE (ordre du score) : l'ancrage doit être reproductible octet pour octet, sinon
// deux validateurs écriraient des chaînes différentes pour le même tirage et la chaîne forkerait.
func drawAuditMembers(seed, jobId string, cands []minerWeight, k int) []string {
	return drawMembersWithDomain(auditCommitteeDomain, seed, jobId, cands, k)
}

// drawMembersWithDomain — même tirage, DOMAINE paramétré. Extrait pour ADR-033 (comité de
// RE-ADJUDICATION) : deux comités tirés pour le même job sur la même graine doivent être
// décorrélés, sinon connaître l'un donne l'autre. Le domaine est le seul séparateur.
func drawMembersWithDomain(domain, seed, jobId string, cands []minerWeight, k int) []string {
	if k <= 0 || len(cands) == 0 {
		return nil
	}
	h := sha256.Sum256([]byte(domain + seed + "|" + jobId))
	drawSeed := hex.EncodeToString(h[:])

	// selectCommittee rend un SET (ordre de map, non déterministe à l'itération) ; on a besoin d'une
	// LISTE ordonnée. On rejoue donc le même tri ici, puis on coupe.
	type scored struct {
		id    string
		h     [32]byte
		stake uint64
	}
	all := make([]scored, 0, len(cands))
	for _, m := range cands {
		all = append(all, scored{id: m.id, h: sha256.Sum256([]byte(drawSeed + "|" + m.id)), stake: m.stake})
	}
	sort.Slice(all, func(i, j int) bool { return lessByStakeScore(all[i].h, all[i].stake, all[i].id, all[j].h, all[j].stake, all[j].id) })

	if k > len(all) {
		k = len(all) // moins d'éligibles que de sièges -> on prend tout le monde (ADR-032 §b)
	}
	out := make([]string, 0, k)
	for i := 0; i < k; i++ {
		out = append(out, all[i].id)
	}
	return out
}

// drawAuditCommittee — tire et renvoie les membres habilités à juger `jobId`.
// Éligibles : tous les mineurs enregistrés SAUF le primaire (il ne se juge pas) et SAUF le disputeur
// s'il est lui-même mineur (il ne juge pas sa propre accusation).
func (k Keeper) drawAuditCommittee(ctx context.Context, seed, jobId, primaryId, disputerId string) ([]string, error) {
	var cands []minerWeight
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		if key == primaryId || (disputerId != "" && key == disputerId) {
			return false, nil
		}
		cands = append(cands, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	size := auditCommitteeDrawSize
	// Filet : un comité plus petit que le quorum rendrait le slash mathématiquement inerte. On ne
	// laisse pas un réglage produire une garde qui ne peut jamais mordre.
	if p, pErr := k.Params.Get(ctx); pErr == nil && p.AuditMinQuorum > asUint64(size) {
		size = int(p.AuditMinQuorum)
	}
	return drawAuditMembers(seed, jobId, cands, size), nil
}

// asUint64 — adaptateur de comparaison (AuditMinQuorum est uint64, la taille de tirage est un int).
func asUint64(n int) uint64 {
	if n < 0 {
		return 0
	}
	return uint64(n)
}

// auditCommitteeAllowed — lit la liste ANCRÉE et la rend sous forme d'ensemble.
// `ok=false` signifie « aucun ancrage » -> l'appelant DOIT retomber en fail-closed (aucun slash dur).
// C'est volontairement asymétrique : rater la capture d'un tricheur est borné et -EV (ADR-025), alors
// que slasher un honnête est catastrophique et irréversible en réputation.
func (k Keeper) auditCommitteeAllowed(ctx context.Context, jobId string) (map[string]bool, bool) {
	raw, err := k.AuditCommittee.Get(ctx, jobId)
	if err != nil || strings.TrimSpace(raw) == "" {
		return nil, false
	}
	allowed := make(map[string]bool, 8)
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
