package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// REGISTRATION WRITES TWO PER-MINER DATES; BOTH EXITS MUST FORGET BOTH.
//
// `CreateMiner` writes `MinerLastCommitHeight` (later) and `MinerRegisteredHeight` (at once). Both
// exits — the voluntary `DeleteMiner` and the eviction in `pruneInactiveMiners` — used to remove the
// first and keep the second.
//
// ⛔ WHAT THIS IS AND IS NOT. It changes no decision today, and saying otherwise would be the easy
// overclaim: every reader of `MinerRegisteredHeight` reaches it through a LIVE miner (the eviction
// walk iterates `Miner`, the pool-freeze predicate is called with an id taken from the miner record),
// and the map is not exported at genesis, so an orphan does not even survive an export/import round
// trip. What it is: residue in a store nothing prunes, and an entry that a future reader can take as
// a fact about a miner that no longer exists. The fix is symmetry, not a bug fix — an exit that
// forgets half of what registration wrote leaves the next reader to guess which half.
//
// These two cases exist so the symmetry cannot be silently undone: remove either `Remove` and one of
// them goes red.

func TestVoluntaryExit_ForgetsBOTHDates(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mLeaving", "creator_voluntary_residue_")

	// The two dates registration would have written, both present before the exit.
	require.NoError(t, f.keeper.MinerLastCommitHeight.Set(f.ctx, "mLeaving", uint64(vitH-10)))
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mLeaving", uint64(vitH-1000)))

	_, err := srv.DeleteMiner(ctx, &types.MsgDeleteMiner{Creator: creator, MinerId: "mLeaving"})
	require.NoError(t, err, "no open obligation: the exit must be allowed")

	has, hErr := f.keeper.Miner.Has(f.ctx, "mLeaving")
	require.NoError(t, hErr)
	require.False(t, has, "the miner record is gone")

	hasLast, lErr := f.keeper.MinerLastCommitHeight.Has(f.ctx, "mLeaving")
	require.NoError(t, lErr)
	require.False(t, hasLast, "the last-commit date must go with it (this half already worked)")

	hasReg, rErr := f.keeper.MinerRegisteredHeight.Has(f.ctx, "mLeaving")
	require.NoError(t, rErr)
	require.False(t, hasReg,
		"the REGISTRATION date must go too: an exit that forgets half of what registration wrote "+
			"leaves an entry that reads as a fact about a miner that no longer exists")
}

func TestEviction_ForgetsBOTHDates(t *testing.T) {
	f := initFixture(t)
	_, _ = rgSetup(t, f, "mEvince", "creator_residu_eviction_")

	// Sterile identity: no commit ever, registered far outside any prune window. This is exactly the
	// shape the eviction targets — and the shape whose registration date is its only reference.
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mEvince", 1))

	// The eviction is driven through the SHIPPED EndBlocker, like the neighbouring prune tests —
	// no test-only seam is exported into consensus code for the convenience of a bench.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	has, hErr := f.keeper.Miner.Has(f.ctx, "mEvince")
	require.NoError(t, hErr)
	require.False(t, has, "a sterile identity outside the window is evicted")

	hasReg, rErr := f.keeper.MinerRegisteredHeight.Has(f.ctx, "mEvince")
	require.NoError(t, rErr)
	require.False(t, hasReg,
		"eviction must forget the registration date too, for the same reason as the voluntary exit")
}
