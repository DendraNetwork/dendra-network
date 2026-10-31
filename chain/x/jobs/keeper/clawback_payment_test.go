package keeper_test

import (
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestClawbackPaymentMutePenaltyAndZero(t *testing.T) {
	for _, c := range []struct {
		name            string
		stake, fee, bps uint64
		mute            bool
		want            uint64
	}{
		{"zero", 0, 0, 0, false, 0},
		{"no mute penalty", 1000, 100, 1000, false, 900},
		{"mute penalty", 1000, 100, 1000, true, 810},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := initFixture(t)
			aeFundModule(f)
			op := ssAddr20(t, f, "clawback-operator")
			client := ssAddr20(t, f, "clawback-client")
			require.NoError(t, f.keeper.Miner.Set(f.ctx, "m", types.Miner{MinerId: "m", Operator: op, Stake: c.stake}))
			job := types.Job{JobId: "j", MinerId: "m", Client: client, Fee: c.fee}
			p := types.DefaultParams()
			p.SilenceSlashBps = c.bps
			require.NoError(t, f.keeper.ClawbackPaymentForCoverage(f.ctx, &job, p, c.mute))
			miner, err := f.keeper.Miner.Get(f.ctx, "m")
			require.NoError(t, err)
			require.Equal(t, c.want, miner.Stake)
		})
	}
}
