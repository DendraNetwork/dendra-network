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
// The list was deliberately empty from epoch 14 until epoch 15: the two plans that
// lived here before, `v2-anchored-committees` and `v3-audit-unwind`, synchronised switches on chains
// that no longer exist, since `dendra-testnet` started from a fresh genesis at epoch 14.
//
// A plan that adds or removes a store must fill `Added`/`Deleted` in the loader below for its own name.
var upgradePlans = []string{UpgradeEpoch15}

// UpgradeEpoch15 is consensus epoch 15: jurors are declared judges, a governed
// minimum kit version on the presence proof, and the proto debts of that epoch. It reaches
// `dendra-testnet` by a `MsgSoftwareUpgrade` proposal naming this plan, at a height the owner chooses
// after the rehearsal on a throwaway chain. The handler runs `RunMigrations`, which
// runs the one migration of the epoch, `x/jobs` version 1 to 2 (keeper/migrations.go): it writes the
// new parameters at 0 and nothing else. No store is added or removed, so the loader lists none; the
// renamed emission item keeps its key bytes (x/emission/types/keys.go, measured by a test), so the
// handler copies nothing. Reading on a node: `dendrad query upgrade applied <this name>`.
//
// The epoch 15 binary starts only after the old one stopped at the height of a plan that passed, and two
// guards hold that order. The upgrade module halts a binary that registers a plan still pending ("binary
// updated before trigger"). With no plan on chain it checks nothing, and the consensus epoch guard
// (epoch_guard.go::epochGuard) refuses the first block of a state that holds a module below this binary's
// version (for this plan, x/jobs below 2): the node stops before applying it, and the message names the
// release to run and the command that reads the stored version.
const UpgradeEpoch15 = "epoch-15-jurors-are-judges"

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
