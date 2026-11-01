package app

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"testing"
	"time"

	"cosmossdk.io/core/header"
	"cosmossdk.io/log"
	sdkmath "cosmossdk.io/math"
	"cosmossdk.io/store/prefix"
	"cosmossdk.io/store/rootmulti"
	snapshottypes "cosmossdk.io/store/snapshots/types"
	storetypes "cosmossdk.io/store/types"
	upgradetypes "cosmossdk.io/x/upgrade/types"
	abci "github.com/cometbft/cometbft/abci/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	cmttypes "github.com/cometbft/cometbft/types"
	dbm "github.com/cosmos/cosmos-db"
	"github.com/cosmos/cosmos-sdk/baseapp"
	"github.com/cosmos/cosmos-sdk/client/flags"
	"github.com/cosmos/cosmos-sdk/crypto/keys/secp256k1"
	"github.com/cosmos/cosmos-sdk/server"
	"github.com/cosmos/cosmos-sdk/testutil/mock"
	simtestutil "github.com/cosmos/cosmos-sdk/testutil/sims"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/module"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	banktypes "github.com/cosmos/cosmos-sdk/x/bank/types"
	genutiltypes "github.com/cosmos/cosmos-sdk/x/genutil/types"
	protoio "github.com/cosmos/gogoproto/io"
	"github.com/stretchr/testify/require"

	emissionkeeper "github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	emissiontypes "github.com/DendraNetwork/dendra-network/chain/x/emission/types"
	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// These tests drive the shipped PreBlocker and InitChainer through the real application, from a genesis this
// binary builds, and then edit the stored versions where a node could hold another state.

const guardChainID = "epoch-guard-test"

var guardStart = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

// versionsBeforePlan holds, for each plan of upgradePlans, the module versions the release before the plan
// stores: the state the guard must tell from the plan's. For UpgradeEpoch15 it is what dendra-testnet holds
// under epoch 14, read on a node with: dendrad query upgrade module-versions
var versionsBeforePlan = map[string]module.VersionMap{
	UpgradeEpoch15: {
		"06-solomachine": 0, "07-tendermint": 0, "auth": 5, "authz": 2, "bank": 4, "circuit": 1, "consensus": 1,
		"distribution": 3, "emission": 1, "epochs": 1, "evidence": 1, "feegrant": 2, "genutil": 1, "gov": 5,
		"group": 2, "ibc": 8, "interchainaccounts": 3, "jobs": 1, "modelregistry": 1, "nft": 1, "params": 1,
		"runtime": 0, "slashing": 4, "staking": 5, "transfer": 6, "upgrade": 2,
	},
}

type guardChain struct {
	app    *App
	valSet *cmttypes.ValidatorSet
}

func newGuardAppOn(t *testing.T, db dbm.DB, trace io.Writer, chainID string, opts simtestutil.AppOptionsMap) *App {
	t.Helper()
	all := simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()}
	for k, v := range opts {
		all[k] = v
	}
	return New(log.NewNopLogger(), db, trace, true, all, baseapp.SetChainID(chainID))
}

func newGuardApp(t *testing.T, db dbm.DB, trace io.Writer) *App {
	t.Helper()
	return newGuardAppOn(t, db, trace, guardChainID, nil)
}

// guardGenesis builds a genesis with this binary: one validator and one funded account. Chains started from
// the same bytes compute the same app hashes.
func guardGenesis(t *testing.T) ([]byte, *cmttypes.ValidatorSet) {
	t.Helper()
	app := newGuardApp(t, dbm.NewMemDB(), nil)
	pk, err := mock.NewPV().GetPubKey()
	require.NoError(t, err)
	valSet := cmttypes.NewValidatorSet([]*cmttypes.Validator{cmttypes.NewValidator(pk, 1)})
	key := secp256k1.GenPrivKey()
	acc := authtypes.NewBaseAccount(key.PubKey().Address().Bytes(), key.PubKey(), 0, 0)
	balance := banktypes.Balance{
		Address: acc.GetAddress().String(),
		Coins:   sdk.NewCoins(sdk.NewCoin(sdk.DefaultBondDenom, sdkmath.NewInt(100_000_000_000))),
	}
	// The emission module refuses a genesis whose Reserve its account does not hold.
	reserve := banktypes.Balance{
		Address: authtypes.NewModuleAddress(emissiontypes.ModuleName).String(),
		Coins:   sdk.NewCoins(sdk.NewCoin("udndr", sdkmath.NewIntFromUint64(emissionkeeper.GenesisReserveU))),
	}
	genesis, err := simtestutil.GenesisStateWithValSet(app.AppCodec(), app.DefaultGenesis(), valSet,
		[]authtypes.GenesisAccount{acc}, balance, reserve)
	require.NoError(t, err)
	state, err := json.Marshal(genesis)
	require.NoError(t, err)
	return state, valSet
}

func initGuardChain(app *App, chainID string, start time.Time, initialHeight int64, state []byte) error {
	_, err := app.InitChain(&abci.RequestInitChain{
		ChainId:         chainID,
		Time:            start,
		InitialHeight:   initialHeight,
		ConsensusParams: simtestutil.DefaultConsensusParams,
		AppStateBytes:   state,
	})
	return err
}

// startGuardChain runs InitChain on app from state and commits its first block.
func startGuardChain(t *testing.T, app *App, chainID string, start time.Time, state []byte, valSet *cmttypes.ValidatorSet) *guardChain {
	t.Helper()
	require.NoError(t, initGuardChain(app, chainID, start, 1, state))
	c := &guardChain{app: app, valSet: valSet}
	_, err := c.block(1, start)
	require.NoError(t, err, "the first block after a genesis built by this binary")
	c.commit(t)
	return c
}

// newGuardChain starts the application on a genesis built by this binary and commits its first block.
func newGuardChain(t *testing.T, db dbm.DB, trace io.Writer, start time.Time) *guardChain {
	t.Helper()
	state, valSet := guardGenesis(t)
	return startGuardChain(t, newGuardApp(t, db, trace), guardChainID, start, state, valSet)
}

func (c *guardChain) block(height int64, at time.Time) (*abci.ResponseFinalizeBlock, error) {
	return c.blockOf(height, at, nil)
}

// blockOf finalizes the block of that hash. After propose with the same hash, FinalizeBlock collects the
// optimistic execution; with another hash it aborts it and runs the block again.
func (c *guardChain) blockOf(height int64, at time.Time, hash []byte) (*abci.ResponseFinalizeBlock, error) {
	return c.app.FinalizeBlock(&abci.RequestFinalizeBlock{
		Height:             height,
		Time:               at,
		Hash:               hash,
		NextValidatorsHash: c.valSet.Hash(),
		ProposerAddress:    c.valSet.Proposer.Address,
	})
}

// propose runs ProcessProposal. Above the first block, an accepted proposal starts the block's optimistic
// execution, the path of every block on a live node (app.go enables it).
func (c *guardChain) propose(t *testing.T, height int64, at time.Time, hash []byte) {
	t.Helper()
	rsp, err := c.app.ProcessProposal(&abci.RequestProcessProposal{
		Height:             height,
		Time:               at,
		Hash:               hash,
		NextValidatorsHash: c.valSet.Hash(),
		ProposerAddress:    c.valSet.Proposer.Address,
	})
	require.NoError(t, err)
	require.Equal(t, abci.ResponseProcessProposal_ACCEPT, rsp.Status, "ProcessProposal runs before the guard and accepts")
}

func guardHash(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:]
}

func (c *guardChain) commit(t *testing.T) {
	t.Helper()
	_, err := c.app.Commit()
	require.NoError(t, err)
}

// headCtx works on the application's own store: what it writes is read by the next block.
func (c *guardChain) headCtx() sdk.Context {
	h := c.app.LastBlockHeight()
	return c.app.NewUncachedContext(false, cmtproto.Header{Height: h, ChainID: guardChainID}).
		WithHeaderInfo(header.Info{Height: h, ChainID: guardChainID})
}

func (c *guardChain) stored(t *testing.T) module.VersionMap {
	t.Helper()
	vm, err := c.app.UpgradeKeeper.GetModuleVersionMap(c.headCtx())
	require.NoError(t, err)
	return vm
}

// setStoredVersions replaces the stored version map, the entries vm lacks removed.
func (c *guardChain) setStoredVersions(t *testing.T, vm module.VersionMap) {
	t.Helper()
	ctx := c.headCtx()
	store := prefix.NewStore(ctx.KVStore(c.app.GetKey(upgradetypes.StoreKey)), []byte{upgradetypes.VersionMapByte})
	var keys [][]byte
	it := store.Iterator(nil, nil)
	for ; it.Valid(); it.Next() {
		keys = append(keys, bytes.Clone(it.Key()))
	}
	require.NoError(t, it.Close())
	for _, k := range keys {
		store.Delete(k)
	}
	require.NoError(t, c.app.UpgradeKeeper.SetModuleVersionMap(ctx, vm))
	require.Equal(t, vm, c.stored(t))
}

// toEpoch14 stores what a node of the epoch 14 release stores.
func (c *guardChain) toEpoch14(t *testing.T) {
	t.Helper()
	require.Equal(t, uint64(2), c.stored(t)[jobstypes.ModuleName], "premise: a genesis of this binary stores x/jobs at 2")
	c.setStoredVersions(t, versionsBeforePlan[UpgradeEpoch15])
}

func (c *guardChain) schedulePlan(t *testing.T, name string, height int64) {
	t.Helper()
	require.NoError(t, c.app.UpgradeKeeper.ScheduleUpgrade(c.headCtx(), upgradetypes.Plan{Name: name, Height: height}))
}

func (c *guardChain) schedulePlanAt(t *testing.T, height int64) {
	t.Helper()
	c.schedulePlan(t, UpgradeEpoch15, height)
}

// restoreInto takes a snapshot of the last committed state with the store's own snapshotter and restores it
// into a new home, the gesture state sync makes.
func (c *guardChain) restoreInto(t *testing.T) *guardChain {
	t.Helper()
	h := uint64(c.app.LastBlockHeight())
	var chunk bytes.Buffer
	require.NoError(t, c.app.CommitMultiStore().(*rootmulti.Store).Snapshot(h, protoio.NewDelimitedWriter(&chunk)))
	home := newGuardApp(t, dbm.NewMemDB(), nil)
	_, err := home.CommitMultiStore().(*rootmulti.Store).Restore(h, snapshottypes.CurrentFormat,
		protoio.NewDelimitedReader(&chunk, 1<<30))
	require.NoError(t, err)
	require.Equal(t, c.app.LastCommitID(), home.LastCommitID(), "the restored home holds the state it was taken from")
	return &guardChain{app: home, valSet: c.valSet}
}

func TestEpochGuardPassesAGenesisOfThisBinary(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	require.Equal(t, c.app.ModuleManager.GetVersionMap(), c.stored(t), "the InitChainer stores this binary's versions")
	_, err := c.block(2, guardStart.Add(time.Second))
	require.NoError(t, err)
}

func TestEpochGuardRefusesAnEpoch14StateWithNoPlan(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.toEpoch14(t)
	_, err := c.app.UpgradeKeeper.GetUpgradePlan(c.headCtx())
	require.ErrorIs(t, err, upgradetypes.ErrNoUpgradePlanFound, "premise: no plan on chain, so the upgrade module checks nothing")

	working := c.app.CommitMultiStore().WorkingHash()
	_, err = c.block(2, guardStart.Add(time.Second))
	require.Error(t, err, "the first block on an epoch 14 state is refused")
	for _, want := range []string{
		"the chain state holds module jobs at version 1 and this binary runs version 2;",
		fmt.Sprintf("the upgrade plan %q", UpgradeEpoch15),
		"Run the newest v0.2 release",
		"dendrad query upgrade module-versions jobs",
		// A node that has exited has no RPC to answer that query.
		"A node that has exited answers nothing: add --node and the RPC address of a node that runs",
	} {
		require.Contains(t, err.Error(), want)
	}
	// FinalizeBlock writes a block's state into the store before it answers, and Commit only persists it: an
	// unchanged working hash says that nothing of the refused block reached the store. The restart test below
	// shows the replay of that block.
	require.Equal(t, working, c.app.CommitMultiStore().WorkingHash(), "nothing of the refused block reaches the store")
}

// The refusal reads the state and the binary only: a restart of the node on its database, a later clock, and
// another node with another validator key and home give the same error.
func TestEpochGuardRefusalIsTheSameOnEveryNodeAndRestart(t *testing.T) {
	db := dbm.NewMemDB()
	a := newGuardChain(t, db, nil, guardStart)
	a.toEpoch14(t)
	a.app.CommitMultiStore().Commit() // the epoch 14 state, persisted at height 2
	_, errA := a.block(3, guardStart.Add(time.Second))
	require.Error(t, errA)

	restarted := &guardChain{app: newGuardApp(t, db, nil), valSet: a.valSet}
	require.Equal(t, int64(2), restarted.app.LastBlockHeight())
	_, errB := restarted.block(3, guardStart.Add(time.Hour))
	require.Error(t, errB)

	b := newGuardChain(t, dbm.NewMemDB(), nil, guardStart.AddDate(5, 0, 0))
	b.toEpoch14(t)
	_, errC := b.block(2, guardStart.AddDate(5, 0, 1))
	require.Error(t, errC)

	require.Equal(t, errA.Error(), errB.Error(), "a restart replays the block and stops with the same error")
	require.Equal(t, errA.Error(), errC.Error(), "another node, key and clock: the same error")
}

// A live node runs each block as an optimistic execution started by ProcessProposal, in a goroutine that
// recovers nothing. The refusal comes back from FinalizeBlock as an error, nothing of the block reaches the
// store, and a restart, which replays the block without optimistic execution, gives the same error.
func TestEpochGuardRefusesUnderOptimisticExecution(t *testing.T) {
	db := dbm.NewMemDB()
	c := newGuardChain(t, db, nil, guardStart)
	c.toEpoch14(t)
	c.app.CommitMultiStore().Commit() // the epoch 14 state, persisted at height 2
	at := guardStart.Add(time.Second)
	hash := guardHash("block 3")
	working := c.app.CommitMultiStore().WorkingHash()

	c.propose(t, 3, at, hash)
	var err error
	require.NotPanics(t, func() { _, err = c.blockOf(3, at, hash) })
	require.ErrorContains(t, err, "consensus epoch guard")
	require.Equal(t, working, c.app.CommitMultiStore().WorkingHash(), "nothing of the refused block reaches the store")

	restarted := &guardChain{app: newGuardApp(t, db, nil), valSet: c.valSet}
	require.Equal(t, int64(2), restarted.app.LastBlockHeight())
	_, replayErr := restarted.blockOf(3, at, hash)
	require.Error(t, replayErr)
	require.Equal(t, err.Error(), replayErr.Error())
}

func TestEpochGuardLetsThePlanHeightThrough(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.toEpoch14(t)
	c.schedulePlanAt(t, 2)
	_, err := c.block(2, guardStart.Add(time.Second))
	require.NoError(t, err, "at the plan's height the handler runs in the modules' PreBlock, before the guard reads")
	c.commit(t)
	require.Equal(t, uint64(2), c.stored(t)[jobstypes.ModuleName], "the handler moved x/jobs from 1 to 2")
	done, err := c.app.UpgradeKeeper.GetDoneHeight(c.headCtx(), UpgradeEpoch15)
	require.NoError(t, err)
	require.Equal(t, int64(2), done)
	_, err = c.block(3, guardStart.Add(2*time.Second))
	require.NoError(t, err, "the blocks after the plan pass")
}

// At the plan's height a node that runs the block optimistically, a node whose optimistic execution was
// aborted by a later round, and a node that replays the block compute the same app hash, at that height and
// at the next one.
func TestEpochGuardPlanHeightOptimisticAndReplayAgree(t *testing.T) {
	state, valSet := guardGenesis(t)
	atPlan := func() *guardChain {
		c := startGuardChain(t, newGuardApp(t, dbm.NewMemDB(), nil), guardChainID, guardStart, state, valSet)
		c.toEpoch14(t)
		c.schedulePlanAt(t, 2)
		return c
	}
	at := guardStart.Add(time.Second)
	hash := guardHash("block 2")

	replay := atPlan()
	r1, err := replay.blockOf(2, at, hash)
	require.NoError(t, err)
	replay.commit(t)

	optimistic := atPlan()
	optimistic.propose(t, 2, at, hash)
	r2, err := optimistic.blockOf(2, at, hash)
	require.NoError(t, err)
	optimistic.commit(t)

	aborted := atPlan()
	aborted.propose(t, 2, at, guardHash("round 0"))
	aborted.propose(t, 2, at, guardHash("round 1"))
	r3, err := aborted.blockOf(2, at, hash)
	require.NoError(t, err)
	aborted.commit(t)

	require.Equal(t, r1.AppHash, r2.AppHash, "optimistic execution and replay agree at the plan's height")
	require.Equal(t, r1.AppHash, r3.AppHash, "an aborted optimistic execution and replay agree at the plan's height")
	for _, c := range []*guardChain{replay, optimistic, aborted} {
		require.Equal(t, uint64(2), c.stored(t)[jobstypes.ModuleName])
	}

	next := guardHash("block 3")
	optimistic.propose(t, 3, at.Add(time.Second), next)
	a, err := optimistic.blockOf(3, at.Add(time.Second), next)
	require.NoError(t, err)
	b, err := replay.blockOf(3, at.Add(time.Second), next)
	require.NoError(t, err)
	require.Equal(t, a.AppHash, b.AppHash, "and at the block after it")
}

// A plan skipped with --unsafe-skip-upgrades on this binary: the upgrade module clears the plan without running
// its handler, the state stays at epoch 14, and the guard stops the node at the plan's height. The message says
// where such a network stays.
func TestEpochGuardOnAPlanSkippedByThisBinary(t *testing.T) {
	db := dbm.NewMemDB()
	c := newGuardChain(t, db, nil, guardStart)
	c.toEpoch14(t)
	c.schedulePlanAt(t, 3)
	c.app.CommitMultiStore().Commit() // height 2 as the epoch 14 release committed it, the plan pending at 3

	skipping := &guardChain{
		app:    newGuardAppOn(t, db, nil, guardChainID, simtestutil.AppOptionsMap{server.FlagUnsafeSkipUpgrades: []int{3}}),
		valSet: c.valSet,
	}
	require.True(t, skipping.app.UpgradeKeeper.IsSkipHeight(3), "premise: the option reaches the upgrade module")
	_, err := skipping.block(3, guardStart.Add(time.Second))
	require.ErrorContains(t, err, "module jobs at version 1")
	require.ErrorContains(t, err, "a network that skipped the plan with --unsafe-skip-upgrades stays on v0.2")
}

func TestEpochGuardOnAStateSyncedHome(t *testing.T) {
	// A snapshot taken after the plan: the home holds x/jobs at 2, passes, and computes what its source does.
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.toEpoch14(t)
	c.schedulePlanAt(t, 2)
	_, err := c.block(2, guardStart.Add(time.Second))
	require.NoError(t, err)
	c.commit(t)
	home := c.restoreInto(t)
	require.Equal(t, uint64(2), home.stored(t)[jobstypes.ModuleName])
	at := guardStart.Add(2 * time.Second)
	want, err := c.block(3, at)
	require.NoError(t, err)
	got, err := home.block(3, at)
	require.NoError(t, err, "a home synchronized after the plan passes")
	require.Equal(t, want.AppHash, got.AppHash)

	// A snapshot of an epoch 14 state: the home is refused at its first block, as its source would be.
	old := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	old.toEpoch14(t)
	old.app.CommitMultiStore().Commit()
	oldHome := old.restoreInto(t)
	_, err = oldHome.block(oldHome.app.LastBlockHeight()+1, at)
	require.ErrorContains(t, err, "module jobs at version 1")
}

// On a block of an epoch 15 state the shipped PreBlocker does what the modules' PreBlock does: same response,
// same events, same gas on the block's meter, no write. The store trace sees the guard's reads, so a write
// would show.
func TestEpochGuardChangesNothingOnAnEpoch15Block(t *testing.T) {
	var trace bytes.Buffer
	c := newGuardChain(t, dbm.NewMemDB(), &trace, guardStart)
	shippedPreBlocker := func(ctx sdk.Context) (*sdk.ResponsePreBlock, error) {
		// The one FinalizeBlock calls; runtime.App's own PreBlocker method is the modules' only.
		return c.app.BaseApp.PreBlocker()(ctx, &abci.RequestFinalizeBlock{Height: 2})
	}
	shipped := c.preBlockRun(t, &trace, 2, shippedPreBlocker)
	modules := c.preBlockRun(t, &trace, 2, c.app.ModuleManager.PreBlock)
	require.NoError(t, shipped.err)
	require.NoError(t, modules.err)
	require.Equal(t, modules.rsp, shipped.rsp)
	require.Equal(t, modules.events, shipped.events)
	require.Equal(t, modules.gas, shipped.gas, "the guard meters its reads on a gas meter of its own")
	require.Zero(t, modules.writes)
	require.Zero(t, shipped.writes, "the guard writes nothing")
	require.Greater(t, shipped.reads, modules.reads, "the trace sees the guard's reads")

	// At the plan's height the response asks baseapp to reload the consensus parameters; the guard keeps it.
	c.toEpoch14(t)
	c.schedulePlanAt(t, 2)
	atPlan := c.preBlockRun(t, &trace, 2, shippedPreBlocker)
	require.NoError(t, atPlan.err)
	require.True(t, atPlan.rsp.ConsensusParamsChanged)
}

type preBlockResult struct {
	rsp           *sdk.ResponsePreBlock
	err           error
	events        sdk.Events
	gas           storetypes.Gas
	reads, writes int
}

// preBlockRun runs f on a branch of the state two cache levels deep. The inner level is written into the outer
// one through the store trace, so every write f made is traced; the outer level is dropped.
func (c *guardChain) preBlockRun(t *testing.T, trace *bytes.Buffer, height int64,
	f func(sdk.Context) (*sdk.ResponsePreBlock, error),
) preBlockResult {
	t.Helper()
	inner := c.app.CommitMultiStore().CacheMultiStore().CacheMultiStore()
	ctx := sdk.NewContext(inner, cmtproto.Header{Height: height, ChainID: guardChainID}, false, log.NewNopLogger()).
		WithHeaderInfo(header.Info{Height: height, ChainID: guardChainID}).
		WithEventManager(sdk.NewEventManager())
	trace.Reset()
	rsp, err := f(ctx)
	inner.Write()
	r := preBlockResult{rsp: rsp, err: err, events: ctx.EventManager().Events(), gas: ctx.GasMeter().GasConsumed()}
	r.reads, r.writes = countTrace(t, trace)
	return r
}

func countTrace(t *testing.T, trace *bytes.Buffer) (reads, writes int) {
	t.Helper()
	sc := bufio.NewScanner(bytes.NewReader(trace.Bytes()))
	sc.Buffer(make([]byte, 0, 1<<20), 1<<26)
	for sc.Scan() {
		var op struct {
			Operation string `json:"operation"`
		}
		require.NoError(t, json.Unmarshal(sc.Bytes(), &op))
		switch op.Operation {
		case "read", "iterKey", "iterValue":
			reads++
		case "write", "delete":
			writes++
		default:
			t.Fatalf("unknown store trace operation %q", op.Operation)
		}
	}
	require.NoError(t, sc.Err())
	return reads, writes
}

func TestEpochGuardDecision(t *testing.T) {
	plan := lastPlanOf(upgradePlans)
	before := epochGuardReleaseBefore[plan]
	binary := module.VersionMap{jobstypes.ModuleName: 2, "bank": 4, "emission": 1}
	for _, tc := range []struct {
		name   string
		stored module.VersionMap
		says   []string // empty: the state passes
	}{
		{"epoch 14 state", module.VersionMap{jobstypes.ModuleName: 1, "bank": 4, "emission": 1},
			[]string{"holds module jobs at version 1 and this binary runs version 2;"}},
		{"no entry for jobs", module.VersionMap{"bank": 4, "emission": 1}, []string{"module jobs at no version"}},
		{"an entry at 0", module.VersionMap{jobstypes.ModuleName: 0, "bank": 4, "emission": 1}, []string{"module jobs at version 0"}},
		{"another module below", module.VersionMap{jobstypes.ModuleName: 2, "bank": 3, "emission": 1},
			[]string{"holds module bank at version 3 and this binary runs version 4;", "module-versions bank"}},
		{"another module absent", module.VersionMap{jobstypes.ModuleName: 2, "bank": 4},
			[]string{"holds module emission at no version and this binary runs version 1;"}},
		{"two modules below, in the order of their names", module.VersionMap{jobstypes.ModuleName: 1, "bank": 3, "emission": 1},
			[]string{"holds module bank at version 3 and this binary runs version 4, module jobs at version 1 and this binary runs version 2;",
				"module-versions bank"}},
		{"empty map", module.VersionMap{}, []string{"holds module bank at no version"}},
		{"epoch 15 state", module.VersionMap{jobstypes.ModuleName: 2, "bank": 4, "emission": 1}, nil},
		{"a module this binary does not run", module.VersionMap{jobstypes.ModuleName: 2, "bank": 4, "emission": 1, "nft": 1}, nil},
		// A later state on this binary passes here; the upgrade module refuses it at the first block, since
		// this binary has no handler for the plan that state applied last.
		{"later state", module.VersionMap{jobstypes.ModuleName: 3, "bank": 5, "emission": 2}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := epochGuard(tc.stored, binary, plan, before)
			if len(tc.says) == 0 {
				require.NoError(t, err)
				return
			}
			for _, s := range tc.says {
				require.ErrorContains(t, err, s)
			}
			require.ErrorContains(t, err, fmt.Sprintf("the upgrade plan %q", plan))
			require.ErrorContains(t, err, "Run the newest "+before+" release")
		})
	}
	require.ErrorContains(t, epochGuard(module.VersionMap{jobstypes.ModuleName: 2}, module.VersionMap{}, plan, before),
		"this binary registers no module version")

	// A module this binary runs at version 0 (runtime, 06-solomachine and 07-tendermint) has an entry at 0 in the
	// state of a chain that ran this binary's InitChainer or its plan; a state without that entry is refused.
	withZero := module.VersionMap{jobstypes.ModuleName: 2, "runtime": 0}
	require.ErrorContains(t, epochGuard(module.VersionMap{jobstypes.ModuleName: 2}, withZero, plan, before),
		"holds module runtime at no version and this binary runs version 0;")
	require.NoError(t, epochGuard(module.VersionMap{jobstypes.ModuleName: 2, "runtime": 0}, withZero, plan, before))

	// A binary that registers no plan says so, and names no release to run.
	err := epochGuard(module.VersionMap{jobstypes.ModuleName: 1}, module.VersionMap{jobstypes.ModuleName: 2}, "", "")
	require.ErrorContains(t, err, "this binary registers no upgrade plan that writes its versions")
	require.NotContains(t, err.Error(), "the upgrade plan")
	require.NotContains(t, err.Error(), "release")
}

// The PreBlocker refuses a block when the stored versions cannot be read: a state whose versions are not read is
// not shown to be at this binary's epoch. The upgrade keeper's reader fails only on an invalid store iterator, so
// a reader that fails stands in for it here.
func TestEpochGuardRefusesWhenTheStoredVersionsCannotBeRead(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	binary := c.app.ModuleManager.GetVersionMap()
	unread := errors.New("invalid iterator")
	for _, got := range []module.VersionMap{nil, binary} {
		err := refuseStoredVersions(c.headCtx(), func(context.Context) (module.VersionMap, error) { return got, unread }, binary)
		require.ErrorIs(t, err, unread)
		require.ErrorContains(t, err, "consensus epoch guard: reading the stored module versions")
	}
	require.NoError(t, refuseStoredVersions(c.headCtx(), c.app.UpgradeKeeper.GetModuleVersionMap, binary),
		"the upgrade keeper's reader on a state of this binary")
}

// The message names the last plan of upgradePlans and the release line before it, and every plan names that
// line, so the plan a later epoch adds words its own refusal.
func TestEpochGuardNamesTheLastPlan(t *testing.T) {
	require.NotEmpty(t, upgradePlans)
	line := regexp.MustCompile(`^v[0-9]+[.][0-9]+$`)
	for _, p := range upgradePlans {
		require.Regexp(t, line, epochGuardReleaseBefore[p], "plan %s: name the release line that runs the chain up to its height", p)
	}
	require.Equal(t, upgradePlans[len(upgradePlans)-1], lastPlanOf(upgradePlans))
	later := append(append([]string{}, upgradePlans...), "epoch-16-later")
	require.Equal(t, "epoch-16-later", lastPlanOf(later))
	require.Equal(t, "", lastPlanOf(nil))

	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.toEpoch14(t)
	_, err := c.block(2, guardStart.Add(time.Second))
	last := lastPlanOf(upgradePlans)
	require.ErrorContains(t, err, fmt.Sprintf("the upgrade plan %q", last))
	require.ErrorContains(t, err, "Run the newest "+epochGuardReleaseBefore[last]+" release")

	err = epochGuard(module.VersionMap{jobstypes.ModuleName: 2}, module.VersionMap{jobstypes.ModuleName: 3}, "epoch-16-later", "v0.3")
	require.ErrorContains(t, err, `the upgrade plan "epoch-16-later"`)
	require.ErrorContains(t, err, "Run the newest v0.3 release")
	require.ErrorContains(t, err, "stays on v0.3")
}

// The guard tells the state the release before the last plan stores from this binary's, and the plan's handler
// moves the one into the other. A plan that moves no version leaves both equal and fails here: the guard would
// not see that epoch.
func TestEpochGuardSeesTheLastPlan(t *testing.T) {
	plan := lastPlanOf(upgradePlans)
	before, ok := versionsBeforePlan[plan]
	require.True(t, ok, "plan %s: pin in versionsBeforePlan the module versions the release before it stores, "+
		"read on a node of that release with: dendrad query upgrade module-versions", plan)

	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.setStoredVersions(t, before)
	_, err := c.block(2, guardStart.Add(time.Second))
	require.ErrorContains(t, err, "consensus epoch guard", "the state before plan %s passes the guard", plan)

	c = newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	c.setStoredVersions(t, before)
	c.schedulePlan(t, plan, 2)
	_, err = c.block(2, guardStart.Add(time.Second))
	require.NoError(t, err, "the plan's handler runs before the guard reads")
	c.commit(t)
	stored := c.stored(t)
	for name, v := range c.app.ModuleManager.GetVersionMap() {
		require.Equal(t, v, stored[name], "module %s after the plan", name)
	}
	_, err = c.block(3, guardStart.Add(2*time.Second))
	require.NoError(t, err)
}

// The genesis dendra-testnet started from, as the network serves it (curl -s
// https://testnet-api.dendranetwork.com/genesis.json, fields chain_id and genesis_time).
var servedEpoch14Genesis = struct {
	chainID string
	at      time.Time
}{"dendra-testnet", time.Date(2026, time.October, 6, 23, 25, 57, 565805212, time.UTC)}

// genesisRefusal is what the refusal at InitChain says, whether the file is the genesis itself or a zero-height
// export of that chain.
var genesisRefusal = []string{
	"consensus epoch guard: this genesis carries the chain id dendra-testnet and the genesis time that network started from at epoch 14",
	fmt.Sprintf("before the upgrade plan %q", UpgradeEpoch15),
	"It is that genesis, or an export of that chain at zero height, which keeps both fields (dendrad export --for-zero-height)",
	"Join with state sync from a snapshot taken after the plan's height",
	"the newest v0.2 release",
	"A chain restarted from a zero-height export takes a new genesis_time and an empty data directory",
}

// This binary refuses to replay the epoch 14 history of dendra-testnet from its genesis, at InitChain, before
// the InitChainer stores this binary's versions over that state. The refusal reads the chain id, the genesis
// time and the initial height: another chain id, another genesis time, or an export of that chain at a height
// (initial height that height plus one), starts.
func TestEpochGuardRefusesTheGenesisDendraTestnetStartedFrom(t *testing.T) {
	g := servedEpoch14Genesis
	state, valSet := guardGenesis(t)

	// Initial height 0, as a zero-height export writes it, is read as 1 by genesis loading and by baseapp.
	for _, initial := range []int64{1, 0} {
		err := initGuardChain(newGuardAppOn(t, dbm.NewMemDB(), nil, g.chainID, nil), g.chainID, g.at, initial, state)
		for _, want := range genesisRefusal {
			require.ErrorContains(t, err, want, "initial height %d", initial)
		}
	}

	c := startGuardChain(t, newGuardAppOn(t, dbm.NewMemDB(), nil, g.chainID, nil), g.chainID, g.at.Add(time.Nanosecond), state, valSet)
	require.Equal(t, c.app.ModuleManager.GetVersionMap(), c.stored(t), "a new genesis of the same name starts")
	require.NoError(t, initGuardChain(newGuardAppOn(t, dbm.NewMemDB(), nil, "dendra-other", nil), "dendra-other", g.at, 1, state),
		"another chain id with the same genesis time starts")
	for _, initial := range []int64{2, 10} {
		require.NoError(t, initGuardChain(newGuardAppOn(t, dbm.NewMemDB(), nil, g.chainID, nil), g.chainID, g.at, initial, state),
			"an export of that chain at a height starts at initial height %d", initial)
	}
}

// A zero-height export (dendrad export --for-zero-height) keeps the chain id and the genesis time of the node's
// genesis file and writes initial height 0 (cosmos-sdk server/export.go), which genesis loading reads as 1. An
// export of dendra-testnet is therefore refused as its genesis is, even when the exported state is at epoch 15.
// The same export with a new genesis time starts.
func TestEpochGuardRefusesAZeroHeightExportOfDendraTestnet(t *testing.T) {
	g := servedEpoch14Genesis
	state, valSet := guardGenesis(t)
	src := &guardChain{app: newGuardAppOn(t, dbm.NewMemDB(), nil, g.chainID, nil), valSet: valSet}
	require.NoError(t, initGuardChain(src.app, g.chainID, g.at, 2, state))
	_, err := src.block(2, g.at.Add(time.Second))
	require.NoError(t, err)
	src.commit(t)
	require.Equal(t, src.app.ModuleManager.GetVersionMap(), src.stored(t), "premise: an epoch 15 state")

	exported, err := src.app.ExportAppStateAndValidators(true, nil, nil)
	require.NoError(t, err)
	require.Equal(t, int64(0), exported.Height, "premise: a zero-height export")
	// The file dendrad export writes, then read the way a node reads its genesis file.
	written := func(at time.Time) *cmttypes.GenesisDoc {
		ag := genutiltypes.NewAppGenesisWithVersion(g.chainID, exported.AppState)
		ag.GenesisTime = at
		ag.InitialHeight = exported.Height
		ag.Consensus = genutiltypes.NewConsensusGenesis(exported.ConsensusParams, exported.Validators)
		require.NoError(t, ag.ValidateAndComplete())
		doc, err := ag.ToGenesisDoc()
		require.NoError(t, err)
		require.NoError(t, doc.ValidateAndComplete())
		return doc
	}

	doc := written(g.at)
	require.Equal(t, int64(1), doc.InitialHeight, "initial height 0 is read as 1")
	err = initGuardChain(newGuardAppOn(t, dbm.NewMemDB(), nil, doc.ChainID, nil), doc.ChainID, doc.GenesisTime, doc.InitialHeight, doc.AppState)
	for _, want := range genesisRefusal {
		require.ErrorContains(t, err, want)
	}

	doc = written(g.at.Add(time.Second))
	c := &guardChain{app: newGuardAppOn(t, dbm.NewMemDB(), nil, doc.ChainID, nil), valSet: valSet}
	require.NoError(t, initGuardChain(c.app, doc.ChainID, doc.GenesisTime, doc.InitialHeight, doc.AppState),
		"the export with a new genesis time starts")
	_, err = c.block(1, doc.GenesisTime.Add(time.Second))
	require.NoError(t, err, "and its first block passes the epoch guard: the exported state is at epoch 15")
}
