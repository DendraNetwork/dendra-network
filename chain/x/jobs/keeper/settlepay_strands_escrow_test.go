package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// A JOB SETTLED THROUGH `SettlePay` LEAVES ITS `OpenJob` ESCROW IN THE MODULE FOR EVER.
//
// `OpenJob` moves `msg.Fee` from the client into the module account and books an expiry appointment
// for it. `SettlePay` pays the winning miners with `SendCoins` — from the CLIENT's own wallet, account
// to account — and never touches that escrow. It then appends "+settled", which is terminal:
//
//	expireUnservedJob  returns immediately unless the state is exactly "open"  -> no refund, ever;
//	Payout / SettleSemantic / SettleJob  all refuse through `jobIsPaid`         -> no release either.
//
// So the client pays TWICE — once into escrow at OpenJob, once out of its wallet at SettlePay — and
// only the second payment reaches anybody. The handler answers success both times.
//
// ⚠️ HOW THIS FILE CAME TO EXIST, because the honest version is more useful than the tidy one. I set
// out to show something else entirely: that nothing in optimistic mode credits `miner.Demand`, which
// would have shut both emission payouts. That claim was FALSE, and an existing test said so within a
// minute of running — `TestADR025OptimisticCutAndDemand` settles in mode 1 and asserts `Demand = 6`.
// I had read `if params.VerificationMode == 1 {` in `SettleSemantic` and assumed a refusal; it is a
// DISPATCH, to `settleOptimistic`, which credits demand exactly like the k=3 path. A grep hit is not a
// branch, and the live chain's `demand = 0` says its miner has settled no job — not that it cannot.
// What survived the refutation is the escrow above, found while checking the second settlement path.
//
// ⚠️ AND WHAT THIS FILE DOES NOT CLAIM. This is not an attack: `SettlePay` is reserved to the job's own
// client, so the only wallet that can trigger it is the one that loses the money. The tooling does not
// use it either — `services/client.py` sends `settle-semantic`. It is a silent,
// self-inflicted, unrecoverable loss on a message the chain exposes and accepts, which is enough to
// pin. Whether the fix is to release the escrow, to refuse a job that carries one, or to retire the
// message is an owner's call, not this file's.
//
// ⛔ THESE CASES PIN A DEFECT, NOT A RULE — SO A FIX MUST TURN THEM RED. Cases (a) and (c) assert that
// the escrow STAYS in the module; whoever closes this will see them fail, and that failure is the
// signal working, not a regression. Invert the assertions in the same pass as the fix. Case (b) is the
// only one that pins a rule, and it must stay green either way.

// spOuvreEtRegle opens a job (escrow taken), plants a single-miner committee with its commit, and
// settles through SettlePay. Returns the client address, the miner's operator, and the fee escrowed.
var spErr error // the error returned by the helper's last SettlePay attempt

func spOuvreEtRegle(t *testing.T, f *fixture, jobID string) (string, string, uint64) {
	t.Helper()
	srv := keeper.NewMsgServerImpl(f.keeper)
	const fee = uint64(80) // the ADR-025 shape: fee 80 against a 1000 bond passes the OpenJob guard

	client := ssAddr20(t, f, "sp-client-"+jobID)
	cbz, err := f.addressCodec.StringToBytes(client)
	require.NoError(t, err)
	// Escrow + the wallet payment, and nothing more: the mock bank answers "rich" for an address it has
	// never seen, so an unset balance would hide a transfer that never happened.
	f.bank.setBalance(sdk.AccAddress(cbz), sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(3080))))

	// A FULL committee: SettlePay refuses anything smaller ("incomplete committee"), so a single-miner
	// fixture never reaches the payment at all — it would have measured the guard, not the escrow.
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
	// Since ADR-045 entry 9 this path REFUSES a job that still holds its escrow, so the helper no longer
	// settles: it ATTEMPTS, and hands the error back for each case to judge on its own.
	_, err = srv.SettlePay(f.ctx, &types.MsgSettlePay{Creator: client, JobId: jobID, Amount: 3000})
	spErr = err
	for _, op := range ops {
		require.Equal(t, uint64(0), ssBal(t, f, op),
			"a refusal pays nobody: the miners' wallets have not moved")
	}
	return client, ops[0], fee
}

// (a) THE ESCROW IS STILL THERE AFTER A SUCCESSFUL SETTLEMENT, and the client is out both amounts.
func TestSettlePayRefusesAJobThatStillHoldsItsEscrow(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	client, premier, fee := spOuvreEtRegle(t, f, "jSP1")

	require.Error(t, spErr,
		"a job that still holds the escrow OpenJob took is not this path's business")
	require.Contains(t, spErr.Error(), "settle-semantic",
		"and the refusal NAMES the path that works, or it strands the operator instead of the escrow")

	require.Equal(t, fee, f.bank.mod[types.ModuleName].AmountOf("udndr").Uint64(),
		"the escrow has not moved: a refusal displaces no funds")
	require.Equal(t, uint64(3080-80), ssBal(t, f, client),
		"the client paid the fee ONLY: the second charge did not happen")
	require.Equal(t, uint64(0), ssBal(t, f, premier))

	job, err := f.keeper.Job.Get(f.ctx, "jSP1")
	require.NoError(t, err)
	require.NotContains(t, job.State, "settled",
		"and above all: the terminal marker is NOT written, so every release path stays open")
	require.Equal(t, "open", job.State)
}

// (b) THE CONTROL, and the reason (a) means anything: the SAME job settled through the path the
// tooling actually uses EMPTIES the module. Without this half, "the module still holds 80" could not
// be told apart from "this fixture never releases anything".
func TestSemanticSettlementDoesEmptyTheEscrow(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, _ = ssSetup(t, f, "jSP2", map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "1,2,3"}, 10000)
	_, err := srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{
		Creator: ssAddr20(t, f, "anyone-sp"), JobId: "jSP2",
	})
	require.NoError(t, err)
	require.True(t, f.bank.mod[types.ModuleName].IsZero(),
		"the settlement path in use pays FROM the escrow and leaves nothing behind")
}

// (c) NO PATH RELEASES IT AFTERWARDS. Every disbursement entry point is driven, plus the EndBlocker at
// the appointment OpenJob itself booked — a single-path check would miss the one that was added later.
// The counterpart of the previous case, and the one that shows the refusal is USEFUL rather than
// merely prudent: after a refused settle-pay the job is intact and the protocol path still empties
// the escrow. Without it we would have shown that we refuse, not that we preserved anything.
func TestARefusedSettlePayLeavesTheEscrowRELEASABLE(t *testing.T) {
	f := initFixture(t)
	seedJurorPool(t, f, 5)
	_, _, fee := spOuvreEtRegle(t, f, "jSP2")
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
