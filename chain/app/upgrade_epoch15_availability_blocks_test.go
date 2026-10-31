package app

import (
	"testing"
	"time"

	"cosmossdk.io/collections"
	dbm "github.com/cosmos/cosmos-db"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	jobskeeper "github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The epoch 15 plan next to an availability boundary, through the real FinalizeBlock and Commit. The
// keeper tests drive the handlers on hand-made contexts; these drive the blocks, the CheckTx state and
// the query context the node builds, so the roll height is read where the node reads it
// (x/jobs/keeper/availability.go, availChallengeEpoch). Windows of 4 blocks, a deadline of 2.

// availUpgradeArm arms availability on the head state, stores the challenge an epoch 14 binary would
// have rolled, and registers one bonded miner. It returns the miner's operator.
func availUpgradeArm(t *testing.T, c *guardChain) string {
	t.Helper()
	ctx := c.headCtx()
	p, err := c.app.JobsKeeper.Params.Get(ctx)
	require.NoError(t, err)
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2
	require.NoError(t, c.app.JobsKeeper.Params.Set(ctx, p))
	require.NoError(t, c.app.JobsKeeper.AvailChallenge.Set(ctx, "rolled-by-epoch-14"))
	op := sdk.AccAddress([]byte("avail-upgrade-oper01")).String()
	require.NoError(t, c.app.JobsKeeper.Miner.Set(ctx, "mAvail", jobstypes.Miner{MinerId: "mAvail", Creator: op, Operator: op, Stake: p.MinStake}))
	return op
}

func availUpgradeBlocks(t *testing.T, c *guardChain, from, to int64) {
	t.Helper()
	for h := from; h <= to; h++ {
		_, err := c.block(h, guardStart.Add(time.Duration(h)*time.Second))
		require.NoError(t, err, "block %d", h)
		c.commit(t)
	}
}

// availUpgradeProve runs ProveAvailability on a branch of the CheckTx state, what the node runs for a
// broadcast and for `--gas auto`, and returns its error and the branch.
func availUpgradeProve(c *guardChain, op, challenge string) (sdk.Context, error) {
	check, _ := c.app.GetContextForCheckTx(nil).CacheContext()
	_, err := jobskeeper.NewMsgServerImpl(c.app.JobsKeeper).ProveAvailability(check,
		&jobstypes.MsgProveAvailability{Creator: op, MinerId: "mAvail", Challenge: challenge})
	return check, err
}

// availUpgradeAfterTheRoll checks the committed boundary block 4 once the epoch 15 binary rolled it:
// the roll height is 4, the query reports epoch 1 next to the new challenge, and CheckTx accepts a
// proof of it on time and counts it for epoch 1.
func availUpgradeAfterTheRoll(t *testing.T, c *guardChain, op string) {
	t.Helper()
	rolled, err := c.app.JobsKeeper.AvailChallengeHeight.Get(c.headCtx())
	require.NoError(t, err)
	require.Equal(t, int64(4), rolled)
	qctx, err := c.app.CreateQueryContext(0, false)
	require.NoError(t, err)
	r, err := jobskeeper.NewQueryServerImpl(c.app.JobsKeeper).GetAvailChallenge(qctx, &jobstypes.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, int64(4), qctx.BlockHeight())
	require.Equal(t, int64(1), r.Epoch)
	require.NotEqual(t, "rolled-by-epoch-14", r.Challenge)
	check, err := availUpgradeProve(c, op, r.Challenge)
	require.NoError(t, err, "CheckTx on the committed boundary, deadline armed")
	has, err := c.app.JobsKeeper.Available.Has(check, collections.Join(int64(1), "mAvail"))
	require.NoError(t, err)
	require.True(t, has)
}

// The plan height is the boundary.
func TestUpgradeEpoch15PlanOnAnAvailabilityBoundaryRealBlocks(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	op := availUpgradeArm(t, c)
	availUpgradeBlocks(t, c, 2, 3)
	_, err := c.app.JobsKeeper.AvailChallengeHeight.Get(c.headCtx())
	require.ErrorIs(t, err, collections.ErrNotFound, "premise: no boundary yet")
	c.toEpoch14(t)
	c.schedulePlanAt(t, 4)
	availUpgradeBlocks(t, c, 4, 4)
	availUpgradeAfterTheRoll(t, c, op)
}

// The plan height is one block before the boundary.
func TestUpgradeEpoch15PlanOneBlockBeforeAnAvailabilityBoundaryRealBlocks(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	op := availUpgradeArm(t, c)
	availUpgradeBlocks(t, c, 2, 2)
	c.toEpoch14(t)
	c.schedulePlanAt(t, 3)
	availUpgradeBlocks(t, c, 3, 4)
	availUpgradeAfterTheRoll(t, c, op)
}

// The plan height is one block after the boundary: the halt sits on the committed boundary block 4,
// which the epoch 14 binary rolled without a roll height (removed here after the commit to leave that
// state). Until block 5 commits, the query and CheckTx count the challenge for epoch 0, and they agree:
// CheckTx refuses the proof as too late, and the query's epoch tells the miner daemon to wait. Block 5
// is the first block of the epoch 15 binary, where both count epoch 1.
func TestUpgradeEpoch15HaltAfterAnAvailabilityBoundaryRealBlocks(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	op := availUpgradeArm(t, c)
	availUpgradeBlocks(t, c, 2, 4)
	require.NoError(t, c.app.JobsKeeper.AvailChallengeHeight.Remove(c.headCtx()))
	c.toEpoch14(t)
	c.schedulePlanAt(t, 5)

	halt := c.headCtx()
	r, err := jobskeeper.NewQueryServerImpl(c.app.JobsKeeper).GetAvailChallenge(halt, &jobstypes.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.NotEqual(t, "rolled-by-epoch-14", r.Challenge, "premise: block 4 rolled the challenge")
	require.Equal(t, int64(0), r.Epoch, "no roll height on the committed boundary: one epoch lower")
	_, err = availUpgradeProve(c, op, r.Challenge)
	require.ErrorContains(t, err, "too late", "CheckTx counts the epoch the query reports")

	availUpgradeBlocks(t, c, 5, 5)
	qctx, err := c.app.CreateQueryContext(0, false)
	require.NoError(t, err)
	r, err = jobskeeper.NewQueryServerImpl(c.app.JobsKeeper).GetAvailChallenge(qctx, &jobstypes.QueryGetAvailChallengeRequest{})
	require.NoError(t, err)
	require.Equal(t, int64(1), r.Epoch)
	check, err := availUpgradeProve(c, op, r.Challenge)
	require.NoError(t, err, "CheckTx on block 5, inside the deadline of epoch 1")
	has, err := c.app.JobsKeeper.Available.Has(check, collections.Join(int64(1), "mAvail"))
	require.NoError(t, err)
	require.True(t, has)
}

// Optimistic execution and replay agree on the boundary block at the plan height.
func TestUpgradeEpoch15PlanOnAnAvailabilityBoundaryOptimisticAndReplayAgree(t *testing.T) {
	state, valSet := guardGenesis(t)
	mk := func() *guardChain {
		c := startGuardChain(t, newGuardApp(t, dbm.NewMemDB(), nil), guardChainID, guardStart, state, valSet)
		availUpgradeArm(t, c)
		availUpgradeBlocks(t, c, 2, 3)
		c.toEpoch14(t)
		c.schedulePlanAt(t, 4)
		return c
	}
	at := guardStart.Add(4 * time.Second)
	hash := guardHash("block 4")
	replay := mk()
	ra, err := replay.blockOf(4, at, hash)
	require.NoError(t, err)
	optimistic := mk()
	optimistic.propose(t, 4, at, hash)
	rb, err := optimistic.blockOf(4, at, hash)
	require.NoError(t, err)
	require.Equal(t, ra.AppHash, rb.AppHash)
}

// An export after the boundary, imported at the next height: the roll height is not carried, and the
// first roll of the imported chain writes it.
func TestUpgradeEpoch15RollHeightIsNotExported(t *testing.T) {
	c := newGuardChain(t, dbm.NewMemDB(), nil, guardStart)
	availUpgradeArm(t, c)
	require.NoError(t, c.app.JobsKeeper.Miner.Remove(c.headCtx(), "mAvail"))
	availUpgradeBlocks(t, c, 2, 4)
	exp, err := c.app.ExportAppStateAndValidators(false, nil, nil)
	require.NoError(t, err)
	n := newGuardApp(t, dbm.NewMemDB(), nil)
	require.NoError(t, initGuardChain(n, guardChainID, guardStart, exp.Height, exp.AppState))
	nc := &guardChain{app: n, valSet: c.valSet}
	for h := exp.Height; h <= 8; h++ {
		_, err := nc.block(h, guardStart.Add(time.Duration(h)*time.Second))
		require.NoError(t, err, "height %d", h)
		nc.commit(t)
		if h < 8 {
			_, gErr := n.JobsKeeper.AvailChallengeHeight.Get(nc.headCtx())
			require.ErrorIs(t, gErr, collections.ErrNotFound, "height %d: no roll yet on the imported chain", h)
		}
	}
	rolled, err := n.JobsKeeper.AvailChallengeHeight.Get(nc.headCtx())
	require.NoError(t, err)
	require.Equal(t, int64(8), rolled)
}
