package app

import (
	"context"
	"fmt"

	storetypes "cosmossdk.io/store/types"
	upgradetypes "cosmossdk.io/x/upgrade/types"
	"github.com/cosmos/cosmos-sdk/types/module"
)

// VERSION UPGRADES COORDINATED BY THE CHAIN ITSELF.
//
// What this file closes. `UpgradeKeeper` was wired — module registered, `SetModuleVersionMap` called
// at genesis — but NO named handler was registered. The consequences, in order of severity:
//
//  1. A passed `MsgSoftwareUpgrade` proposal HALTS the chain at the planned height and refuses to
//     restart: "unknown upgrade <name>". Cosmos's official upgrade path was therefore a guaranteed
//     outage, and nobody finds that out until they have triggered it in production.
//  2. Without that path, every consensus-breaking change is done by hand: stop all validators, swap
//     the binary, restart. With 2 validators at 50/50, stopping one stops block production — which at
//     least makes a mixed-version window impossible. At 4 validators and beyond that safety net is
//     gone: two nodes on the old code and two on the new one is a FORK, not an outage.
//
// WHAT THIS FILE DOES NOT PROVIDE, stated plainly. An upgrade handler coordinates the MOMENT of the
// change: everyone stops at the same height, and a node still running the old binary refuses to
// continue instead of forking silently. It does NOT make a LOGIC change replayable from genesis. The
// history genuinely contains two behaviours, and no migration rewrites blocks that are already signed.
// For that, the remedy remains snapshots plus state-sync, which are already shipped. Confusing the two
// leads to believing the history is repaired when it is not.

// upgradePlans — the NAMED plans this binary can apply. Each must match a proposal's `name` exactly,
// otherwise the chain halts on "unknown upgrade".
//
// ⛔ EMPTY SINCE EPOCH 14 (ADR-048 item 7), AND THAT IS A DECISION. The two plans that lived here,
// `v2-anchored-committees` and `v3-audit-unwind`, synchronised switches on chains that no longer exist:
// `dendra-testnet` starts from a fresh genesis at epoch 14, so no node will ever stop at either height.
// Keeping them would advertise upgrade paths nobody can take. The MECHANISM stays — handler loop and store
// loader — so the next plan is one line here, not a rediscovery of why the loader was missing.
//
// A plan that ADDS or REMOVES a store must fill `Added`/`Deleted` in the loader below for its own name.
var upgradePlans = []string{}

// RegisterUpgradeHandlers registers the handler of every named plan plus the store loader. Called from
// New() once the keepers are built.
func (app *App) RegisterUpgradeHandlers() {
	for _, name := range upgradePlans {
		app.UpgradeKeeper.SetUpgradeHandler(
			name,
			func(ctx context.Context, _ upgradetypes.Plan, fromVM module.VersionMap) (module.VersionMap, error) {
				// RunMigrations runs the migrations of modules whose ConsensusVersion has moved. A logic
				// change at CONSTANT ConsensusVersion has nothing to migrate: the handler then exists only
				// to SYNCHRONIZE the switch. "No migration" is not the same thing as "no effect".
				return app.ModuleManager.RunMigrations(ctx, app.Configurator(), fromVM)
			},
		)
	}

	// STORE LOADER. Needed only when an upgrade ADDS or REMOVES a module: without it the new module's
	// store does not exist at the first post-upgrade block and the node panics.
	// `ReadUpgradeInfoFromDisk` reads the plan the UpgradeKeeper wrote before halting.
	upgradeInfo, err := app.UpgradeKeeper.ReadUpgradeInfoFromDisk()
	if err != nil {
		panic(fmt.Errorf("reading the upgrade plan from disk: %w", err))
	}
	// ⛔ THE CHECK MUST NAME EVERY PLAN, NOT THE LATEST ONE — hence the loop over the same list the
	// handlers come from. A missing loader is invisible until the first upgrade that adds a store.
	for _, name := range upgradePlans {
		if upgradeInfo.Name == name && !app.UpgradeKeeper.IsSkipHeight(upgradeInfo.Height) {
			app.SetStoreLoader(upgradetypes.UpgradeStoreLoader(
				upgradeInfo.Height,
				&storetypes.StoreUpgrades{},
			))
		}
	}
}
