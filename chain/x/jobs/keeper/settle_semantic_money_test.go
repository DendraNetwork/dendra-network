package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE MONEY CORE OF THE DEFAULT SETTLEMENT PATH, WHICH NO TEST HAD EVER EXECUTED.
//
// Measured with `go tool cover` on the shipped tree: the 50 coverage blocks between L67 and L182 of
// `msg_server_settle_semantic.go` were ALL at count 0. That range is not an error branch — it is the
// payout, the burn, the outlier slash, the slash record and the client refund. `DefaultParams()`
// never sets `VerificationMode`, so its Go zero value 0 selects exactly this handler: a default
// deployment settles here, and the handler is permissionless (its only entry check is the shape of
// the caller's address).
//
// ⚠️ THE PUBLIC NETWORK RUNS MODE 1 TODAY (measured: `verification_mode = 1`), so this is not the
// code producing its blocks. Saying otherwise would be the easy overclaim. What it is: the path any
// operator gets from the defaults, moving real escrow, with nothing behind it.
//
// ⛔ AND A TEST THAT ONLY ASSERTS `require.NoError` WOULD PROVE NOTHING HERE. The mock bank answers
// `richCoins()` (10^12) for any address it has never seen (`keeper_test.go`, `balOf`), so an
// unfunded account looks rich and a missing transfer looks successful. Every case below pins the
// module escrow, each operator balance, `burned`, `Pools.Treasury` and `Stake` SEPARATELY.

// ssAddr20 builds a valid bech32 address from a short label, like the neighbouring settlement tests.
func ssAddr20(t *testing.T, f *fixture, s string) string {
	t.Helper()
	b := make([]byte, 20)
	copy(b, s)
	out, err := f.addressCodec.BytesToString(b)
	require.NoError(t, err)
	return out
}

func ssBal(t *testing.T, f *fixture, addr string) uint64 {
	t.Helper()
	bz, err := f.addressCodec.StringToBytes(addr)
	require.NoError(t, err)
	return f.bank.bal[sdk.AccAddress(bz).String()].AmountOf("udndr").Uint64()
}

// ssSetup fabricates a full committee of three miners with parsable vector commits, a funded module
// escrow, and ZEROED operator balances — without zeroing them the mock's rich default would drown the
// delta this test exists to measure.
func ssSetup(t *testing.T, f *fixture, jobID string, commits map[string]string, fee uint64) (string, map[string]string) {
	t.Helper()
	ops := map[string]string{}
	for id, c := range commits {
		op := ssAddr20(t, f, "operator-"+id)
		ops[id] = op
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: ssAddr20(t, f, "creator-"+id), Operator: op, Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: c}))
		bz, err := f.addressCodec.StringToBytes(op)
		require.NoError(t, err)
		f.bank.setBalance(sdk.AccAddress(bz), sdk.NewCoins())
	}
	client := ssAddr20(t, f, "client-"+jobID)
	cbz, err := f.addressCodec.StringToBytes(client)
	require.NoError(t, err)
	f.bank.setBalance(sdk.AccAddress(cbz), sdk.NewCoins())
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{JobId: jobID, Fee: fee, Client: client}))
	// The escrow the handler pays FROM. Without it the transfers fail on funds, not on logic.
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(fee)))
	return client, ops
}

// (a) UNANIMOUS COMMITTEE: everyone is paid, the burn is real, and the rounding surplus goes back to
// the client rather than staying in the module.
func TestSettleSemanticPaysBurnsAndRefundsTheSurplus(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	client, ops := ssSetup(t, f, "jUnan",
		map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "1,2,3"}, 10000)

	_, err := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone"), JobId: "jUnan",
	})
	require.NoError(t, err)

	// fee 10000, fee_burn_bps 500 -> burn 500, distributable 9500, 3 winners -> 3166 each, surplus 2.
	// Every number is DERIVED from the parameters above, not chosen: change fee_burn_bps and the
	// arithmetic follows, which is what makes this a rule rather than a fixture.
	for id, op := range ops {
		require.Equal(t, uint64(3166), ssBal(t, f, op), "miner %s must be paid its share of the escrow", id)
	}
	require.Equal(t, uint64(500), f.bank.burned.AmountOf("udndr").Uint64(), "the soft burn is real, not accounted")
	require.Equal(t, uint64(2), ssBal(t, f, client), "the rounding surplus goes back to the client")
	require.True(t, f.bank.mod[types.ModuleName].IsZero(), "the escrow is emptied: nothing stays in the module")

	job, err := f.keeper.Job.Get(f.ctx, "jUnan")
	require.NoError(t, err)
	require.Contains(t, job.State, "+paid+finalized")
	require.Empty(t, job.SlashRecords, "a unanimous committee slashes nobody")
}

// (b) ONE OUTLIER: the majority is paid, the divergent miner is slashed at slash_leak_bps, the amount
// funds the treasury, and the slash is RECORDED so a successful dispute can give it back.
func TestSettleSemanticSlashesTheOutlierAndRecordsIt(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, ops := ssSetup(t, f, "jOut",
		map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}, 10000)
	p, err := f.keeper.Params.Get(f.ctx)
	require.NoError(t, err)
	attendu := uint64(1000) * p.SlashLeakBps / 10000 // derived from the parameter the handler reads

	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone"), JobId: "jOut",
	})
	require.NoError(t, err)

	require.Equal(t, uint64(4750), ssBal(t, f, ops["ma"]), "2 winners share 9500")
	require.Equal(t, uint64(4750), ssBal(t, f, ops["mb"]))
	require.Zero(t, ssBal(t, f, ops["mc"]), "an outlier is not paid")

	mc, err := f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, uint64(1000)-attendu, mc.Stake, "the outlier is slashed at slash_leak_bps")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, attendu, pools.Treasury, "the slashed amount funds the treasury")

	job, err := f.keeper.Job.Get(f.ctx, "jOut")
	require.NoError(t, err)
	require.Len(t, job.SlashRecords, 1, "the slash must be RECORDED, or a successful dispute cannot restore it")
	require.Equal(t, "mc", job.SlashRecords[0].MinerId)
	require.Equal(t, attendu, job.SlashRecords[0].Amount)
}

// (c) REPLAY: the second call must not slash again. The error code alone would not prove it — a
// handler that refused AFTER slashing would return the same error. The stake and the treasury are
// what prove the guard fires first.
//
// ⚠️ WHAT THIS CASE CAN AND CANNOT ATTRIBUTE, measured rather than assumed. The replay is guarded
// TWICE: `jobIsPaid` (paid || settled) and the anti-double-slash guard on "finalized". Removing the
// second alone turns this case RED; removing the first alone leaves it GREEN, because the second one
// still refuses. That is defence in depth doing its job, not a hole in this bench — and it is written
// here so nobody "fixes" a gap that does not exist. What the case proves is that the PROTECTION is
// present; it cannot attribute the refusal to one of the two guards, and no assertion on an error
// string would change that.
func TestSettleSemanticRefusesAReplayWithoutSlashingTwice(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, _ = ssSetup(t, f, "jRep",
		map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}, 10000)
	msg := &types.MsgSettleSemantic{Creator: ssAddr20(t, f, "anyone"), JobId: "jRep"}

	_, err := srv.SettleSemantic(f.ctx, msg)
	require.NoError(t, err)
	apres, err := f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	pools1, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)

	// ⛔ RE-FUND THE MODULE FIRST, OR THIS CASE PASSES FOR THE WRONG REASON. After the first
	// settlement the escrow is empty, so a second pass would fail on funds at the FIRST payout and
	// never reach the outlier — the replay guard would look proven while never being consulted.
	// Measured: with the module left empty, removing `jobIsPaid` leaves this test green.
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(10000)))

	_, err = srv.SettleSemantic(f.ctx, msg)
	require.Error(t, err, "a settled job must not settle twice")

	rejoue, err := f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, apres.Stake, rejoue.Stake, "the replay must not slash the outlier a second time")
	pools2, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, pools1.Treasury, pools2.Treasury, "and it must not credit the treasury twice")
}

// (d) NO STRICT MAJORITY: three mutually orthogonal answers. Nobody is paid, nobody is slashed, and —
// the reason this case matters — `per := distributable / nWin` is never reached with nWin == 0.
func TestSettleSemanticRefusesWithoutAStrictMajority(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, ops := ssSetup(t, f, "jNoMaj",
		map[string]string{"ma": "1,0,0", "mb": "0,1,0", "mc": "0,0,1"}, 10000)

	_, err := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone"), JobId: "jNoMaj",
	})
	require.Error(t, err, "three orthogonal answers are not a majority")

	for id, op := range ops {
		require.Zero(t, ssBal(t, f, op), "nobody is paid when no majority forms (%s)", id)
		m, mErr := f.keeper.Miner.Get(f.ctx, id)
		require.NoError(t, mErr)
		require.Equal(t, uint64(1000), m.Stake, "and nobody is slashed either (%s)", id)
	}
	require.True(t, f.bank.burned.IsZero(), "no burn on a refused settlement")
	job, err := f.keeper.Job.Get(f.ctx, "jNoMaj")
	require.NoError(t, err)
	require.NotContains(t, job.State, "+paid", "the job stays unsettled and its escrow untouched")
}
