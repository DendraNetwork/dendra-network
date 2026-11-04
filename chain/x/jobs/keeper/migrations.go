package keeper

import (
	"fmt"

	sdk "github.com/cosmos/cosmos-sdk/types"
)

// Migrator carries the in-place store migrations of x/jobs, one per ConsensusVersion step. The module
// registers each step in `RegisterServices`, and `app/upgrades.go` runs them through `RunMigrations`.
type Migrator struct {
	k Keeper
}

func NewMigrator(k Keeper) Migrator { return Migrator{k: k} }

// Migrate1to2 is consensus epoch 15. It writes the five parameters the epoch
// added at 0 and nothing else: the judge maps start empty (a miner with no declaration is not a
// judge, and it declares with MsgDeclareJudge), no store is added or removed, and the renamed
// emission item keeps its key bytes (x/emission/types/keys.go).
//
// Reading the stored params with the new binary already yields 0 for the new fields, since proto3
// reads a missing field as the zero value, and writing them back at 0 produces the same bytes. This
// step therefore changes no byte of state, which a test measures; it exists so that the version map
// records the epoch and so that `RunMigrations` has the handler it refuses to go without. It does not
// run `Validate`: the new rules bound only the new fields, which are 0 here, and a refusal at the
// upgrade height on a rule the stored set predates would halt the one validator for a parameter
// governance can correct in a block.
func (m Migrator) Migrate1to2(ctx sdk.Context) error {
	p, err := m.k.Params.Get(ctx)
	if err != nil {
		return fmt.Errorf("epoch 15 migration: params unreadable: %w", err)
	}
	p.MinKitVersion = 0
	p.JudgeVerdictWindowBlocks = 0
	p.JudgeMissedDrawsMax = 0
	p.AvailSampleBps = 0
	p.CommitteeMinVrfPowerBps = 0
	return m.k.Params.Set(ctx, p)
}
