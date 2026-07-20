package keeper

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math/big"
	"sort"

	"dendra/x/jobs/types"
)

// CommitteeSize : nombre de mineurs assignes a un job (gouvernable plus tard via params).
const CommitteeSize = 3

// minerWeight : identite + bond d'un mineur, pour la selection ponderee.
type minerWeight struct {
	id    string
	stake uint64
}

// selectCommittee — cœur PUR (testable sans keeper) de la selection de comite.
//
// GO-04 -- PONDERATION PAR LE STAKE : chaque mineur recoit un score = hash(graine|minerId) / stake,
// compare par PRODUIT CROISE en big.Int (entiers, ZERO flottant -> identique sur tous les validateurs).
// Les `size` plus PETITS scores sont retenus : plus le stake est grand, plus le score est petit, plus le
// mineur est selectionne. Le BOND etant reel (GO-13), (a) controler plus de slots coute proportionnellement
// plus de coins bloques, et (b) SPLITTER son stake en N identites ne change pas l'influence totale -> la
// selection devient anti-sybil (vs l'ancienne selection UNIFORME ou chaque identite comptait pareil). Un
// stake nul (mineur entierement slashe) est relegue en toute fin (jamais prioritaire).
func selectCommittee(seed string, miners []minerWeight, size int) map[string]bool {
	type scored struct {
		id    string
		h     [32]byte
		stake uint64
	}
	all := make([]scored, 0, len(miners))
	for _, m := range miners {
		all = append(all, scored{id: m.id, h: sha256.Sum256([]byte(seed + "|" + m.id)), stake: m.stake})
	}
	sort.Slice(all, func(i, j int) bool {
		return lessByStakeScore(all[i].h, all[i].stake, all[i].id, all[j].h, all[j].stake, all[j].id)
	})
	set := map[string]bool{}
	for i := 0; i < size && i < len(all); i++ {
		set[all[i].id] = true
	}
	return set
}

// lessByStakeScore — comparateur PONDÉRÉ PAR LE STAKE, source unique de vérité du tri.
//
// h_i/stake_i < h_j/stake_j  <=>  h_i*stake_j < h_j*stake_i  (produit croisé big.Int, EXACT).
// Entiers uniquement, ZÉRO flottant -> le même ordre sur tous les validateurs, sinon la chaîne forke.
// Un stake nul (mineur entièrement slashé) est relégué en fin, jamais prioritaire ; égalité départagée
// par id pour le déterminisme.
//
// Extrait de selectCommittee pour être PARTAGÉ avec drawAuditMembers (ADR-032) : les deux tirages
// doivent trier à l'identique. Deux copies de cette arithmétique auraient divergé un jour, et une
// divergence d'ordre entre validateurs se paie par un fork, pas par un test rouge.
func lessByStakeScore(hi [32]byte, si uint64, idi string, hj [32]byte, sj uint64, idj string) bool {
	if si == 0 || sj == 0 {
		if si != sj {
			return si > sj // un stake>0 passe avant un stake==0
		}
		return idi < idj
	}
	li := new(big.Int).Mul(new(big.Int).SetBytes(hi[:]), new(big.Int).SetUint64(sj))
	lj := new(big.Int).Mul(new(big.Int).SetBytes(hj[:]), new(big.Int).SetUint64(si))
	if c := li.Cmp(lj); c != 0 {
		return c < 0
	}
	return idi < idj
}

// assignedCommittee derive le comite d'un job de facon DETERMINISTE, PONDEREE PAR LE STAKE (GO-04).
//
// H6 -- ANTI-GRINDING : si un BEACON a ete fixe pour ce job (par open-job, a partir d'un alea de bloc
// imprevisible), la graine = beacon|jobId -> le createur ne peut PAS grinder le jobId pour choisir un
// comite complice (il ne controle pas le beacon). Sans beacon (anciens jobs) : jobId seul (retro-compat).
func (k Keeper) assignedCommittee(ctx context.Context, jobId string, size int) (map[string]bool, error) {
	seed := jobId
	if b, err := k.Beacon.Get(ctx, jobId); err == nil {
		// Beacon présent mais graine VIDE = comité en attente de révélation différée (H6) : NE PAS
		// retomber sur jobId seul (grindable). On refuse jusqu'à la révélation par l'EndBlocker.
		if b.Seed == "" {
			return nil, fmt.Errorf("comite non revele pour le job %q (revelation differee en attente)", jobId)
		}
		seed = b.Seed + "|" + jobId
	}
	var miners []minerWeight
	err := k.Miner.Walk(ctx, nil, func(key string, m types.Miner) (bool, error) {
		miners = append(miners, minerWeight{id: key, stake: m.Stake})
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	return selectCommittee(seed, miners, size), nil
}
