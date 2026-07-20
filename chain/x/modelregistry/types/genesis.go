package types

import "fmt"

// DefaultGenesis returns the default genesis state
func DefaultGenesis() *GenesisState {
	return &GenesisState{
		Params: DefaultParams(),
	}
}

// Validate performs basic genesis state validation returning an error upon any
// failure.
func (gs GenesisState) Validate() error {
	if err := gs.Params.Validate(); err != nil {
		return err
	}
	// modèles posés au genesis : id non vide + unicité (évite un registre incohérent).
	seen := map[string]bool{}
	for _, m := range gs.Models {
		if m.Id == "" {
			return fmt.Errorf("model id vide dans le genesis")
		}
		if seen[m.Id] {
			return fmt.Errorf("model id duplique dans le genesis: %s", m.Id)
		}
		seen[m.Id] = true
	}
	return nil
}
