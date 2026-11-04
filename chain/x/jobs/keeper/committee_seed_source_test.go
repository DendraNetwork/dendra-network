package keeper_test

import (
	"encoding/hex"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Committee selection consumes the decentralized VRF seed when committee_seed_source == 1. A
// decentralized seed is written at the current height (as the PreBlocker does), then jobs are opened and
// the beacon seed checked: legacy (source=0) differs from the decentralized seed, source=1 equals it,
// and source=1 with no seed at that height falls back to legacy (non-empty).
func TestCommitteeSeedSourceDecentralized(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx7 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(7)
	dseed := []byte{0xde, 0xad, 0xbe, 0xef, 0x01, 0x02, 0x03, 0x04}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx7, 7, dseed))
	// The PreBlocker writes the contributors' power share next to the seed, and the power bar applies to
	// every seed taken on the decentralized path, including here where the governed count floor is at its
	// default of 0. A fixture that wrote only the seed would exercise the fallback instead of the source
	// selection.
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx7, 7, 6667))

	// source=0 (legacy): beacon seed = height:time, which differs from the decentralized seed
	p := types.DefaultParams()
	p.CommitteeSeedSource = 0
	require.NoError(t, f.keeper.Params.Set(ctx7, p))
	_, err = srv.OpenJob(ctx7, &types.MsgOpenJob{Creator: creator, JobId: "jLegacy", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx7, "jLegacy")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "source=0 -> legacy seed, not the decentralized one")

	// source=1: beacon seed = the decentralized VRF seed (hex) at this height
	p.CommitteeSeedSource = 1
	require.NoError(t, f.keeper.Params.Set(ctx7, p))
	_, err = srv.OpenJob(ctx7, &types.MsgOpenJob{Creator: creator, JobId: "jVrf", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx7, "jVrf")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "source=1 -> seed = the decentralized VRF seed at the current height")

	// source=1 with no seed at this height: legacy fallback (non-empty, different from the VRF seed)
	ctx9 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(9)
	require.NoError(t, f.keeper.Params.Set(ctx9, p))
	_, err = srv.OpenJob(ctx9, &types.MsgOpenJob{Creator: creator, JobId: "jNone", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx9, "jNone")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "source=1 with no seed at H -> non-empty legacy fallback")
	require.NotEqual(t, dhex, b.Seed)
}

// committee_min_vrf_contributors floor: a decentralized seed that is present but under-decentralized
// (contributors below the floor) does not draw the committee, and the legacy fallback is visible. Above
// the floor the seed is used. The chain never halts on this.
func TestCommitteeMinVrfContributors(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11)
	dseed := []byte{0xca, 0xfe, 0xba, 0xbe, 0x09, 0x08, 0x07, 0x06}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx, 11, dseed))

	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfContributors = 2 // floor = 2 contributors
	require.NoError(t, f.keeper.Params.Set(ctx, p))

	// seed present but a single contributor (below the floor of 2): legacy fallback
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 11, 1))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jUnder", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx, "jUnder")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "1 contributor < floor of 2 -> legacy fallback (under-decentralized seed not used)")
	require.NotEmpty(t, b.Seed, "legacy fallback is non-empty (no halt)")

	// 2 contributors (>= floor) and power >= 2/3 (the power bar, written by the PreBlocker next to the
	// seed): the decentralized VRF seed is used
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 11, 2))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 11, 6667))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jOver", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jOver")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "2 contributors >= floor (and power >= 2/3) -> decentralized VRF seed used")
}

// Dynamic VRF floor in power: the seed is used only if the valid VRF contributors carry at least 2/3 of
// the commit's power (MinVrfContributorPowerBps=6667), the BFT bar, which dust Sybils cannot move. A
// count-only floor could be griefed: zero-stake identities inflating N would pin the chain to the
// legacy seed. A missing power figure (a seed written by code that never measured the share) falls back
// visibly.
//
// The power bar does not depend on the governed count floor: committee_min_vrf_contributors=0 disarms
// the count bar and nothing else. Cases (e) and (f) check this, so a single parameter cannot switch off
// two guards at once.
func TestCommitteeVrfFloorDynamicPower(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(13)
	dseed := []byte{0x0d, 0x15, 0xea, 0x5e, 0x01, 0x02, 0x03, 0x04}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx, 13, dseed))

	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfContributors = 2 // static floor armed at 2
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 2))

	// (a) count OK but power absent (seed written without the power share): visible legacy fallback
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jNoPow", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx, "jNoPow")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "power absent -> legacy fallback (fails visibly)")
	require.NotEmpty(t, b.Seed, "legacy fallback is non-empty (no halt)")

	// (b) under-weighted contributors (2 dust validators on a commit dominated by others: 3000 bps < 6667):
	// legacy fallback, so dust Sybils cannot get a power-minority seed accepted
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 3000))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jLowPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jLowPow")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "power 30%% < 2/3 -> legacy fallback")

	// (c) contributors hold >= 2/3 of the power (6667 bps): the seed is used
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 6667))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jOkPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jOkPow")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "power 6667 bps >= 2/3 -> seed used")

	// (d) the static count bar stays active: 1 contributor < param 2, fallback even at full power
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 1))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 10000))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jLowCount", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jLowCount")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "count 1 < param 2 -> fallback (the static bar is unchanged)")

	// (e) count bar dormant (param=0) and power absent: fallback. The parameter an operator can set from the
	// environment disarms the count of heads; it does not reach the BFT power bar.
	p.CommitteeMinVrfContributors = 0
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 1))
	require.NoError(t, f.keeper.DeleteDecentralizedSeedContributorPower(ctx, 13))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jDormantNoPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jDormantNoPow")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "param=0 does not disarm the power bar: power absent -> fallback")
	require.NotEmpty(t, b.Seed, "legacy fallback is non-empty (no halt)")

	// (f) same dormant count, power restored above 2/3: the seed is used. Without this case, (e) would also
	// pass with a count bar that stayed armed, and would prove nothing about the power bar.
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 10000))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jDormantPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jDormantPow")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "param=0 -> count bar dormant; 1 contributor at full power -> seed used")
}

// Validate bounds committee_seed_source to {0,1}.
func TestCommitteeSeedSourceValidate(t *testing.T) {
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	require.NoError(t, p.Validate())
	p.CommitteeSeedSource = 2
	require.Error(t, p.Validate(), "a value above 1 is rejected")
}
