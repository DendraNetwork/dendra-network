package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE OWNER / OPERATOR SPLIT, AS THE CHAIN APPLIES IT TODAY — a test only, no change of consensus.
//
// A miner record carries two addresses. The CREATOR signs `create-miner`: the identifier is DERIVED from
// it, the bond is taken from it, it alone may update or delete the miner, and the bond goes back to it on
// exit and on eviction. The OPERATOR is named by the creator: it signs the commits, the availability
// proofs, the subsidy claims and the key rotations, and the chain pays it.
//
// The miner kit's owner mode (`deploy/join.sh --owner`, `DENDRA_MINER_OWNER`) is built on exactly this
// split: a cold key that never touches the mining machine registers the miner and holds the bond, and the
// machine's own key operates it. This file fixes the property that mode relies on, in both directions:
// what the hot key CAN do, and what it CANNOT.
//
// ⚠️ IT ALSO FIXES THE PRESENT LIMIT, ON PURPOSE. Payments go to the OPERATOR
// (`msg_server_claim_subsidy.go::ClaimSubsidy`, and the settlement payouts in `msg_server_payout.go::Payout`
// do the same), so a stolen hot key still collects the revenue: the split protects the CAPITAL, not the
// income. The day a creator-chosen payout address reaches the chain, the subsidy assertion below turns
// red, and that is the signal to update the kit's documentation with it.

func ownerSplitAddrs(t *testing.T, f *fixture) (cold, hot string) {
	t.Helper()
	cold, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte("owner_split_cold_key")))
	require.NoError(t, err)
	hot, err = f.addressCodec.BytesToString(sdk.AccAddress([]byte("owner_split_hot_key_")))
	require.NoError(t, err)
	return cold, hot
}

func ownerSplitBalance(t *testing.T, f *fixture, addr string) sdk.Coins {
	t.Helper()
	bz, err := f.addressCodec.StringToBytes(addr)
	require.NoError(t, err)
	return f.bank.SpendableCoins(f.ctx, sdk.AccAddress(bz))
}

// ownerSplitRegister funds the cold key, leaves the hot key EMPTY (so a bond taken from it would fail
// loudly instead of passing on the mock bank's "rich by default" accounts), and registers the miner the
// way the kit's owner mode does: signed by the cold key, operator = the hot key.
func ownerSplitRegister(t *testing.T, f *fixture, srv types.MsgServer, cold, hot string, stake uint64) string {
	t.Helper()
	coldBz, err := f.addressCodec.StringToBytes(cold)
	require.NoError(t, err)
	hotBz, err := f.addressCodec.StringToBytes(hot)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(coldBz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000_000))))
	f.bank.setBalance(sdk.AccAddress(hotBz), sdk.NewCoins())

	id := deriveIDFor(t, f, cold)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: cold, MinerId: id, Operator: hot, Stake: stake})
	require.NoError(t, err, "a cold key registers a miner operated by another address")
	return id
}

func TestOwnerHoldsBondOperatorSigns(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 10 // availability ARMED, so the handler reaches its operator check
	p.WorkGateBps = 5000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	cold, hot := ownerSplitAddrs(t, f)
	const stake = uint64(1_000_000)
	id := ownerSplitRegister(t, f, srv, cold, hot, stake)

	// THE IDENTITY AND THE BOND BELONG TO THE COLD KEY.
	rec, err := f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	require.Equal(t, cold, rec.Creator)
	require.Equal(t, hot, rec.Operator)
	require.NotEqual(t, deriveIDFor(t, f, hot), id, "the identifier is derived from the OWNER, never from the operator")
	require.Equal(t, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000_000-int64(stake)))).String(),
		ownerSplitBalance(t, f, cold).String(), "the bond is taken from the cold key")
	require.True(t, ownerSplitBalance(t, f, hot).IsZero(), "nothing is taken from the hot key")

	// WHAT THE HOT KEY CAN DO: everything the miner does while it runs.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: hot, JobId: "e7__reveals__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.NoError(t, err, "the operator anchors the miner's commits")
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: cold, JobId: "e8__reveals__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "the cold key does not operate the miner")

	_, err = srv.ProveAvailability(f.ctx, &types.MsgProveAvailability{Creator: hot, MinerId: id, Challenge: "c"})
	require.Error(t, err)
	require.NotErrorIs(t, err, sdkerrors.ErrUnauthorized, "the operator passes the availability proof's authorisation (it fails later, on the absent challenge)")
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	_, err = srv.ProveAvailability(f.ctx, &types.MsgProveAvailability{Creator: cold, MinerId: id, Challenge: "c"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: hot, MinerId: id, NewVrfPubkey: strings.Repeat("ef", 32)})
	require.NoError(t, err, "the operator rotates the miner's keys -- a stolen hot key can too")
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: cold, MinerId: id})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// THE PRESENT LIMIT: the subsidy is paid to the OPERATOR. See the header.
	rec, err = f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	rec.Demand = 1000
	require.NoError(t, f.keeper.Miner.Set(f.ctx, id, rec))
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: cold, MinerId: id})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "the cold key cannot claim")
	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: hot, MinerId: id})
	require.NoError(t, err)
	require.Equal(t, uint64(1000*5000/10000), f.emission.paid[hot], "the subsidy is credited to the OPERATOR")
	require.Zero(t, f.emission.paid[cold], "nothing reaches the owner: the split protects the capital, not the income")

	// WHAT THE HOT KEY CANNOT DO: touch the bond or the record.
	_, err = srv.UpdateMiner(f.ctx, &types.MsgUpdateMiner{Creator: hot, MinerId: id, Operator: hot, Region: "us"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "the operator cannot update the miner")
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: hot, MinerId: id})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "the operator cannot delete the miner, so it cannot withdraw the bond")
	rec, err = f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	require.Equal(t, stake, rec.Stake)
	require.True(t, ownerSplitBalance(t, f, hot).IsZero())

	// THE COLD KEY TAKES IT BACK: delete-miner refunds the bond to the CREATOR.
	_, err = srv.DeleteMiner(f.ctx, &types.MsgDeleteMiner{Creator: cold, MinerId: id})
	require.NoError(t, err)
	_, err = f.keeper.Miner.Get(f.ctx, id)
	require.Error(t, err, "the miner is gone")
	require.Equal(t, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000_000))).String(),
		ownerSplitBalance(t, f, cold).String(), "the whole bond is back on the cold key")
	require.True(t, ownerSplitBalance(t, f, hot).IsZero(), "and none of it on the hot key")
}

// EVICTION RETURNS THE BOND TO THE OWNER TOO. An owner whose mining machine dies (and with it the hot key)
// does not lose the bond: the inactive miner is evicted and the bond goes to the creator
// (`miner_vitality.go::pruneInactiveMiners`).
func TestOwnerGetsTheBondBackOnEviction(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	cold, hot := ownerSplitAddrs(t, f)
	id := ownerSplitRegister(t, f, srv, cold, hot, 1_000_000)

	require.NoError(t, f.keeper.EndBlock(sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(vitH)))

	_, err := f.keeper.Miner.Get(f.ctx, id)
	require.Error(t, err, "a miner inert beyond the prune window is evicted")
	require.Equal(t, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000_000))).String(),
		ownerSplitBalance(t, f, cold).String(), "the bond goes back to the owner")
	require.True(t, ownerSplitBalance(t, f, hot).IsZero(), "never to the operator")
}

// UPDATE-MINER REPLACES THE OPERATOR WITH WHATEVER IT CARRIES, EMPTY INCLUDED — today's behaviour, fixed
// here so that the kit's documentation stays right: `update-miner` must always carry the operator. An
// update that omits it (proto3 omits an empty string) leaves a miner no key can operate: its commits are
// refused until the owner names the operator again.
func TestOwnerUpdateWithoutOperatorSilencesTheMinerToday(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	cold, hot := ownerSplitAddrs(t, f)
	id := ownerSplitRegister(t, f, srv, cold, hot, 1_000_000)

	_, err := srv.UpdateMiner(f.ctx, &types.MsgUpdateMiner{Creator: cold, MinerId: id, Region: "us"})
	require.NoError(t, err)
	rec, err := f.keeper.Miner.Get(f.ctx, id)
	require.NoError(t, err)
	require.Empty(t, rec.Operator, "an update without an operator ERASES it")
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: hot, JobId: "e7__reveals__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "the hot key can no longer commit")

	_, err = srv.UpdateMiner(f.ctx, &types.MsgUpdateMiner{Creator: cold, MinerId: id, Operator: hot, Region: "us"})
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: hot, JobId: "e7__reveals__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.NoError(t, err, "naming the operator again restores it")
}
