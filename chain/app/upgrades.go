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

// UpgradeName — the name of the plan to be voted on. It MUST match the proposal's `name` field
// exactly, otherwise the chain halts on "unknown upgrade".
const UpgradeName = "v2-anchored-committees"

// UpgradeNameAuditUnwind — ADR-044 gesture 2 (consensus epoch 13). A SECOND named plan, not a rename:
// the old name must keep resolving. A chain that halted on `v2-anchored-committees` and has not yet
// been restarted would meet a binary that no longer knows the plan it is stopped at, and halt on
// "unknown upgrade" — an upgrade path that breaks the upgrade it is replacing.
//
// The handler runs no migration, and that is not a shortcut. `audit_unwind_blocks` defaults to 0 and
// proto3 omits a zero, so the stored Params keep the SAME bytes across the switch: there is nothing to
// rewrite. What the plan buys is SYNCHRONISATION — every validator starts reading field 41 at the same
// height. Without it, arming the parameter later would find some nodes deferring and others unwinding.
const UpgradeNameAuditUnwind = "v3-audit-unwind"

// RegisterUpgradeHandlers registers the handler for the current plan plus the store loader. Called
// from New() once the keepers are built.
func (app *App) RegisterUpgradeHandlers() {
	app.UpgradeKeeper.SetUpgradeHandler(
		UpgradeName,
		func(ctx context.Context, _ upgradetypes.Plan, fromVM module.VersionMap) (module.VersionMap, error) {
			// RunMigrations runs the migrations of modules whose ConsensusVersion has moved. A logic
			// change at CONSTANT ConsensusVersion (the case of ADR-032/033) has nothing to migrate:
			// here the handler exists only to SYNCHRONIZE the switch, which is exactly what is wanted.
			// "No migration" is not the same thing as "no effect".
			return app.ModuleManager.RunMigrations(ctx, app.Configurator(), fromVM)
		},
	)

	app.UpgradeKeeper.SetUpgradeHandler(
		UpgradeNameAuditUnwind,
		func(ctx context.Context, _ upgradetypes.Plan, fromVM module.VersionMap) (module.VersionMap, error) {
			// Same shape as above and for the same reason: no ConsensusVersion moves, so there is no
			// migration to run. `audit_unwind_blocks` arrives at its zero value, which is what the
			// chain already behaves as — the plan exists to make every node switch at ONE height.
			return app.ModuleManager.RunMigrations(ctx, app.Configurator(), fromVM)
		},
	)

	// STORE LOADER. Needed only when an upgrade ADDS or REMOVES a module: without it the new module's
	// store does not exist at the first post-upgrade block and the node panics.
	// `ReadUpgradeInfoFromDisk` reads the plan the UpgradeKeeper wrote before halting.
	upgradeInfo, err := app.UpgradeKeeper.ReadUpgradeInfoFromDisk()
	if err != nil {
		panic(fmt.Errorf("reading the upgrade plan from disk: %w", err))
	}
	// ⛔ THE CHECK MUST NAME EVERY PLAN, NOT THE LATEST ONE. Written as `== UpgradeName` it silently
	// stops registering the loader the day a second plan exists — and a missing loader is invisible
	// until the first upgrade that does add a store, where it panics at the first post-upgrade block.
	if (upgradeInfo.Name == UpgradeName || upgradeInfo.Name == UpgradeNameAuditUnwind) &&
		!app.UpgradeKeeper.IsSkipHeight(upgradeInfo.Height) {
		// This plan adds and removes no store. The loader is registered anyway: it is then an explicit
		// no-op, and the next upgrade only has to fill in `Added`/`Deleted` instead of rediscovering
		// that the loader was missing. An empty list can be read; an absent loader cannot be seen.
		app.SetStoreLoader(upgradetypes.UpgradeStoreLoader(
			upgradeInfo.Height,
			&storetypes.StoreUpgrades{},
		))
	}
}
