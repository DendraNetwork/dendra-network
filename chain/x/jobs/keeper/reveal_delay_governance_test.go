package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// livePublicParams reproduces the parameter set this test needs: committee_seed_source=1 and
// committee_reveal_delay=0.
// It does not describe a deployed chain: docker/entrypoint-chain.sh defaults
// DENDRA_COMMITTEE_REVEAL_DELAY to 1, and the live value is read from the chain's params.
// A delay of 0 under verification_mode=1 is refused by Params.Validate; this fixture only reaches it
// because Params.Set bypasses Validate. The zero is the starting point the test needs, never a
// description of the live network.
//
// It is reproduced rather than imported, because the genesis is built by a jq filter in a shell script
// and there is no Go value to import. If the entrypoint changes, this fixture does not follow by
// itself, which is why it is written out field by field instead of hidden behind a helper.
func livePublicParams() types.Params {
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 5000
	p.MinStake = 50000
	p.SlashLeakBps = 8000
	p.FeeBurnBps = 500
	p.DisputeWindow = 10
	p.DisputeBond = 50000
	p.AuditResolveTimeout = 120
	p.HoldBps = 10000
	p.AuditMinQuorum = 4
	p.SilenceSlashBps = 2000
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfContributors = 2
	p.AvailRequireDemand = true
	p.CommitteeRevealDelay = 0 // the subject of this test, not the live value (the chain serves 1)
	return p
}

// This test establishes that `committee_reveal_delay` is reachable by governance: it does not require
// a genesis, so changing it does not have to wait for a chain reset.
//
// Several parameters in this module are genesis-only in practice because `Validate()` refuses to move
// them, and the answer decides whether a reset is the only way to make this change. `UpdateParams`
// writes the whole `Params` struct through `k.Params.Set`, so the question reduces to whether
// `Validate()` and the invariant-#8 guard accept the new set. They do, and this test shows it.
func TestRevealDelayIsGovernableWithoutAReset(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	require.NoError(t, f.keeper.Params.Set(f.ctx, livePublicParams()))
	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)

	before, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(0), before.CommitteeRevealDelay, "starting point of this fixture — not the live value")

	next := livePublicParams()
	next.CommitteeRevealDelay = 1
	_, err = ms.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: authority, Params: next})
	require.NoError(t, err, "if this fails, the parameter is genesis-only and must ride the reset")

	after, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1), after.CommitteeRevealDelay)
}

// It takes effect immediately, without a restart.
//
// "Governance can set the field" and "the chain behaves differently" are two different claims. The
// second is the one that matters, and it is observable: `OpenJob` reads the parameter on every call and
// branches on it. Before the update it writes a filled beacon (the seed frozen at open time); after the
// update it writes an empty beacon plus a pending reveal at H+delay.
//
// Both halves are asserted on the same chain instance, one after the other. A test that only checked
// the "after" state could not distinguish a working parameter from a fixture that was always going to
// produce that result.
func TestRevealDelayChangesOpenJobBehaviourLive(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	require.NoError(t, f.keeper.Params.Set(f.ctx, livePublicParams()))
	authority, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)
	client, err := f.addressCodec.BytesToString([]byte("revealDelayClient___________"))
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(40)

	// `livePublicParams()` carries the real `audit_min_quorum`, so `OpenJob` refuses to freeze a pool that
	// could never reach a verdict (ADR-042). This test is about the reveal delay, not the pool floor: the
	// population below is what its subject presupposes, and it never had to be stated while opening a job
	// was unconditional. Registering it here keeps the measured behaviour (a job that opens) rather than
	// lowering the floor to keep the fixture as it was.
	for i := 0; i < 8; i++ {
		id := fmt.Sprintf("rdMiner%d", i)
		require.NoError(t, f.keeper.MinerRegisteredHeight.Set(f.ctx, id, 1))
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Stake: 1_000_000}))
	}

	// Before, delay 0: the seed is frozen at open time, so the beacon is non-empty and no reveal is
	// pending. This is the grinding surface the parameter exists to close: with the decentralised seed
	// unavailable, the fallback is `height:time`, and the job creator sees both terms.
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jobBefore", Fee: 30})
	require.NoError(t, err)
	bBefore, err := f.keeper.Beacon.Get(ctx, "jobBefore")
	require.NoError(t, err)
	require.NotEmpty(t, bBefore.Seed, "delay=0 freezes the seed at open time")

	// Governance acts: no restart, no reset, no new genesis.
	next := livePublicParams()
	next.CommitteeRevealDelay = 1
	_, err = ms.UpdateParams(ctx, &types.MsgUpdateParams{Authority: authority, Params: next})
	require.NoError(t, err)

	// After, delay 1: the beacon is empty at open and a reveal is scheduled for H+1, whose AppHash the
	// creator cannot know when it opens the job.
	_, err = ms.OpenJob(ctx, &types.MsgOpenJob{Creator: client, JobId: "jobAfter", Fee: 30})
	require.NoError(t, err)
	bAfter, err := f.keeper.Beacon.Get(ctx, "jobAfter")
	require.NoError(t, err)
	require.Empty(t, bAfter.Seed, "delay>0 must leave the seed unset at open time")

	pending, err := f.keeper.PendingReveal.Has(ctx, collections.Join(ctx.BlockHeight()+1, "jobAfter"))
	require.NoError(t, err)
	require.True(t, pending, "a reveal must be scheduled at H+delay")
}
