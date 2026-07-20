package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// NEW-GO-36 — RewardTraining réservé à l'autorité (gov) + Units borné (anti-overflow).
func TestRewardTrainingGatedGO36(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	rando, err := f.addressCodec.BytesToString([]byte("randomCaller________________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: rando, Stake: 1000}))

	// (1) non-autorité → REFUS (avant : permissionless, gonflait TrainingPaid de tout mineur).
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: rando, MinerId: "m0", Units: 100})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// (2) autorité mais Units hors borne → REFUS (anti-overflow uint64).
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: f.authority, MinerId: "m0", Units: 1_000_000_000_000_001})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (3) autorité + Units OK → succès, TrainingPaid = 100 × 10.
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: f.authority, MinerId: "m0", Units: 100})
	require.NoError(t, err)
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), pools.TrainingPaid)
}

// NEW-GO-37 — ClaimSubsidy : seul l'opérateur du mineur peut réclamer sa subvention (anti-grief).
func TestClaimSubsidyOperatorOnlyGO37(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	attacker, err := f.addressCodec.BytesToString([]byte("attackerAddr________________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: attacker, MinerId: "m0"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized) // un tiers ne peut pas épuiser le quota du mineur
}

// NEW-GO-38 — SettlePay répartit `Amount` entre les gagnants (total = Amount), et NON Amount × N.
func TestSettlePaySplitsAmountGO38(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	addr20 := func(s string) (string, sdk.AccAddress) {
		b := make([]byte, 20)
		copy(b, s)
		o, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return o, sdk.AccAddress(b)
	}
	client, clientAddr := addr20("settlepay-client")
	f.bank.setBalance(clientAddr, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000))))

	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", State: "open"}))
	for _, id := range []string{"a", "b", "c"} {
		op, _ := addr20("op-" + id)
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{MinerId: id, Operator: op, Stake: 1000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "j1", Amount: 3000})
	require.NoError(t, err)

	// le client a payé 3000 au TOTAL (per=1000 × 3 gagnants), PAS 9000.
	require.Equal(t, "7000udndr", f.bank.SpendableCoins(f.ctx, clientAddr).String())
}
