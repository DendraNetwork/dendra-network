package keeper

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
)

// InitGenesis initializes the module's state from a provided genesis state.
func (k Keeper) InitGenesis(ctx context.Context, genState types.GenesisState) error {
	initHMiners := sdk.UnwrapSDKContext(ctx).BlockHeight()
	// One anchor, computed once and used by both loops. Deriving it twice let the two sides agree by
	// coincidence instead of by construction; `frozenPoolAnchor` is now the only place that decides.
	anchor := frozenPoolAnchor(initHMiners)
	for _, elem := range genState.MinerMap {
		// Checked here because this is the init path that cannot be skipped. `Validate()` runs
		// from `ValidateGenesis`, which the CLI and the launch kit call; `InitChain` calls `InitGenesis`
		// directly. A guard that lived only in the first could be disarmed by not running a command.
		// Failing here refuses the block instead of recording a chosen identifier for good.
		if err := types.ValidateMinerIdentity(elem.MinerId, elem.Creator); err != nil {
			return err
		}
		if err := k.Miner.Set(ctx, elem.MinerId, elem); err != nil {
			return err
		}
		// Arm the vitality guard again after an import. `MinerRegisteredHeight` is not
		// exported, so an imported miner arrives with no registration date, and `pruneInactiveMiners`
		// never touches an undated miner: the whole imported population could never be pruned, the
		// eviction guard silently disabled by the migration. The registration is dated at the import
		// height, so the grace window starts fresh (never a retroactive punishment) and the guard is armed
		// again. On a new chain MinerMap is empty, so this is a no-op (miners are dated at create-miner).
		// There is no `initH > 0` guard, because that guard was the defect.
		// It looked harmless ("on a new chain this is a no-op"), but the loop it wrapped is already a
		// no-op on a new chain, since `MinerMap` is empty there. What the guard did was disarm the
		// re-anchoring in the case that needs it most: the SDK leaves the header height at 0 during
		// InitChain whenever `InitialHeight` is not > 1, which is what `dendrad export
		// --for-zero-height` produces. An import at height 0 therefore re-anchored nobody, and
		// `jobPoolFreezeHeight` then returned an error for every imported job: every draw deferred
		// forever, with no error path anyone watches.
		// The value comes from `frozenPoolAnchor` (pool_freeze.go), one block below the import height:
		// `initH` itself would tie with a create-miner landing in the first finalised block and let a
		// newcomer into every imported pool. No reader confuses the anchor with an absence: both
		// `minerInFrozenPool` and `pruneInactiveMiners` branch on the Get error, never on the value.
		if _, e := k.MinerRegisteredHeight.Get(ctx, elem.MinerId); e != nil {
			if err := k.MinerRegisteredHeight.Set(ctx, elem.MinerId, anchor); err != nil {
				return err
			}
		}
	}
	// Dispute re-anchoring (counterpart of BlocksRemaining below).
	// In genesis, `DisputeHeight` carries the number of blocks elapsed since the dispute was opened,
	// not an absolute height (see ExportGenesis). It is re-anchored on the init height, which keeps
	// the remaining time of the window exactly.
	// The result may be negative (the window had already expired before the export), which is
	// intended and safe: `DisputeHeight` is a signed int64, no business logic tests its value (a
	// dispute is recognised by the `+disputed` state), and a negative value makes adjudication
	// immediately possible, the behaviour owed to an expired window.
	initHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	// The two anchors cannot drift, by construction.
	//
	// `MinerRegisteredHeight` and `JobPoolFreezeHeight` are compared at every committee draw, so they
	// must be written with the same value. A runtime comparison of two heights read from the same ctx
	// cannot enforce that: the condition is always false, so it looks like a live guard while being
	// unable to fire, which makes a later reader believe the invariant is watched.
	//
	// Both loops write `anchor` instead, computed once above by `frozenPoolAnchor`. There is one value,
	// so there is nothing to keep in agreement.
	_ = initHJobs // read by the dispute re-anchoring below; the anchors no longer derive from it
	for _, elem := range genState.JobMap {
		// The discriminant is the state marker; using the value was the defect.
		// `DisputeHeight` travels as an age, so a dispute opened at the export block encodes as
		// `elapsed = 0`, which `!= 0` cannot tell apart from a job that was never disputed. The
		// freshest dispute, the only one whose re-commit window still had its full length, was left at
		// 0 on import, and `msg_server_adjudicate.go` (`BlockHeight() < DisputeHeight +
		// DisputeWindow`) stopped blocking: the window was cancelled entirely, and the fresh committee
		// could be adjudicated before it had a chance to re-submit.
		// The export side already treats the state as authoritative ("a dispute is recognised by the
		// `+disputed` state"); this reads it instead of deriving it from a number that has no room for
		// a sentinel.
		if jobIsDisputed(elem.State) {
			elem.DisputeHeight = initHJobs - elem.DisputeHeight
		}
		if err := k.Job.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
		// Arm the miner-pool freeze again, with the same pattern as
		// `MinerRegisteredHeight` above. It is not exported, so an imported job arrives without an
		// anchor, and under the "absence is an error" rule its three draws would be deferred forever.
		// It is dated at the import height, so `registered == freeze` for the whole imported
		// population and `<=` holds for all of them, which is correct: they were all there before.
		//
		// It deliberately takes the same value as the miner side, from the same function. The two are
		// compared (`reg <= freeze`), so arming one without the other, or arming them from two
		// expressions that merely agree today, is how they drift apart silently. The tie that `initH`
		// produced against a first-block `create-miner` was that kind of silent equality; see
		// `frozenPoolAnchor` for the window it opened and why one block below closes it. No
		// `initH > 0` guard either: the loop is already a no-op on a chain with no jobs.
		if _, e := k.JobPoolFreezeHeight.Get(ctx, elem.JobId); e != nil {
			if err := k.JobPoolFreezeHeight.Set(ctx, elem.JobId, anchor); err != nil {
				return err
			}
		}
	}
	if genState.Pools != nil {
		if err := k.Pools.Set(ctx, *genState.Pools); err != nil {
			return err
		}
	}
	for _, elem := range genState.CommitMap {
		if err := k.Commit.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
	}
	for _, elem := range genState.BeaconMap {
		if err := k.Beacon.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
	}

	// Deadlines arrive as remaining time and are re-anchored on the restart height; an appointment
	// exported in absolute terms would fall behind the restarted chain and never fire.
	initH := sdk.UnwrapSDKContext(ctx).BlockHeight()

	// Added state (GenesisState fields 7+).
	// This whole block tolerates emptiness: a genesis produced by an earlier binary has none of these
	// fields, and the import must then behave as it did before.
	for _, e := range genState.HeldFee {
		if err := k.HeldFee.Set(ctx, e.JobId, e.Amount); err != nil {
			return err
		}
	}
	for _, e := range genState.HeldBurn {
		if err := k.HeldBurn.Set(ctx, e.JobId, e.Amount); err != nil {
			return err
		}
	}
	// The retained protocol cut and the pending subsidy right, per job. Coins the
	// module holds and a right a miner earned both travel, or an export strands them without an owner.
	for _, set := range []struct {
		m       collections.Map[string, uint64]
		entries []types.HeldAmount
	}{
		{k.HeldValidatorShare, genState.HeldValidatorShare},
		{k.HeldTeamShare, genState.HeldTeamShare},
		{k.HeldTreasuryShare, genState.HeldTreasuryShare},
		{k.HeldDemand, genState.HeldDemand},
	} {
		for _, e := range set.entries {
			if err := set.m.Set(ctx, e.JobId, e.Amount); err != nil {
				return err
			}
		}
	}
	for _, e := range genState.PendingReveal {
		if err := k.PendingReveal.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	// The expiry appointment travels like its neighbours: as an age, re-anchored on the import height.
	for _, e := range genState.PendingJobExpiry {
		if err := k.PendingJobExpiry.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAudit {
		if err := k.PendingAudit.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAuditResolve {
		if err := k.PendingAuditResolve.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.PendingAppealResolve {
		if err := k.PendingAppealResolve.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
			return err
		}
	}
	for _, e := range genState.AuditCommittee {
		if err := k.AuditCommittee.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	// The anchored work committee travels too. Without it the import would derive it again
	// from the stake of that moment, and the anchoring would not survive the first restart.
	for _, e := range genState.WorkCommittee {
		if err := k.WorkCommittee.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.ValidatorVrfPubkey {
		if err := k.ValidatorVrfPubkey.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.MinerOptimisticCount {
		if err := k.MinerOptimisticCount.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.AvailFailCount {
		if err := k.AvailFailCount.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	for _, e := range genState.AvailFailWindowStart {
		if err := k.AvailFailWindowStart.Set(ctx, e.Key, e.Value); err != nil {
			return err
		}
	}
	if genState.AvailChallenge != "" {
		if err := k.AvailChallenge.Set(ctx, genState.AvailChallenge); err != nil {
			return err
		}
	}
	// A selected audit stays selected across a migration. Losing this marker would replay
	// the lottery on retry, reopening the one-in-two escape through the import path. Same class as M7.
	for _, id := range genState.AuditSelected {
		if err := k.AuditSelected.Set(ctx, id); err != nil {
			return err
		}
	}
	// The retry queue of due releases survives: HeldFee travels (field 7), but without this queue
	// nothing would attempt the transfer again, a slow confiscation of what is owed.
	for _, id := range genState.PendingHeldRelease {
		if err := k.PendingHeldRelease.Set(ctx, id); err != nil {
			return err
		}
	}
	// The opened-audits counter is monotonic and travels, otherwise the partition sum
	// breaks forever after a migration (classes recounted while `opened` restarts from zero).
	if genState.AuditsOpened > 0 {
		if err := k.AuditsOpened.Set(ctx, genState.AuditsOpened); err != nil {
			return err
		}
	}
	// Held-fund timestamps arrive as an age (elapsed blocks) and are re-anchored on the restart height.
	//
	// The age is lost whenever the restart height is below it, and this clamp is where. Measured on one
	// held fee exported with an age of 60 blocks, imported again at three heights and read 29 blocks
	// later: at h=1 the age reads 30, at h=30 it reads 59, at h=200 it reads 89, the correct answer being
	// 89 every time. Below the accumulated age the clamp fires, `since` becomes 0, and the debt looks
	// exactly as old as the new chain. On a chain restarted from a fresh genesis, every retention age is
	// therefore understated for its first blocks.
	//
	// The clamp itself is correct: a uint64 anchor cannot express "60 blocks old" at height 1, because the
	// anchor would have to be negative. Carrying the age across a height reset means storing the age,
	// which changes the state shape (collection, genesis field, migration) and is an owner's decision.
	// This note keeps readers from taking the clamp for a guarantee: `held_oldest_age` is the figure
	// `audit_sampling.go` names as what makes "held is not lost" falsifiable, and an understated age is a
	// false green on exactly that axis.
	for _, e := range genState.HeldSinceElapsed {
		since := int64(0)
		if h := sdk.UnwrapSDKContext(ctx).BlockHeight(); h > int64(e.Value) {
			since = h - int64(e.Value)
		}
		if err := k.HeldSince.Set(ctx, e.Key, uint64(since)); err != nil {
			return err
		}
	}
	// Committee anchor timestamps follow the same relative rule as HeldSince, including the loss
	// documented above: below the accumulated age the lock looks young instead of old.
	for _, e := range genState.CommitteeSinceElapsed {
		since := int64(0)
		if h := sdk.UnwrapSDKContext(ctx).BlockHeight(); h > int64(e.Value) {
			since = h - int64(e.Value)
		}
		if err := k.CommitteeSince.Set(ctx, e.Key, uint64(since)); err != nil {
			return err
		}
	}

	// Invariant #8, enforced at runtime: refuse at load time a genesis whose params would make Sybil
	// self-dealing +EV (an emission drain). It doubles the guard in GenesisState.Validate() in case an
	// init path bypasses validate-genesis.
	if !genState.Params.WashSubsidyNegativeEV(genState.Params.WorkGateBps) {
		return errors.New("invariant #8 violated: these genesis params would make Sybil self-dealing +EV (emission drain)")
	}

	// No init path may install unvalidated parameters.
	//
	// `validate-genesis` calls `GenesisState.Validate()`, but nothing forces an init path to go through
	// it: a hand-made, imported or third-party genesis reaches InitGenesis directly. Every cross-bound in
	// `Params.Validate()`, including those refusing an armed optimistic mode without deterrence, without
	// a dispute bond, or without an unpredictable committee seed, would then hold only for those who
	// chose to run it. A guard that depends on the tool used to start the chain is only a convention.
	//
	// The refusal is loud and happens at init, the only moment where fixing it still costs a
	// configuration file instead of a chain reset.
	if err := genState.Params.Validate(); err != nil {
		return fmt.Errorf("invalid genesis parameters (refused at load time): %w", err)
	}
	if err := k.checkTeamAddressPrefix(genState.Params); err != nil {
		return fmt.Errorf("invalid genesis parameters (refused at load time): %w", err)
	}

	// Enforcing the model registry assumes the registry anchors something.
	//
	// `enforce_model_registry` requires a commit to cite a registered, active model. The binding to the
	// artifact (the fact that the miner really served that model) rests entirely on the `weights_sha256`
	// anchored in the registry: without an anchor, `ExpectedWeights` answers "nothing to check" and the
	// miner can declare one model while serving anything. A genesis that arms enforcement while leaving
	// active models without an anchor therefore advertises a constraint that does not exist for those
	// models, and nothing shows it, since the commit is accepted.
	//
	// The init order places `modelregistry` before `jobs`, so the registry is already loaded here. A
	// devnet, which does not enforce the registry, keeps its unanchored models: that state is coherent,
	// and nothing changes for it.
	if genState.Params.EnforceModelRegistry {
		unanchored, err := k.modelRegistryKeeper.ActiveWithoutAnchor(ctx)
		if err != nil {
			return fmt.Errorf("model registry unreadable at load time (fail-closed refusal): %w", err)
		}
		if len(unanchored) > 0 {
			return fmt.Errorf("enforce_model_registry=true but %d active model(s) have no weights_sha256 (%s): "+
				"no model_id <-> artefact binding for those models, a miner can declare them and serve something else. "+
				"Anchor their weights or deactivate them", len(unanchored), strings.Join(unanchored, ","))
		}
	}
	return k.Params.Set(ctx, genState.Params)
}

// ExportGenesis returns the module's exported genesis.
func (k Keeper) ExportGenesis(ctx context.Context) (*types.GenesisState, error) {
	var err error

	genesis := types.DefaultGenesis()
	genesis.Params, err = k.Params.Get(ctx)
	if err != nil {
		return nil, err
	}
	if err := k.Miner.Walk(ctx, nil, func(_ string, val types.Miner) (stop bool, err error) {
		genesis.MinerMap = append(genesis.MinerMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	// Dispute deadline: exported relative, never absolute.
	// `DisputeHeight` is a height of the old chain. Exported as is and imported into a chain starting
	// from zero, the guard `BlockHeight() < DisputeHeight + DisputeWindow` (msg_server_adjudicate.go)
	// stays true for the entire height of the old chain: adjudication becomes unreachable, the
	// disputer's bond stays locked, and the job is frozen for good. So the blocks elapsed since the
	// dispute opened are exported, and InitGenesis re-anchors them. Same reasoning as
	// `BlocksRemaining` for the pending queues.
	expHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	if err := k.Job.Walk(ctx, nil, func(_ string, val types.Job) (stop bool, err error) {
		// The same discriminant as the import, which it must be for the round trip to be one. A
		// dispute opened at this very block encodes as `elapsed = 0`, indistinguishable from a job
		// that was never disputed. Reading the `+disputed` marker instead of the number restores the
		// one bit the age encoding has no room for.
		if jobIsDisputed(val.State) {
			val.DisputeHeight = expHJobs - val.DisputeHeight
		}
		genesis.JobMap = append(genesis.JobMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	// An absent item must not be exported as a zero item, otherwise export/import is not an identity.
	// On a chain where nothing ever touched the pools, the key `pools/value/` is absent; exporting a
	// zero-valued `Pools{}` anyway makes `InitGenesis` write it, so the imported chain carries a key the
	// original did not have. Semantically that is a no-op (every reader treats absence as zero, see
	// `msg_server_adjudicate.go`), but the app hash differs: a node built from the export and one from the
	// original no longer hold the same state, and a round trip that is not an identity cannot prove a
	// migration.
	//
	// Declaring `pools/value/` as a known divergence in the test would have silenced a field that carries
	// money (Treasury, MinerLocked) and hidden a real pool divergence forever. The cause is fixed instead:
	// absent at export, absent in genesis (`genesis.Pools` stays nil), and the import writes nothing
	// (guarded by `genState.Pools != nil`). Exact in both directions, with no coverage lost.
	if pools, perr := k.Pools.Get(ctx); perr == nil {
		genesis.Pools = &pools
	} else if !errors.Is(perr, collections.ErrNotFound) {
		return nil, perr
	}
	if err := k.Commit.Walk(ctx, nil, func(_ string, val types.Commit) (stop bool, err error) {
		genesis.CommitMap = append(genesis.CommitMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.Beacon.Walk(ctx, nil, func(_ string, val types.Beacon) (stop bool, err error) {
		genesis.BeaconMap = append(genesis.BeaconMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}

	// Added state (see genesis.proto fields 7+).
	// Deliberately not exported: DecentralizedSeed, DecentralizedSeedContributors,
	// DecentralizedSeedContributorPower, BlockHash, and `Available`.
	//
	// The first four are caches indexed by height, produced by the vote extensions. `Available` is
	// indexed by epoch (msg_server_prove_availability.go) and purged at every epoch boundary
	// (availability.go). After a genesis, neither a height nor an epoch index designates anything any
	// more: carrying them over would produce keys pointing nowhere, and a height-based computation on
	// them would only produce a meaningless number.
	//
	// Their absence is a documented choice, which matters because "collection not exported" does not mean
	// "data lost".
	if err := k.HeldFee.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.HeldFee = append(genesis.HeldFee, types.HeldAmount{JobId: k2, Amount: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.HeldBurn.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.HeldBurn = append(genesis.HeldBurn, types.HeldAmount{JobId: k2, Amount: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	for _, set := range []struct {
		m   collections.Map[string, uint64]
		out *[]types.HeldAmount
	}{
		{k.HeldValidatorShare, &genesis.HeldValidatorShare},
		{k.HeldTeamShare, &genesis.HeldTeamShare},
		{k.HeldTreasuryShare, &genesis.HeldTreasuryShare},
		{k.HeldDemand, &genesis.HeldDemand},
	} {
		out := set.out
		if err := set.m.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
			*out = append(*out, types.HeldAmount{JobId: k2, Amount: v})
			return false, nil
		}); err != nil {
			return nil, err
		}
	}
	// Remaining time, not an absolute height (see genesis.proto/PendingEntry). Floor at 1: a deadline
	// already past at export time must fire at the block after the restart, instead of sitting behind
	// the current height, where the walk, which matches the exact height, would never see it.
	now := sdk.UnwrapSDKContext(ctx).BlockHeight()
	remaining := func(deadline int64) int64 {
		if r := deadline - now; r > 0 {
			return r
		}
		return 1
	}

	// The composite-key KeySets get one explicit Walk each. A generic helper would require an
	// interface whose signature matches collections.KeySet exactly; writing them out is longer but
	// cannot get the contract wrong.
	if err := k.PendingReveal.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingReveal = append(genesis.PendingReveal, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingJobExpiry.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingJobExpiry = append(genesis.PendingJobExpiry, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAudit.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAudit = append(genesis.PendingAudit, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAuditResolve.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAuditResolve = append(genesis.PendingAuditResolve, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingAppealResolve.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingAppealResolve = append(genesis.PendingAppealResolve, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AuditCommittee.Walk(ctx, nil, func(k2, v string) (bool, error) {
		genesis.AuditCommittee = append(genesis.AuditCommittee, types.StringEntry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.WorkCommittee.Walk(ctx, nil, func(k2, v string) (bool, error) {
		genesis.WorkCommittee = append(genesis.WorkCommittee, types.StringEntry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.ValidatorVrfPubkey.Walk(ctx, nil, func(k2, v string) (bool, error) {
		genesis.ValidatorVrfPubkey = append(genesis.ValidatorVrfPubkey, types.StringEntry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.MinerOptimisticCount.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.MinerOptimisticCount = append(genesis.MinerOptimisticCount, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AvailFailCount.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.AvailFailCount = append(genesis.AvailFailCount, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.AvailFailWindowStart.Walk(ctx, nil, func(k2 string, v uint64) (bool, error) {
		genesis.AvailFailWindowStart = append(genesis.AvailFailWindowStart, types.Uint64Entry{Key: k2, Value: v})
		return false, nil
	}); err != nil {
		return nil, err
	}
	// Two simple-key KeySets that carry a selection (AuditSelected) and a debt
	// (PendingHeldRelease), not a cache, so they travel across a migration.
	if err := k.AuditSelected.Walk(ctx, nil, func(id string) (bool, error) {
		genesis.AuditSelected = append(genesis.AuditSelected, id)
		return false, nil
	}); err != nil {
		return nil, err
	}
	if err := k.PendingHeldRelease.Walk(ctx, nil, func(id string) (bool, error) {
		genesis.PendingHeldRelease = append(genesis.PendingHeldRelease, id)
		return false, nil
	}); err != nil {
		return nil, err
	}
	if opened, oErr := k.AuditsOpened.Get(ctx); oErr == nil {
		genesis.AuditsOpened = opened
	}
	// Held-fund timestamps are exported as an age (elapsed time), with the same rule as PendingEntry:
	// an absolute height from the old chain dates nothing on the new one.
	expHHeld := sdk.UnwrapSDKContext(ctx).BlockHeight()
	if err := k.HeldSince.Walk(ctx, nil, func(jobId string, since uint64) (bool, error) {
		elapsed := uint64(0)
		if expHHeld > int64(since) {
			elapsed = uint64(expHHeld) - since
		}
		genesis.HeldSinceElapsed = append(genesis.HeldSinceElapsed, types.Uint64Entry{Key: jobId, Value: elapsed})
		return false, nil
	}); err != nil {
		return nil, err
	}
	// Committee anchor timestamps, exported as an age (the committees themselves travel in
	// audit_committee above). A timestamp that did not travel would silence `oldest_lock_age` at the
	// migration, when an old lock most deserves to be seen.
	if err := k.CommitteeSince.Walk(ctx, nil, func(key string, since uint64) (bool, error) {
		elapsed := uint64(0)
		if expHHeld > int64(since) {
			elapsed = uint64(expHHeld) - since
		}
		genesis.CommitteeSinceElapsed = append(genesis.CommitteeSinceElapsed, types.Uint64Entry{Key: key, Value: elapsed})
		return false, nil
	}); err != nil {
		return nil, err
	}
	if ac, e := k.AvailChallenge.Get(ctx); e == nil {
		genesis.AvailChallenge = ac
	} else if !errors.Is(e, collections.ErrNotFound) {
		return nil, e
	}

	return genesis, nil
}
