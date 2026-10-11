package app

import (
	"context"
	"fmt"

	storetypes "cosmossdk.io/store/types"
	upgradetypes "cosmossdk.io/x/upgrade/types"
	"github.com/cosmos/cosmos-sdk/types/module"
)

// Version upgrades coordinated by the chain itself.
//
// Wiring `UpgradeKeeper` (module registered, `SetModuleVersionMap` called at genesis) registers no
// named handler. Without one, two things follow, the worst first:
//
//  1. A passed `MsgSoftwareUpgrade` proposal halts the chain at the planned height, and the node then
//     refuses to restart with "unknown upgrade <name>". The standard upgrade path becomes a guaranteed
//     outage that nobody notices before triggering it in production.
//  2. Without that path, every consensus-breaking change is done by hand: stop all validators, swap
//     the binary, restart. With 2 validators at 50/50, stopping one stops block production, which at
//     least rules out a mixed-version window. From 4 validators on, two nodes on the old code and two
//     on the new one fork instead of halting.
//
// What this file does not provide: an upgrade handler coordinates the moment of the change (every
// node stops at the same height, and a node still running the old binary refuses to continue instead
// of forking silently). It does not make a logic change replayable from genesis. The history really
// contains two behaviours and no migration rewrites blocks that are already signed; the remedy for
// that remains snapshots plus state-sync, which already ship.

// upgradePlans lists the named plans this binary can apply. Each must match a proposal's `name`
// exactly, otherwise the chain halts on "unknown upgrade".
//
// The list has been deliberately empty since epoch 14 (ADR-048 item 7). The two plans that lived here,
// `v2-anchored-committees` and `v3-audit-unwind`, synchronised switches on chains that no longer
// exist: `dendra-testnet` starts from a fresh genesis at epoch 14, so no node will stop at either
// height, and keeping them would advertise upgrade paths nobody can take. The mechanism stays (the
// handler loop and the store loader), so the next plan is one line here.
//
// A plan that adds or removes a store must fill `Added`/`Deleted` in the loader below for its own name.
var upgradePlans = []string{}

// RegisterUpgradeHandlers registers the handler of every named plan plus the store loader. Called from
// New() once the keepers are built.
func (app *App) RegisterUpgradeHandlers() {
	for _, name := range upgradePlans {
		app.UpgradeKeeper.SetUpgradeHandler(
			name,
			func(ctx context.Context, _ upgradetypes.Plan, fromVM module.VersionMap) (module.VersionMap, error) {
				// RunMigrations runs the migrations of the modules whose ConsensusVersion changed. A logic change at
				// a constant ConsensusVersion has nothing to migrate, and the handler then only synchronizes the
				// switch. Having no migration does not mean having no effect.
				return app.ModuleManager.RunMigrations(ctx, app.Configurator(), fromVM)
			},
		)
	}

	// Store loader. It is needed only when an upgrade adds or removes a module: without it the new
	// module's store does not exist at the first block after the upgrade, and the node panics.
	// `ReadUpgradeInfoFromDisk` reads the plan the UpgradeKeeper wrote before halting.
	upgradeInfo, err := app.UpgradeKeeper.ReadUpgradeInfoFromDisk()
	if err != nil {
		panic(fmt.Errorf("reading the upgrade plan from disk: %w", err))
	}
	// The check must cover every plan, so it loops over the same list the handlers come from. A missing
	// loader stays invisible until the first upgrade that adds a store.
	for _, name := range upgradePlans {
		if upgradeInfo.Name == name && !app.UpgradeKeeper.IsSkipHeight(upgradeInfo.Height) {
			app.SetStoreLoader(upgradetypes.UpgradeStoreLoader(
				upgradeInfo.Height,
				&storetypes.StoreUpgrades{},
			))
		}
	}
}
