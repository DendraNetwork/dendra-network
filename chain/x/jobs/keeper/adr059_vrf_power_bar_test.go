package keeper_test

import (
	"encoding/hex"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Epoch 15, `committee_min_vrf_power_bps`: the governed bar is what the seed selection
// applies, 0 selects the compiled two thirds, and the health query reports the bar in force.
func TestADR059_TheGovernedPowerBarIsWhatTheSeedSelectionApplies(t *testing.T) {
	require.Equal(t, uint64(types.VrfContributorPowerFloorBps), uint64(keeper.MinVrfContributorPowerBps),
		"the keeper applies the number Validate bounds against: one constant under two names")

	f := initFixture(t)
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(13)
	dseed := []byte{0xca, 0xfe, 0xba, 0xbe, 0x13, 0x13, 0x13, 0x13}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx, 13, dseed))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 2))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 7000))
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	require.NoError(t, f.keeper.Params.Set(ctx, p))

	seed, decentralized := f.keeper.CommitteeBaseSeedSourcedForTest(ctx, "fallback")
	require.True(t, decentralized, "7000 bps is above the compiled bar")
	require.Equal(t, dhex, seed)

	p.CommitteeMinVrfPowerBps = 8000
	require.NoError(t, p.Validate())
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	seed, decentralized = f.keeper.CommitteeBaseSeedSourcedForTest(ctx, "fallback")
	require.False(t, decentralized, "the governed bar is raised above the share: legacy fallback, never a halt")
	require.Equal(t, "fallback", seed)

	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 8000))
	_, decentralized = f.keeper.CommitteeBaseSeedSourcedForTest(ctx, "fallback")
	require.True(t, decentralized, "at the governed bar the seed is taken")

	q := keeper.NewQueryServerImpl(f.keeper)
	resp, err := q.CommitteeSeedHealth(ctx, &types.QueryCommitteeSeedHealthRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(8000), resp.CommitteeMinVrfPowerBps, "the health query reports the governed bar")
	require.Equal(t, uint64(8000), resp.ContributorPowerBps)

	p.CommitteeMinVrfPowerBps = 0
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	resp, err = q.CommitteeSeedHealth(ctx, &types.QueryCommitteeSeedHealthRequest{})
	require.NoError(t, err)
	require.Equal(t, uint64(types.VrfContributorPowerFloorBps), resp.CommitteeMinVrfPowerBps, "at 0 it reports the compiled bar")
}
