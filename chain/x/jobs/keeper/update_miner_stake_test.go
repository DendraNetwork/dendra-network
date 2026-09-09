package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A FIELD THE PROTO OFFERS AND THE HANDLER DISCARDS IS A PROMISE NOBODY KEEPS.
//
// `MsgUpdateMiner` carries `stake`. The handler cannot honour it — the escrow is real and this handler
// moves no coins; changing a bond means delete (refund) then re-create. It used to drop the field in
// silence, so an operator asking to reduce its bond received a SUCCESS and an unchanged escrow, with
// no way to learn that nothing had happened.
//
// ⛔ AND THE FIX HAS ONE TRAP, WHICH IS THE ZERO RULE. proto3 omits a zero value, so a client updating
// only `region` sends `stake` unset — indistinguishable from `stake = 0`. A refusal written as
// `msg.Stake != val.Stake` would reject every partial update, which is the normal case. Zero means
// "not asking"; asking for zero is `DeleteMiner`. The three cases below pin exactly that distinction,
// and the middle one is the reason the guard is not written the obvious way.

func TestUpdateMinerRefusesAnExplicitlyDifferentBond(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mUpd", "creator_upd_stake_")

	before, err := f.keeper.Miner.Get(f.ctx, "mUpd")
	require.NoError(t, err)
	require.Positive(t, before.Stake, "without a starting bond this case would prove nothing")

	_, err = srv.UpdateMiner(ctx, &types.MsgUpdateMiner{
		Creator: creator, MinerId: "mUpd", Operator: creator, Region: "eu",
		Stake: before.Stake / 2,
	})
	require.Error(t, err, "asking for a different bond must be REFUSED, not dropped in silence")

	after, err := f.keeper.Miner.Get(f.ctx, "mUpd")
	require.NoError(t, err)
	require.Equal(t, before.Stake, after.Stake, "and the bond stays what it was")
	require.Equal(t, before.Region, after.Region,
		"the refusal is WHOLE: it must not apply half the request before refusing the other half")
}

func TestUpdateMinerAppliesTheRestWhenTheBondIsAbsent(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mUpd2", "creator_upd_absent_")

	before, err := f.keeper.Miner.Get(f.ctx, "mUpd2")
	require.NoError(t, err)

	// THE NORMAL CASE, and the one a guard written too broadly would break: an unset `stake` is 0 in
	// proto3, which is indistinguishable from asking for zero. Here it means "I am not asking".
	_, err = srv.UpdateMiner(ctx, &types.MsgUpdateMiner{
		Creator: creator, MinerId: "mUpd2", Operator: creator, Region: "us",
	})
	require.NoError(t, err, "a partial update (region only) must pass: an absent stake is not a request")

	after, err := f.keeper.Miner.Get(f.ctx, "mUpd2")
	require.NoError(t, err)
	require.Equal(t, "us", after.Region, "the requested field is applied")
	require.Equal(t, before.Stake, after.Stake, "and the bond is preserved, as before")
}

func TestUpdateMinerAcceptsTheCurrentBondUnchanged(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, ctx := rgSetup(t, f, "mUpd3", "creator_upd_same_")

	before, err := f.keeper.Miner.Get(f.ctx, "mUpd3")
	require.NoError(t, err)

	// A client that reads the miner back and resends the whole object passes the SAME bond: that is not
	// a change request, and refusing it would punish the most honest shape of call.
	_, err = srv.UpdateMiner(ctx, &types.MsgUpdateMiner{
		Creator: creator, MinerId: "mUpd3", Operator: creator, Region: "ap",
		Stake: before.Stake,
	})
	require.NoError(t, err, "resending the current bond is not a change request")

	after, err := f.keeper.Miner.Get(f.ctx, "mUpd3")
	require.NoError(t, err)
	require.Equal(t, "ap", after.Region)
	require.Equal(t, before.Stake, after.Stake)
}
