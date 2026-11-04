package app

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	storetypes "cosmossdk.io/store/types"
	abci "github.com/cometbft/cometbft/abci/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/module"
)

// Consensus epoch guard.
//
// The upgrade module compares binary and state only around a stored plan. With no plan on chain it checks
// nothing, so a node started on this binary over a state of the previous epoch would apply this epoch's rules
// to it, with no migration run, and leave the network at the first block whose result depends on them (for
// epoch 15, a jury draw).
//
// The state records the consensus version of every module: the InitChainer writes this binary's at genesis,
// and a plan's handler writes them through RunMigrations. A module of this binary that the state holds below
// this binary's version, or does not hold, means the chain has not applied the last plan of upgradePlans. A
// plan that moves no version leaves nothing to compare, and TestEpochGuardSeesTheLastPlan fails for it.
//
// How the node stops. The PreBlocker returns an error, the form the upgrade module uses for its own halts, and
// FinalizeBlock fails before anything of the block reaches the store. A node at the head then stops its
// consensus and keeps running: its RPC answers and its height no longer moves. A node catching up from its
// peers exits. A restart of either exits at the handshake, on the same block. The sign is the guard's line in
// the log, not the state of the process or of its container. A panic would end the whole process when the
// block runs as an optimistic execution, whose goroutine recovers nothing.
//
// What comes before the refusal. The guard runs at FinalizeBlock: a validator on this binary has already
// proposed, signed and stored the refused block. On a network of one validator that block is final, and the
// release before the plan must replay it. It can while PrepareProposal, ProcessProposal and ExtendVote
// (vote_extensions.go) are the same in both releases; an epoch that changes one of them replays that case on
// a real node before its release.
//
// A plan skipped with --unsafe-skip-upgrades leaves the state at the previous epoch, so this binary stops at
// the plan's height; a network that skips the plan stays on the release before it.
//
// What the guard does not see: a later epoch whose plan moves no version, and a genesis of the previous epoch
// replayed by this binary, whose InitChainer stores this binary's versions. The genesis dendra-testnet started
// from is refused at InitChain (refuseEpoch14Genesis), and so is a zero-height export of that chain, which keeps
// its chain id and genesis time, whatever epoch the exported state is at. A node of this binary joins that
// network by state sync from a snapshot taken after the plan's height.

// epochGuardReleaseBefore names, for each plan of upgradePlans, the release line whose binary runs the chain up
// to the plan's height (a consensus epoch moves the minor number of the release). TestEpochGuardNamesTheLastPlan
// requires an entry for every plan.
var epochGuardReleaseBefore = map[string]string{UpgradeEpoch15: "v0.2"}

// lastPlanOf is the last plan of a list, the one whose versions the binary that registers the list runs.
func lastPlanOf(plans []string) string {
	if len(plans) == 0 {
		return ""
	}
	return plans[len(plans)-1]
}

// refuseStateFromEarlierEpoch runs in the PreBlocker after the modules' PreBlock, so that at a plan's height
// it reads the versions the handler has just written.
func (app *App) refuseStateFromEarlierEpoch(ctx sdk.Context) error {
	return refuseStoredVersions(ctx, app.UpgradeKeeper.GetModuleVersionMap, app.ModuleManager.GetVersionMap())
}

// refuseStoredVersions reads the stored versions with read, the upgrade keeper's reader, and refuses the block
// when the read fails: a state whose versions are not read is not shown to be at this binary's epoch. It meters
// the read on a gas meter of its own: the block's context then meters exactly what it metered before the guard
// existed.
func refuseStoredVersions(ctx sdk.Context, read func(context.Context) (module.VersionMap, error), binary module.VersionMap) error {
	stored, err := read(ctx.WithGasMeter(storetypes.NewInfiniteGasMeter()))
	if err != nil {
		return fmt.Errorf("consensus epoch guard: reading the stored module versions: %w", err)
	}
	plan := lastPlanOf(upgradePlans)
	return epochGuard(stored, binary, plan, epochGuardReleaseBefore[plan])
}

// epochGuard refuses a state that holds a module of this binary below this binary's consensus version, or
// holds no version for it. A module the state holds and this binary does not run is not read. A binary with
// no module version is refused, since it would compare nothing. plan and releaseBefore only word the message;
// the modules are listed in the order of their names, so every node writes the same message.
func epochGuard(stored, binary module.VersionMap, plan, releaseBefore string) error {
	if len(binary) == 0 {
		return errors.New("consensus epoch guard: this binary registers no module version")
	}
	names := make([]string, 0, len(binary))
	for name := range binary {
		names = append(names, name)
	}
	sort.Strings(names)
	var behind []string
	first := ""
	for _, name := range names {
		have, ok := stored[name]
		if ok && have >= binary[name] {
			continue
		}
		got := "no version"
		if ok {
			got = fmt.Sprintf("version %d", have)
		}
		if first == "" {
			first = name
		}
		behind = append(behind, fmt.Sprintf("module %s at %s and this binary runs version %d", name, got, binary[name]))
	}
	if first == "" {
		return nil
	}
	msg := "consensus epoch guard: the chain state holds " + strings.Join(behind, ", ") + "; "
	if plan == "" {
		msg += "this binary registers no upgrade plan that writes its versions, and it does not apply blocks of that state. "
	} else {
		msg += fmt.Sprintf("the upgrade plan %q writes this binary's versions at its height, and this binary does not apply "+
			"blocks of a chain that has not applied that plan. ", plan)
	}
	if releaseBefore != "" {
		msg += fmt.Sprintf("Run the newest %s release until the chain halts at the plan's height, then start this binary; "+
			"a network that skipped the plan with --unsafe-skip-upgrades stays on %s. ", releaseBefore, releaseBefore)
	}
	return errors.New(msg + "Read the stored version with: dendrad query upgrade module-versions " + first +
		". A node that has exited answers nothing: add --node and the RPC address of a node that runs, such as the network's")
}

// The genesis dendra-testnet started from, at epoch 14, before UpgradeEpoch15: its chain id and genesis time,
// read with curl -s https://testnet-api.dendranetwork.com/genesis.json (the file docker/GENESIS_SHA256 names
// by its SHA-256), fields chain_id and genesis_time.
const epoch14GenesisChainID = "dendra-testnet"

var epoch14GenesisTime = time.Date(2026, time.October, 6, 23, 25, 57, 565805212, time.UTC)

// refuseEpoch14Genesis refuses to start this binary from the genesis dendra-testnet started from. The
// InitChainer would store this binary's versions over that epoch 14 state and the epoch guard would see
// nothing: the node would replay the epoch 14 history under the epoch 15 rules and stop much later, on an app
// hash the network did not compute or on the upgrade module's "binary updated before trigger", and neither
// message names the cause.
//
// It reads the chain id, the genesis time and the initial height, never the state. An export of that chain at a
// height (dendrad export) starts at that height plus one and is not refused. A zero-height export (dendrad export
// --for-zero-height) keeps the chain id and the genesis time of the node's genesis file and writes initial
// height 0, which genesis loading reads as 1: it is refused, even when the exported state is at epoch 15, and a
// chain restarted from it takes a new genesis time. The data directory must be empty too: CometBFT keeps the
// first genesis a data directory loaded and reads that one at every start.
func refuseEpoch14Genesis(req *abci.RequestInitChain) error {
	if req.ChainId != epoch14GenesisChainID || !req.Time.Equal(epoch14GenesisTime) || req.InitialHeight > 1 {
		return nil
	}
	return fmt.Errorf("consensus epoch guard: this genesis carries the chain id %s and the genesis time that network started "+
		"from at epoch 14, before the upgrade plan %q, and starts at height 1. It is that genesis, or an export of that chain "+
		"at zero height, which keeps both fields (dendrad export --for-zero-height). This binary does not replay the epoch 14 "+
		"history. Join with state sync from a snapshot taken after the plan's height, or replay the history with the newest "+
		"%s release, which halts at the plan's height, then start this binary. A chain restarted from a zero-height export "+
		"takes a new genesis_time and an empty data directory",
		epoch14GenesisChainID, UpgradeEpoch15, epochGuardReleaseBefore[UpgradeEpoch15])
}
