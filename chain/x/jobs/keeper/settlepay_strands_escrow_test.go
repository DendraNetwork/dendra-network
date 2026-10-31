package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A job settled through `SettlePay` must not leave its `OpenJob` escrow in the module forever.
//
// `OpenJob` moves `msg.Fee` from the client into the module account and books an expiry appointment for
// it. `SettlePay` pays the winning miners with `SendCoins`, from the client's own wallet, account to
// account, and never touches that escrow. It then appended "+settled", which is terminal:
//
//	expireUnservedJob  returns immediately unless the state is exactly "open"  -> no refund, ever;
//	Payout / SettleSemantic  both refuse through `jobIsPaid`                   -> no release either.
//
// So the client paid twice, once into escrow at OpenJob and once out of its wallet at SettlePay, and
// only the second payment reached anybody, with the handler answering success both times.
//
// This was found while checking another claim, that nothing in optimistic mode credits `miner.Demand`.
// That claim was false: `TestADR025OptimisticCutAndDemand` settles in mode 1 and asserts `Demand = 6`,
// because `if params.VerificationMode == 1 {` in `SettleSemantic` dispatches to `settleOptimistic`,
// which credits demand like the k=3 path. A grep hit is not a branch.
//
// It is not an attack: `SettlePay` is reserved to the job's own client, so the only wallet that can
// trigger it is the one that loses the money, and the tooling sends `settle-semantic` instead. It was a
// silent, self-inflicted, unrecoverable loss on a message the chain exposes and accepts.
//
// `SettlePay` refuses a job that still holds its escrow and names the path that
// works. Cases (a) and (c) check that refusal and that the escrow stays releasable; case (b) checks the
// protocol path.

// spOpenAndSettle opens a job (escrow taken), plants a single-miner committee with its commit, and
// attempts SettlePay. It returns the client address, the miner's operator, and the fee escrowed.
var spErr error // the error returned by the helper's last SettlePay attempt

func spOpenAndSettle(t *testing.T, f *fixture, jobID string) (string, string, uint64) {
	t.Helper()
	srv := keeper.NewMsgServerImpl(f.keeper)
	const fee = uint64(80) // the optimistic shape: fee 80 against a 1000 bond passes the OpenJob guard

	client := ssAddr20(t, f, "sp-client-"+jobID)
	cbz, err := f.addressCodec.StringToBytes(client)
	require.NoError(t, err)
	// Escrow plus the wallet payment, and nothing more: the mock bank answers "rich" for an address it has
	// never seen, so an unset balance would hide a transfer that never happened.
	f.bank.setBalance(sdk.AccAddress(cbz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(3080))))

	// A full committee: SettlePay refuses anything smaller ("incomplete committee"), so a single-miner
	// fixture would never reach the payment and would measure that guard instead of the escrow.
	var ops []string
	for _, id := range []string{"spM1", "spM2", "spM3"} {
		op := ssAddr20(t, f, "sp-op-"+id+"-"+jobID)
		obz, oErr := f.addressCodec.StringToBytes(op)
		require.NoError(t, oErr)
		f.bank.setBalance(sdk.AccAddress(obz), sdk.NewCoins())
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: op, Operator: op, Stake: 1000,
		}))
		ops = append(ops, op)
	}

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: jobID, Fee: fee})
	require.NoError(t, err)
	require.Equal(t, fee, f.bank.mod[types.ModuleName].AmountOf("udndr").Uint64(),
		"premise: OpenJob really escrowed the fee into the module")

	for _, id := range []string{"spM1", "spM2", "spM3"} {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: "CANON"}))
	}
	// This path refuses a job that still holds its escrow, so the helper no longer
	// settles: it attempts, and hands the error back for each case to judge.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: jobID, Amount: 3000})
	spErr = err
	for _, op := range ops {
		require.Equal(t, uint64(0), ssBal(t, f, op),
			"a refusal pays nobody: the miners' wallets have not moved")
	}
	return client, ops[0], fee
}

// (a) SettlePay refuses a job that still holds its escrow, and the client pays the fee only once.
func TestSettlePayRefusesAJobThatStillHoldsItsEscrow(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	client, first, fee := spOpenAndSettle(t, f, "jSP1")

	require.Error(t, spErr,
		"a job that still holds the escrow OpenJob took is not this path's business")
	require.Contains(t, spErr.Error(), "settle-semantic",
		"and the refusal names the path that works, or it strands the operator instead of the escrow")

	require.Equal(t, fee, f.bank.mod[types.ModuleName].AmountOf("udndr").Uint64(),
		"the escrow has not moved: a refusal displaces no funds")
	require.Equal(t, uint64(3080-80), ssBal(t, f, client),
		"the client paid the fee only: the second charge did not happen")
	require.Equal(t, uint64(0), ssBal(t, f, first))

	job, err := f.keeper.Job.Get(f.ctx, "jSP1")
	require.NoError(t, err)
	require.NotContains(t, job.State, "settled",
		"and above all: the terminal marker is not written, so every release path stays open")
	require.Equal(t, "open", job.State)
}

// (b) The control, which gives (a) its meaning: the same job settled through the path the tooling
// uses empties the module. Without this half, "the module still holds 80" could not be told apart
// from "this fixture never releases anything".
func TestSemanticSettlementDoesEmptyTheEscrow(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, _ = ssSetup(t, f, "jSP2", map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "1,2,3"}, 10000)
	_, err := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone-sp"), JobId: "jSP2",
	})
	require.NoError(t, err)
	require.True(t, f.bank.mod[types.ModuleName].IsZero(),
		"the settlement path in use pays from the escrow and leaves nothing behind")
}

// (c) After a refused settle-pay the escrow stays releasable: the job is intact, its state exactly
// "open", with no settlement marker, so the expiry and the protocol paths can still release it.
// This shows the refusal preserved something, beyond refusing.
func TestARefusedSettlePayLeavesTheEscrowRELEASABLE(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	_, _, fee := spOpenAndSettle(t, f, "jSP2")
	require.Error(t, spErr)
	require.Equal(t, fee, f.bank.mod[types.ModuleName].AmountOf("udndr").Uint64())

	job, err := f.keeper.Job.Get(f.ctx, "jSP2")
	require.NoError(t, err)
	require.Equal(t, "open", job.State,
		"the state stays exactly open, the value expireUnservedJob requires in order to refund")
	require.NotContains(t, job.State, "paid")
	require.NotContains(t, job.State, "settled",
		"no settlement marker: Payout and SettleSemantic stay open, which is what jobIsPaid tests")
}
