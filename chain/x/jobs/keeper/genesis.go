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
	for _, elem := range genState.MinerMap {
		// ADR-039 — CHECKED HERE BECAUSE THIS IS THE DOOR THAT CANNOT BE SKIPPED. `Validate()` runs from
		// `ValidateGenesis`, which the CLI and the launch kit call; `InitChain` calls `InitGenesis`
		// directly. A guard living only in the first is a guard an operator disarms by not running a
		// command. Failing here refuses the block instead of engraving a chosen identifier for good.
		if err := types.ValidateMinerIdentity(elem.MinerId, elem.Creator); err != nil {
			return err
		}
		if err := k.Miner.Set(ctx, elem.MinerId, elem); err != nil {
			return err
		}
		// ADR-034 M7 — RE-ARM THE VITALITY GUARD AFTER AN IMPORT. `MinerRegisteredHeight` is NOT exported,
		// so an imported miner arrives with no registration date at all, and `pruneInactiveMiners` never
		// touches an undated miner: the whole imported population would become UNPRUNABLE FOR LIFE — the
		// eviction guard silently disabled by the migration. Date the registration at the import height:
		// the grace window is FRESH (never a retroactive punishment) and the guard is armed again. On a NEW
		// chain MinerMap is empty, so this is a no-op (miners are dated at create-miner).
		if initHMiners > 0 {
			if _, e := k.MinerRegisteredHeight.Get(ctx, elem.MinerId); e != nil {
				if err := k.MinerRegisteredHeight.Set(ctx, elem.MinerId, uint64(initHMiners)); err != nil {
					return err
				}
			}
		}
	}
	// DISPUTE RE-ANCHORING (counterpart of BlocksRemaining below).
	// In genesis, `DisputeHeight` does NOT carry an absolute height but the number of blocks ELAPSED
	// since the dispute was opened (see ExportGenesis). It is re-anchored on the init height, which
	// preserves the REMAINING TIME of the window exactly.
	// The result may be NEGATIVE (the window had already expired before the export) — that is intended
	// and safe: `DisputeHeight` is a signed int64, no business logic tests its value (a dispute is
	// recognised by the `+disputed` state), and a negative value makes adjudication immediately
	// possible, which is precisely the behaviour owed to an expired window.
	initHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	// ADR-037 §3.b — EQUALITY GUARD ON THE TWO INIT HEIGHTS.
	// `MinerRegisteredHeight` (set above) and `JobPoolFreezeHeight` (set below) are COMPARED at every
	// committee draw. If one of the two loops ever moved to a different block, their relative order
	// would stop being a fact of history and become an artefact of the migration code: the whole
	// imported population would become ineligible on every imported job, WITHOUT A SINGLE ERROR — the
	// same silent failure that disqualified `DisputeHeight` as an anchor. Here the import FAILS loudly
	// rather than producing empty committees.
	if initHJobs != initHMiners {
		return fmt.Errorf("ADR-037: divergent init heights (miners=%d, jobs=%d) — the two compared anchors must be set at the SAME block, otherwise the whole imported population silently becomes ineligible", initHMiners, initHJobs)
	}
	for _, elem := range genState.JobMap {
		if elem.DisputeHeight != 0 {
			elem.DisputeHeight = initHJobs - elem.DisputeHeight
		}
		if err := k.Job.Set(ctx, elem.JobId, elem); err != nil {
			return err
		}
		// ADR-037 §2.5 — RE-ARM THE MINER-POOL FREEZE, SAME PATTERN AS `MinerRegisteredHeight` ABOVE.
		// It is not exported, so an imported job arrives WITHOUT an anchor, and under the "absence is an
		// ERROR" rule its three draws would be DEFERRED forever. Date it at the import height:
		// `registered == freeze == initH` for the whole imported population, so `<=` holds for all of them
		// — correct, they were all there before — and is STRICTLY FALSE for any identity created later.
		// On a NEW chain JobMap is empty, so this is a no-op.
		if initHJobs > 0 {
			if _, e := k.JobPoolFreezeHeight.Get(ctx, elem.JobId); e != nil {
				if err := k.JobPoolFreezeHeight.Set(ctx, elem.JobId, uint64(initHJobs)); err != nil {
					return err
				}
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

	// Deadlines arrive as REMAINING TIME: they are re-anchored on the restart height, otherwise an
	// appointment exported in absolute terms would fall behind the restarted chain and never fire.
	initH := sdk.UnwrapSDKContext(ctx).BlockHeight()

	// --- ADDED STATE (GenesisState fields 7+) ------------------------------------------------------
	// This whole block tolerates emptiness: a genesis produced by an earlier binary simply has none of
	// these fields, and the import must then behave exactly as it did before.
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
	for _, e := range genState.PendingReveal {
		if err := k.PendingReveal.Set(ctx, collections.Join(initH+e.BlocksRemaining, e.Id)); err != nil {
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
	// ADR-034 C3 — a SELECTED audit stays selected across a migration: losing this marker would replay
	// the lottery on retry, reopening the one-in-two escape through the import path. Same class as M7.
	for _, id := range genState.AuditSelected {
		if err := k.AuditSelected.Set(ctx, id); err != nil {
			return err
		}
	}
	// The retry queue of DUE releases survives: HeldFee travels (field 7), but without this queue
	// nothing would re-attempt the transfer, which amounts to a slow confiscation of what is owed.
	for _, id := range genState.PendingHeldRelease {
		if err := k.PendingHeldRelease.Set(ctx, id); err != nil {
			return err
		}
	}
	// ADR-034 partition — the opened-audits counter is MONOTONE: it travels, otherwise the partition sum
	// breaks forever after a migration (classes recounted while `opened` restarts from zero).
	if genState.AuditsOpened > 0 {
		if err := k.AuditsOpened.Set(ctx, genState.AuditsOpened); err != nil {
			return err
		}
	}
	// Held-fund timestamps arrive as an AGE (elapsed blocks) and are re-anchored on the restart height.
	// Clamping at 0 keeps an old debt OLD (the displayed age, current height minus 0, keeps climbing):
	// the `held_oldest_age` alarm survives the migration instead of being rejuvenated by it.
	for _, e := range genState.HeldSinceElapsed {
		since := int64(0)
		if h := sdk.UnwrapSDKContext(ctx).BlockHeight(); h > int64(e.Value) {
			since = h - int64(e.Value)
		}
		if err := k.HeldSince.Set(ctx, e.Key, uint64(since)); err != nil {
			return err
		}
	}
	// Committee anchor timestamps follow the same relative rule as HeldSince: re-anchored as
	// `initH - age`, clamped at 0 on the OLD side (a juror lock survives the migration, it does not
	// rejuvenate).
	for _, e := range genState.CommitteeSinceElapsed {
		since := int64(0)
		if h := sdk.UnwrapSDKContext(ctx).BlockHeight(); h > int64(e.Value) {
			since = h - int64(e.Value)
		}
		if err := k.CommitteeSince.Set(ctx, e.Key, uint64(since)); err != nil {
			return err
		}
	}

	// INVARIANT #8 — runtime defence: refuse at load time a genesis whose params would make Sybil
	// self-dealing +EV (an emission drain). Doubles the guard in GenesisState.Validate() in case an init
	// path bypasses validate-genesis.
	if !genState.Params.WashSubsidyNegativeEV(genState.Params.WorkGateBps) {
		return errors.New("invariant #8 violated: these genesis params would make Sybil self-dealing +EV (emission drain)")
	}

	// NO INIT PATH MAY INSTALL UNVALIDATED PARAMETERS.
	//
	// `validate-genesis` calls `GenesisState.Validate()`, but nothing forces an init path to go through
	// it: a hand-made, imported, or third-party-generated genesis reaches InitGenesis directly. Every
	// cross-bound in `Params.Validate()` — including those refusing an ARMED optimistic mode without
	// deterrence, without a dispute bond, or without an unpredictable committee seed — would then hold
	// only for those who chose to invoke them. A guard that depends on the tool used to start the chain
	// is not a guard; it is a convention.
	//
	// The refusal is LOUD and happens at init: the only moment where fixing it still costs a
	// configuration file rather than a chain reset.
	if err := genState.Params.Validate(); err != nil {
		return fmt.Errorf("invalid genesis parameters (refused at load time): %w", err)
	}

	// ENFORCING THE MODEL REGISTRY ASSUMES THE REGISTRY ANCHORS SOMETHING.
	//
	// `enforce_model_registry` requires a commit to cite a registered, active model. The binding to the
	// ARTEFACT — the fact that the miner really served THAT model — rests entirely on the
	// `weights_sha256` anchored in the registry: without an anchor, `ExpectedWeights` answers "nothing
	// to check" and the miner can declare one model while serving anything. A genesis that arms
	// enforcement while leaving ACTIVE models without an anchor therefore advertises a constraint that
	// does not exist for those models — and it is invisible, since the commit is accepted.
	//
	// The init order places `modelregistry` before `jobs`, so the registry is already loaded here. A
	// devnet, which does NOT enforce the registry, keeps its unanchored models: that is a coherent
	// state, not a hole, and nothing changes for it.
	if genState.Params.EnforceModelRegistry {
		unanchored, err := k.modelRegistryKeeper.ActiveWithoutAnchor(ctx)
		if err != nil {
			return fmt.Errorf("model registry unreadable at load time (fail-closed refusal): %w", err)
		}
		if len(unanchored) > 0 {
			return fmt.Errorf("enforce_model_registry=true but %d ACTIVE model(s) have no weights_sha256 (%s): "+
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
	// DISPUTE DEADLINE: exported RELATIVE, never absolute.
	// `DisputeHeight` is a height of the OLD chain. Exported as-is and reimported into a chain starting
	// from zero, the guard `BlockHeight() < DisputeHeight + DisputeWindow`
	// (msg_server_adjudicate.go) stays true for the ENTIRE height of the old chain: adjudication
	// becomes unreachable and the disputer's bond stays locked — the job is frozen for good. So the
	// blocks ELAPSED since the dispute opened are exported, and InitGenesis re-anchors them. Same
	// reasoning as `BlocksRemaining` for the pending queues.
	expHJobs := sdk.UnwrapSDKContext(ctx).BlockHeight()
	if err := k.Job.Walk(ctx, nil, func(_ string, val types.Job) (stop bool, err error) {
		if val.DisputeHeight != 0 {
			val.DisputeHeight = expHJobs - val.DisputeHeight
		}
		genesis.JobMap = append(genesis.JobMap, val)
		return false, nil
	}); err != nil {
		return nil, err
	}
	// AN ABSENT ITEM MUST NOT BE EXPORTED AS A ZERO ITEM — otherwise export/import is not an identity.
	// On a chain where nothing ever touched the pools, the key `pools/value/` is ABSENT; exporting a
	// zero-valued `Pools{}` anyway makes `InitGenesis` WRITE it, so the reimported chain carries a key
	// the original did not have. Semantically that is a no-op (every reader treats absence as zero, see
	// `msg_server_adjudicate.go`), but the app-hash differs: two nodes, one from the export and one from
	// the original, no longer hold the same state. A round-trip that is not an identity cannot serve as
	// proof of migration.
	//
	// Declaring `pools/value/` as a known divergence in the test bench would have silenced a field that
	// CARRIES MONEY (Treasury, MinerLocked): the exception would have masked a real pool divergence
	// forever. The cause is fixed instead: absent at export -> absent in genesis (`genesis.Pools` stays
	// nil) -> the import writes nothing (guarded by `genState.Pools != nil`). Exact in both directions,
	// with no coverage lost.
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

	// --- ADDED STATE (see genesis.proto fields 7+) --------------------------------------------------
	// DELIBERATELY NOT EXPORTED: DecentralizedSeed, DecentralizedSeedContributors,
	// DecentralizedSeedContributorPower, BlockHash — and `Available`.
	//
	// The first four are caches indexed by HEIGHT, produced by the vote extensions. `Available` is
	// indexed by EPOCH (msg_server_prove_availability.go) and purged at every epoch boundary
	// (availability.go). After a genesis, neither a height nor an epoch index designates anything any
	// more: carrying them over would produce keys pointing nowhere, and applying a height-based
	// computation to them would only produce a meaningless number.
	//
	// Their absence is therefore a documented CHOICE, not an oversight — a distinction that matters,
	// because "collection not exported" does not mean "data lost".
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
	// REMAINING TIME, not an absolute height (see genesis.proto/PendingEntry). Floor at 1: a deadline
	// already past at export time must fire at the block following the restart, not sit behind the
	// current height — where the walk, which matches the EXACT height, would never see it.
	now := sdk.UnwrapSDKContext(ctx).BlockHeight()
	remaining := func(deadline int64) int64 {
		if r := deadline - now; r > 0 {
			return r
		}
		return 1
	}

	// The composite-key KeySets: one explicit Walk each. A generic helper would require an interface
	// whose signature must match collections.KeySet exactly; writing them out is longer but cannot get
	// the contract wrong.
	if err := k.PendingReveal.Walk(ctx, nil, func(key collections.Pair[int64, string]) (bool, error) {
		genesis.PendingReveal = append(genesis.PendingReveal, types.PendingEntry{BlocksRemaining: remaining(key.K1()), Id: key.K2()})
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
	// ADR-034 C3 — two simple-key KeySets that carry a SELECTION (AuditSelected) and a DEBT
	// (PendingHeldRelease), not a cache: they travel across a migration.
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
	// Held-fund timestamps are exported as an AGE (elapsed time), same rule as PendingEntry: an absolute
	// height from the old chain dates nothing on the new one.
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
	// Committee anchor timestamps, exported as an AGE (the committees themselves travel in
	// audit_committee above; a timestamp that did not travel would silence `oldest_lock_age` at the
	// migration, exactly when an old lock deserves to be seen).
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
