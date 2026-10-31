package keeper_test

import (
	"math"
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// d7KitProver arms availability windows of 100 blocks with the given min_kit_version and registers
// miner mKit with its operator. The current challenge "chal" is set only when withChallenge is true.
func d7KitProver(t *testing.T, minKit uint64, withChallenge bool) (*fixture, string) {
	t.Helper()
	f := initFixture(t)
	p := types.DefaultParams() // mode 0: the plain echo is accepted, so a refusal comes from the checks under test
	p.AvailEpochBlocks = 100
	p.MinKitVersion = minKit
	require.NoError(t, p.Validate())
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	op := ssAddr20(t, f, "kit-prover")
	require.NoError(t, gvSetMiner(f, f.ctx, "mKit", types.Miner{MinerId: "mKit", Operator: op, Stake: 1000}))
	if withChallenge {
		require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "chal"))
	}
	return f, op
}

// d7Prove sends the proof at height 250, inside window 2.
func d7Prove(f *fixture, msg *types.MsgProveAvailability) error {
	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(250)
	_, err := keeper.NewMsgServerImpl(f.keeper).ProveAvailability(ctx, msg)
	return err
}

// d7RequireNoPresence checks that no window record and no forward record exist for the miner, so the
// draws read it as absent at the next window.
func d7RequireNoPresence(t *testing.T, f *fixture) {
	t.Helper()
	it, err := f.keeper.Available.Iterate(f.ctx, nil)
	require.NoError(t, err)
	keys, err := it.Keys()
	require.NoError(t, err)
	require.Empty(t, keys, "a refused proof writes no presence")
	_, err = f.keeper.MinerLastPresentEpoch.Get(f.ctx, "mKit")
	require.ErrorIs(t, err, collections.ErrNotFound, "a refused proof leaves no record for the draws")
}

// The minimum kit version: the refusal applies whenever min_kit_version is above 0, so the smallest vote
// (1) already refuses a proof without the field, and the comparison has no ceiling.
func TestADR059_EveryMinimumAboveZeroRefusesTheKitsBelowIt(t *testing.T) {
	for _, c := range []struct {
		name     string
		min, kit uint64
		accepted bool
	}{
		{"armed at 1: a proof without the field reads 0 and is refused", 1, 0, false},
		{"armed at 1: kit 1 is accepted", 1, 1, true},
		{"armed at the top of the range: one below is refused", math.MaxUint64, math.MaxUint64 - 1, false},
		{"armed at the top of the range: at the minimum it is accepted", math.MaxUint64, math.MaxUint64, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			f, op := d7KitProver(t, c.min, true)
			err := d7Prove(f, &types.MsgProveAvailability{Creator: op, MinerId: "mKit", Challenge: "chal", KitVersion: c.kit})
			if c.accepted {
				require.NoError(t, err)
				present, hErr := f.keeper.Available.Has(f.ctx, collections.Join(int64(2), "mKit"))
				require.NoError(t, hErr)
				require.True(t, present, "the proof is recorded for its window")
				last, lErr := f.keeper.MinerLastPresentEpoch.Get(f.ctx, "mKit")
				require.NoError(t, lErr)
				require.Equal(t, uint64(2), last, "and the draws can read it")
				return
			}
			require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
			require.Contains(t, err.Error(), "min_kit_version")
			d7RequireNoPresence(t, f)
		})
	}
}

// The minimum kit version in ProveAvailability: the kit refusal sits before the challenge, so an old kit whose
// challenge is also stale or missing reads the message it can act on, which is to update the kit.
func TestADR059_AnOldKitIsToldToUpdateBeforeItsChallengeIsRead(t *testing.T) {
	for _, c := range []struct {
		name          string
		withChallenge bool
		challenge     string
	}{
		{"stale challenge", true, "stale"},
		{"no active challenge", false, "chal"},
	} {
		t.Run(c.name, func(t *testing.T) {
			f, op := d7KitProver(t, 3, c.withChallenge)
			err := d7Prove(f, &types.MsgProveAvailability{Creator: op, MinerId: "mKit", Challenge: c.challenge, KitVersion: 2})
			require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
			require.Contains(t, err.Error(), "min_kit_version", "the kit refusal is the first message")
			require.NotContains(t, err.Error(), "challenge")
			d7RequireNoPresence(t, f)

			// Twin: a current kit sending the same proof reaches the challenge check.
			err = d7Prove(f, &types.MsgProveAvailability{Creator: op, MinerId: "mKit", Challenge: c.challenge, KitVersion: 3})
			require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
			require.Contains(t, err.Error(), "challenge")
			require.NotContains(t, err.Error(), "min_kit_version")
			d7RequireNoPresence(t, f)
		})
	}
}

// The minimum kit version in ProveAvailability: the kit refusal sits after the operator check, so it names
// only a kit the miner's own operator runs; a stranger or an unknown miner gets the earlier refusal.
func TestADR059_OnlyTheOperatorIsToldItsKitIsBelowTheMinimum(t *testing.T) {
	f, op := d7KitProver(t, 3, true)
	stranger := ssAddr20(t, f, "kit-stranger")

	err := d7Prove(f, &types.MsgProveAvailability{Creator: stranger, MinerId: "mKit", Challenge: "chal", KitVersion: 0})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "a stranger is refused as not the operator")
	require.NotContains(t, err.Error(), "min_kit_version")
	d7RequireNoPresence(t, f)

	err = d7Prove(f, &types.MsgProveAvailability{Creator: op, MinerId: "mGhost", Challenge: "chal", KitVersion: 0})
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound, "an unknown miner is refused as unknown")
	require.NotContains(t, err.Error(), "min_kit_version")
	d7RequireNoPresence(t, f)

	// Twin: the operator sending the same proof is told about its kit.
	err = d7Prove(f, &types.MsgProveAvailability{Creator: op, MinerId: "mKit", Challenge: "chal", KitVersion: 0})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	require.Contains(t, err.Error(), "min_kit_version")
	d7RequireNoPresence(t, f)
}
