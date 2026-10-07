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
		// ADR-039 — the identifier must be DERIVED here too, not only on the message path. See
		// ValidateMinerIdentity for why one guarded door out of two guards nothing.
		if err := ValidateMinerIdentity(elem.MinerId, elem.Creator); err != nil {
			return err
		}
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
	// INVARIANT #8 — a GENESIS may not ship params that make Sybil self-dealing +EV either, which would
	// let an operator drain emission by paying itself. This is the same guard as `MsgUpdateParams`.
	// The washed subsidy is measured against the jobs WorkGateBps.
	//
	// ⚠️ THIS FILE IS THE `validate-genesis` PATH, NOT THE LOADING PATH. `Validate()` is reached
	// through `AppModule.ValidateGenesis`, which the CLI and the launch kit call; `InitChain` calls
	// `InitGenesis` and never this. Both paths are covered only because `keeper/genesis.go` REPEATS
	// the check at load time, deliberately and with its reasons stated there.
	//
	// So the duplication is load-bearing: deleting the keeper-side copy as redundancy would leave the
	// invariant enforced by the tool an operator chose to run, which is a convention, not a guard.
	if !gs.Params.WashSubsidyNegativeEV(gs.Params.WorkGateBps) {
		return fmt.Errorf("invariant #8 violated: these genesis params would make Sybil self-dealing +EV (emission drain); lower work_gate_bps OR raise cut+burn relative to team+treasury")
	}
	return nil
}
