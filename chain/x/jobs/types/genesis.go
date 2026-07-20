package types

import "fmt"

// DefaultGenesis returns the default genesis state
func DefaultGenesis() *GenesisState {
	return &GenesisState{
		Params:   DefaultParams(),
		MinerMap: []Miner{}, JobMap: []Job{}, Pools: nil, CommitMap: []Commit{}, BeaconMap: []Beacon{}}
}

// Validate performs basic genesis state validation returning an error upon any
// failure.
func (gs GenesisState) Validate() error {
	minerIndexMap := make(map[string]struct{})

	for _, elem := range gs.MinerMap {
		index := fmt.Sprint(elem.MinerId)
		if _, ok := minerIndexMap[index]; ok {
			return fmt.Errorf("duplicated index for miner")
		}
		minerIndexMap[index] = struct{}{}
	}
	jobIndexMap := make(map[string]struct{})

	for _, elem := range gs.JobMap {
		index := fmt.Sprint(elem.JobId)
		if _, ok := jobIndexMap[index]; ok {
			return fmt.Errorf("duplicated index for job")
		}
		jobIndexMap[index] = struct{}{}
	}
	commitIndexMap := make(map[string]struct{})

	for _, elem := range gs.CommitMap {
		index := fmt.Sprint(elem.JobId)
		if _, ok := commitIndexMap[index]; ok {
			return fmt.Errorf("duplicated index for commit")
		}
		commitIndexMap[index] = struct{}{}
	}
	beaconIndexMap := make(map[string]struct{})

	for _, elem := range gs.BeaconMap {
		index := fmt.Sprint(elem.JobId)
		if _, ok := beaconIndexMap[index]; ok {
			return fmt.Errorf("duplicated index for beacon")
		}
		beaconIndexMap[index] = struct{}{}
	}

	if err := gs.Params.Validate(); err != nil {
		return err
	}
	// INVARIANT #8 (internal audit 2026-06-26) — un GENESIS ne peut pas non plus livrer des params rendant le self-dealing
	// Sybil +EV (drain d'émission) : même garde que `MsgUpdateParams`, ici pour `validate-genesis` (l'entrypoint
	// l'exécute) ET tout chemin de chargement de genesis. Le subside washé utilise le WorkGateBps de JOBS.
	if !gs.Params.WashSubsidyNegativeEV(gs.Params.WorkGateBps) {
		return fmt.Errorf("invariant #8 viole : params de genesis rendraient le self-dealing Sybil +EV (drain d'emission) ; baisse work_gate_bps OU augmente cut+burn vs team+treasury")
	}
	return nil
}
