package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// RewardTraining is reserved to the authority (gov), and Units is bounded (anti-overflow).
func TestRewardTrainingGatedGO36(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	rando, err := f.addressCodec.BytesToString([]byte("randomCaller________________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: rando, Stake: 1000}))

	// (1) non-authority -> REFUSED (permissionless, it inflated any miner's TrainingPaid).
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: rando, MinerId: "m0", Units: 100})
	require.ErrorIs(t, err, types.ErrInvalidSigner)

	// (2) authority but Units out of bounds -> REFUSED (uint64 overflow guard).
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: f.authority, MinerId: "m0", Units: 1_000_000_000_000_001})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (3) authority with valid Units -> success, TrainingPaid = 100 × 10.
	_, err = srv.RewardTraining(f.ctx, &types.MsgRewardTraining{Creator: f.authority, MinerId: "m0", Units: 100})
	require.NoError(t, err)
	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(1000), pools.TrainingPaid)
}

// ClaimSubsidy: only the miner's operator may claim its subsidy (anti-grief).
func TestClaimSubsidyOperatorOnlyGO37(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	operator, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	attacker, err := f.addressCodec.BytesToString([]byte("attackerAddr________________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{MinerId: "m0", Operator: operator, Stake: 1000, Demand: 1000}))

	_, err = srv.ClaimSubsidy(f.ctx, &types.MsgClaimSubsidy{Creator: attacker, MinerId: "m0"})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized) // a third party cannot drain the miner's quota
}

// SettlePay splits `Amount` between the winners (total = Amount), NOT Amount × N.
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

	// THE JOB CARRIES ITS CLIENT, because settle-pay is reserved to it. This fixture used to plant a
	// job with an EMPTY Client and still pass: the handler read no such field, so the test proved the
	// split arithmetic on a job nobody owned. Both were wrong together, and the missing field was the
	// one an attacker used to settle someone else's job for the price of gas.
	require.NoError(t, gvSetJob(f, f.ctx, "j1", types.Job{JobId: "j1", State: "open", Client: client}))
	for _, id := range []string{"a", "b", "c"} {
		op, _ := addr20("op-" + id)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Operator: op, Stake: 1000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "j1", Amount: 3000})
	require.NoError(t, err)

	// the client paid 3000 in TOTAL (per = 1000 × 3 winners), NOT 9000.
	require.Equal(t, "7000udndr", f.bank.SpendableCoins(f.ctx, clientAddr).String())
}

// ⛔ SETTLING SOMEONE ELSE'S JOB LOCKED ITS ESCROW FOR EVER, AND IT COST GAS.
//
// `SettlePay` never read `job.Client`. Any address could settle any job -- with `Amount` at ZERO, so
// nothing was even paid -- and the `+settled` mark it writes is terminal: `Payout`, `SettleSemantic`
// and `SettleJob` all refuse afterwards through `jobIsPaid`, while `expireUnservedJob` only refunds a
// job whose state is still exactly "open". No message reached the escrow after that.
//
// The attack was already described at the top of `msg_server_settle_job.go`, where an authority guard
// closes it. The guard was never carried across to this sibling handler: a rule learned in one file
// and not propagated protects only that file.
//
// Both halves are asserted. A guard that refuses the stranger but also the client would stop every
// settlement on the network, which is the same outage by another route.
func TestSettlePayRefusesAThirdParty(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	mk := func(s string) (string, sdk.AccAddress) {
		b := make([]byte, 20)
		copy(b, s)
		o, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return o, sdk.AccAddress(b)
	}
	client, clientAddr := mk("settlepay-owner")
	stranger, strangerAddr := mk("settlepay-stranger")
	f.bank.setBalance(clientAddr, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000))))
	f.bank.setBalance(strangerAddr, sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10000))))

	require.NoError(t, gvSetJob(f, f.ctx, "jOwn", types.Job{JobId: "jOwn", State: "open", Client: client}))
	// A FULL committee: the handler refuses an incomplete one, and that refusal has nothing to do with
	// the authority guard under test -- a fixture one miner short would make this case pass for the
	// wrong reason.
	for i, id := range []string{"mOwn1", "mOwn2", "mOwn3"} {
		_, op := mk("settlepay-miner" + string(rune('a'+i)))
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{MinerId: id, Operator: op.String(), Stake: 1000}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "jOwn__"+id, types.Commit{ResultCommit: "CANON"}))
	}

	// A stranger cannot settle it, at any amount.
	_, err := srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: stranger, JobId: "jOwn", Amount: 3000})
	require.Error(t, err, "a third party settling someone else's job strands the escrow permanently")

	// And a settlement of ZERO is refused too: it pays nobody and still marks the job for good.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jOwn", Amount: 0})
	require.Error(t, err, "amount 0 pays nobody yet writes the terminal mark: it must be refused")

	// The job is untouched by either attempt -- a refusal that half-wrote would be worse than none.
	j, gErr := f.keeper.Job.Get(f.ctx, "jOwn")
	require.NoError(t, gErr)
	require.Equal(t, "open", j.State, "a refused settle-pay must leave the job exactly as it was")

	// THE OTHER HALF: the client itself still settles, or the guard is an outage.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: "jOwn", Amount: 3000})
	require.NoError(t, err, "the job's own client must still be able to settle it")
}
