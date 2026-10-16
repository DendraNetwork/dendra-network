package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	sdk "github.com/cosmos/cosmos-sdk/types"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Registration writes two per-miner dates, and both exits must forget both.
//
// `CreateMiner` writes `MinerLastCommitHeight` (later) and `MinerRegisteredHeight` (at once). Both exits,
// the voluntary `DeleteMiner` and the eviction in `pruneInactiveMiners`, must remove both, not only the
// first.
//
// This changes no decision today: every reader of `MinerRegisteredHeight` reaches it through a live miner
// (the eviction walk iterates `Miner`, the pool-freeze predicate is called with an id taken from the
// miner record), and the map is not exported at genesis, so an orphan does not survive an export/import
// round trip. The leftover is residue in a store nothing prunes, and an entry a future reader could take
// as a fact about a miner that no longer exists. The fix restores symmetry: an exit that forgets half of
// what registration wrote leaves the next reader to guess which half.
//
// These two cases keep the symmetry from being silently undone: remove either `Remove` and one of them
// goes red.

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
		"the registration date must go too: an exit that forgets half of what registration wrote "+
			"leaves an entry that reads as a fact about a miner that no longer exists")
}

func TestEviction_ForgetsBOTHDates(t *testing.T) {
	f := initFixture(t)
	_, _ = rgSetup(t, f, "mEvicted", "creator_residue_eviction")

	// Sterile identity: no commit ever, registered far outside any prune window. This is the shape the
	// eviction targets, and its registration date is its only reference.
	require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, "mEvicted", 1))

	// The eviction is driven through the shipped EndBlocker, like the neighbouring prune tests; no test-only
	// seam is exported into consensus code for a bench.
	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	has, hErr := f.keeper.Miner.Has(f.ctx, "mEvicted")
	require.NoError(t, hErr)
	require.False(t, has, "a sterile identity outside the window is evicted")

	hasReg, rErr := f.keeper.MinerRegisteredHeight.Has(f.ctx, "mEvicted")
	require.NoError(t, rErr)
	require.False(t, hasReg,
		"eviction must forget the registration date too, for the same reason as the voluntary exit")
}
