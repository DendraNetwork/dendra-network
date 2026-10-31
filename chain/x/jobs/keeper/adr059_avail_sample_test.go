package keeper_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Epoch 15, `avail_sample_bps`. The draw is measured on the function, and its effect on the
// one path that reads it, the availability slash.

func TestADR059_AvailabilitySamplingIsDormantAtZeroAndDeterministicWhenArmed(t *testing.T) {
	require.True(t, keeper.AvailabilityProofOwedForTest("c", 1, "any", 0), "0: every bonded miner owes a proof, the behaviour before the field")
	require.True(t, keeper.AvailabilityProofOwedForTest("c", 1, "any", 10000), "10000 draws everyone as well")

	drawn := 0
	differs := false
	for i := 0; i < 400; i++ {
		id := fmt.Sprintf("m%03d", i)
		a := keeper.AvailabilityProofOwedForTest("chal-a", 7, id, 5000)
		require.Equal(t, a, keeper.AvailabilityProofOwedForTest("chal-a", 7, id, 5000),
			"same inputs, same answer: a draw every validator computes alike")
		if a {
			drawn++
		}
		if a != keeper.AvailabilityProofOwedForTest("chal-b", 7, id, 5000) {
			differs = true
		}
	}
	require.InDelta(t, 200, drawn, 60, "at 5000 bps about half the ids owe a proof (measured %d of 400)", drawn)
	require.True(t, differs, "the challenge is an input: a miner cannot know its epochs in advance")
}

// Armed, the draw spares an absent miner that owed no proof; dormant, the same absence is slashed as
// before. Both halves run the shipped slash pass through EndBlock.
func TestADR059_AMinerNotDrawnForAnEpochIsNotBilledForItsAbsence(t *testing.T) {
	for _, c := range []struct {
		name    string
		bps     uint64
		slashed bool
	}{
		{"dormant: every bonded miner owes a proof and the absentee is slashed", 0, true},
		{"armed at 1 bps: the absentee is not drawn, so its absence is not a failure", 1, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := initFixture(t)
			p := types.DefaultParams()
			p.AvailEpochBlocks = 4
			p.AvailDeadlineBlocks = 2
			p.AvailSlashBps = 2000
			p.AvailFailWindow = 10
			p.AvailFailK = 2
			p.AvailPayoutBps = 0
			p.AvailRequireDemand = false
			p.AvailSampleBps = c.bps
			require.NoError(t, p.Validate())
			require.NoError(t, f.keeper.Params.Set(f.ctx, p))

			srv := keeper.NewMsgServerImpl(f.keeper)
			opA, err := f.addressCodec.BytesToString([]byte("operatorAbsent059___________"))
			require.NoError(t, err)
			idA := deriveIDFor(t, f, opA)
			_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: idA, Stake: 1000})
			require.NoError(t, err)
			endBlock := func(h int64) {
				ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
				require.NoError(t, f.keeper.EndBlock(ctxH))
			}
			endBlock(4)  // rolls the epoch 1 challenge
			endBlock(8)  // epoch 1 processed: absent
			endBlock(12) // epoch 2 processed: absent again, k = 2 reached when every epoch is owed

			ma, err := f.keeper.Miner.Get(f.ctx, idA)
			require.NoError(t, err)
			if c.slashed {
				require.Equal(t, uint64(800), ma.Stake, "two owed and missed epochs: the 20 percent availability slash")
				return
			}
			require.Equal(t, uint64(1000), ma.Stake, "the epochs it was not drawn for slide in as zeros, like epochs without a bond")
		})
	}
}
