package keeper_test

import (
	"testing"

	"cosmossdk.io/collections"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Epoch 15: a presence proof below the governed minimum kit writes no presence. The dormant
// case is the twin: at 0 a proof without the field is accepted as before the field existed, so the
// honest old kit is refused only once governance arms the minimum.
func TestADR059_AProofBelowTheMinimumKitWritesNoPresence(t *testing.T) {
	for _, c := range []struct {
		name     string
		min, kit uint64
		accepted bool
	}{
		{"dormant: a proof without the field is accepted", 0, 0, true},
		{"armed: a proof without the field reads 0 and is refused", 3, 0, false},
		{"armed: one below the minimum is refused", 3, 2, false},
		{"armed: at the minimum it is accepted", 3, 3, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := initFixture(t)
			p := types.DefaultParams() // mode 0: the plain echo is accepted, so the case isolates the kit check
			p.AvailEpochBlocks = 100
			p.MinKitVersion = c.min
			require.NoError(t, p.Validate())
			require.NoError(t, f.keeper.Params.Set(f.ctx, p))
			opS := ssAddr20(t, f, "kit-prover")
			require.NoError(t, gvSetMiner(f, f.ctx, "mKit", types.Miner{MinerId: "mKit", Operator: opS, Stake: 1000}))
			require.NoError(t, f.keeper.AvailChallenge.Set(f.ctx, "chal"))
			srv := keeper.NewMsgServerImpl(f.keeper)

			ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(250)
			_, err := srv.ProveAvailability(ctx, &types.MsgProveAvailability{Creator: opS, MinerId: "mKit", Challenge: "chal", KitVersion: c.kit})
			present, hErr := f.keeper.Available.Has(f.ctx, collections.Join(int64(2), "mKit"))
			require.NoError(t, hErr)
			_, lErr := f.keeper.MinerLastPresentEpoch.Get(f.ctx, "mKit")
			if c.accepted {
				require.NoError(t, err)
				require.True(t, present, "the proof is recorded for its window")
				require.NoError(t, lErr, "and the draws can read it")
				return
			}
			require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
			require.Contains(t, err.Error(), "min_kit_version", "the refusal names the parameter the operator must read")
			require.False(t, present, "a refused proof writes no presence")
			require.Error(t, lErr, "so the miner is absent at the next window: drawn neither as primary nor as juror")
		})
	}
}
