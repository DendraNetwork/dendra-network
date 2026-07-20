package types

import "fmt"

// NewParams creates a new Params instance.
func NewParams(reserveReleaseBps, workSplitBps, availSplitBps, workGateBps, epochBlocks uint64) Params {
	return Params{
		ReserveReleaseBps: reserveReleaseBps,
		WorkSplitBps:      workSplitBps,
		AvailSplitBps:     availSplitBps,
		WorkGateBps:       workGateBps,
		EpochBlocks:       epochBlocks,
	}
}

// DefaultParams returns a default set of parameters (v5, ADR-021).
// reserve_release_bps=2200 (22 %) est appliqué UNE fois par époque ; epoch_blocks = ~1 an de blocs
// (~1 s/bloc) -> ~22 %/AN de la Réserve restante (décroissant). Pour un devnet observable, override
// via genesis (emission.params) avec un epoch_blocks court + un reserve_release_bps faible.
func DefaultParams() Params {
	return NewParams(2200, 5000, 2000, 15000, 31_536_000)
}

// Validate validates the set of params.
func (p Params) Validate() error {
	if p.WorkSplitBps+p.AvailSplitBps > 10000 {
		return fmt.Errorf("work_split_bps + avail_split_bps > 10000 (la securite = le reste)")
	}
	if p.ReserveReleaseBps > 10000 {
		return fmt.Errorf("reserve_release_bps > 10000")
	}
	// BORNE HAUTE DE `epoch_blocks`, qui n'en avait aucune : pres de 2^64, `last + epochBlocks`
	// debordait, la comparaison s'inversait, et l'epoque tombait a CHAQUE bloc — une proposition
	// « n'emets plus jamais » produisait l'emission maximale. Plafond a 2^32 blocs (~136 ans a
	// 1 s/bloc) : au-dela, ce n'est pas une intention, c'est une erreur de saisie.
	//
	// ⚠️ `0` N'EST PAS REFUSE ICI, et c'etait ma premiere version — a tort. Le genesis par defaut
	// (`config.yml`, `GenesisState{}`) ne pose AUCUN param d'emission : tout y est a zero. Refuser 0
	// a la validation aurait fait paniquer `InitGenesis` au demarrage d'une chaine neuve, soit
	// remplacer une fuite de valeur par une chaine qui ne demarre pas. `0` = « non configure »,
	// traite au RUNTIME comme emission desactivee (cf. epoch.go) — jamais comme un repli sur les defauts.
	if p.EpochBlocks > 1<<32 {
		return fmt.Errorf("epoch_blocks > 2^32 (deborde le calcul d'echeance et inverse la condition -> emission a chaque bloc)")
	}
	return nil
}
