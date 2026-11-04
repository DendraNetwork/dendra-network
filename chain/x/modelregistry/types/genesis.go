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
	// Models seeded at genesis: non-empty id and uniqueness. A duplicate or empty id would make the
	// registry ambiguous, and a lookup by id would silently resolve to whichever entry landed last.
	seen := map[string]bool{}
	for _, m := range gs.Models {
		if m.Id == "" {
			return fmt.Errorf("empty model id in genesis")
		}
		if seen[m.Id] {
			return fmt.Errorf("duplicate model id in genesis: %s", m.Id)
		}
		seen[m.Id] = true
	}
	return nil
}
