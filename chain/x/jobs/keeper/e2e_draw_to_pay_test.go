package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// DRAW -> SIGNED VERDICT -> CONCLUSION -> PAYMENT, THROUGH THE DOORS A REAL JUDGE USES.
//
// ⛔ WHY THIS FILE EXISTS. Every other test in this package writes verdicts straight into state:
//
//	f.keeper.Commit.Set(ctx, "jv__verdict__mD", types.Commit{ResultCommit: "0"})
//
// A real juror cannot do that. It sends `MsgCreateCommit`, and that message carries four gates the
// direct write skips entirely: the signer must be the miner's OPERATOR (H1), the miner id is
// EXTRACTED from the key rather than supplied, an anchored commit is IMMUTABLE, and the model
// registry is consulted fail-closed. The path that justifies this project — a committee is drawn, it
// judges, the chain concludes, the money moves — was therefore covered end to end by nothing, and a
// defect anywhere in `CreateCommit` would have left the whole suite green.
//
// That is not hypothetical. On 2026-08-11 the miner daemon could not anchor ANY commit whose payload
// began with "-": the CLI read it as a flag. Half the network's commits never reached the chain for
// weeks, and no test noticed, because no test went through the command a miner actually runs.
//
// ⚠️ WHAT THIS FILE DOES NOT COVER, and says so rather than imply otherwise: the audit LOTTERY
// (`runOptimisticAudit`) and the VRF seed. The committee is anchored explicitly here, exactly as the
// other ADR-032 tests do, because the draw has its own determinism tests. What is new here is
// everything AFTER the draw.

// e2eAddr builds a bech32 operator address. The seed must be 20 bytes: a shorter one silently
// produces a different address than the caller reads, and the operator check then fails for a reason
// that has nothing to do with the property under test.
func e2eAddr(t *testing.T, f *fixture, seed string) string {
	t.Helper()
	require.Len(t, seed, 20, "an AccAddress seed must be exactly 20 bytes")
	a, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(seed)))
	require.NoError(t, err)
	return a
}

// e2eStage builds the state a job is in when its audit has been opened and its jury seated: a
// primary that was paid optimistically, a HELD fee awaiting finality, and an ANCHORED committee.
// Returns the primary's operator address and the amount held.
func e2eStage(t *testing.T, f *fixture, jobId, primary string, jurors []string) (string, uint64) {
	t.Helper()
	const held = uint64(21_170) // the order of magnitude of a real held fee on the live network

	primOp := e2eAddr(t, f, "e2e_primary_op_"+jobId[len(jobId)-5:])
	require.NoError(t, gvSetMiner(f, f.ctx, primary, types.Miner{
		MinerId: primary, Operator: primOp, Stake: 1_000_000_000_000,
	}))
	for i, j := range jurors {
		require.NoError(t, gvSetMiner(f, f.ctx, j, types.Miner{
			MinerId: j, Operator: e2eAddr(t, f, "e2e_juror_op_"+string(rune('a'+i))+"_"+jobId[len(jobId)-5:]),
			Stake: 1,
		}))
	}
	// ADR-032: only the ANCHORED jury may weigh on the tally. Without this the tally fails closed and
	// the test would measure the fallback, not the verdict path.
	require.NoError(t, f.keeper.AuditCommittee.Set(f.ctx, jobId, joinIDs(jurors)))
	require.NoError(t, f.keeper.HeldFee.Set(f.ctx, jobId, held))
	require.NoError(t, gvSetJob(f, f.ctx, jobId, types.Job{
		JobId: jobId, State: "open+paid+optimistic+disputed", MinerId: primary,
		Disputer: e2eAddr(t, f, "e2e_disputer_ad_"+jobId[len(jobId)-4:]), DisputeBond: 0, DisputeHeight: 1,
	}))
	return primOp, held
}

func joinIDs(ids []string) string {
	out := ""
	for i, s := range ids {
		if i > 0 {
			out += ","
		}
		out += s
	}
	return out
}

// e2eVote posts a verdict THROUGH THE REAL MESSAGE, signed by the juror's operator.
func e2eVote(t *testing.T, f *fixture, srv types.MsgServer, jobId, juror, verdict string) error {
	t.Helper()
	m, err := f.keeper.Miner.Get(f.ctx, juror)
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator:      m.Operator,
		JobId:        jobId + "__verdict__" + juror,
		PromptCommit: verdict,
		ResultCommit: verdict,
		Kind:         "verdict",
	})
	return err
}

func e2eParams(t *testing.T, f *fixture) {
	t.Helper()
	p := types.DefaultParams()
	p.VerificationMode = 1
	p.DisputeWindow = 10
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(20) // past the dispute window
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(10_000_000)))
}

// AN HONEST PRIMARY IS VINDICATED BY SIGNED VERDICTS, AND ITS HELD FEE IS ACTUALLY PAID.
//
// The assertion that matters is the LAST one. A test stopping at "state contains resolved" would stay
// green on a chain that concludes every audit and pays nobody — which is, to the miner, the same
// outcome as no audit at all.
func TestE2EDrawToPay_SignedVerdictsVindicateAndReleaseTheHeldFee(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)

	jurors := []string{"jurA", "jurB", "jurC", "jurD"} // 4 = the participation floor
	primOp, held := e2eStage(t, f, "jE2Eok", "primeOk", jurors)
	// A KNOWN starting balance: the mock bank answers `richCoins()` for an address it has never seen,
	// so an unset balance makes any delta unreadable.
	primAcc, err := f.addressCodec.StringToBytes(primOp)
	require.NoError(t, err)
	f.bank.setBalance(primAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))

	for _, j := range jurors {
		require.NoError(t, e2eVote(t, f, srv, "jE2Eok", j, "1"),
			"a juror's operator must be able to post its verdict through CreateCommit")
	}

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{
		Creator: e2eAddr(t, f, "e2e_disputer_ad_E2Eok"[:20]), JobId: "jE2Eok",
	})
	require.NoError(t, err)

	job, err := f.keeper.Job.Get(f.ctx, "jE2Eok")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved")
	require.Len(t, job.SlashRecords, 0, "verdicts said valid: the primary must not be slashed")

	require.Equal(t, math.NewInt(int64(held)), f.bank.balOf(primAcc).AmountOf("udndr"),
		"THE POINT OF THE WHOLE PROTOCOL: a vindicated primary is PAID the fee that was held. "+
			"A resolved job that pays nothing is indistinguishable from no audit at all.")
}

// A CHEATING PRIMARY IS SLASHED BY SIGNED VERDICTS, AND ITS HELD FEE IS NOT PAID TO IT.
func TestE2EDrawToPay_SignedVerdictsSlashAndWithholdTheFee(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)

	jurors := []string{"jurE", "jurF", "jurG", "jurH"}
	primOp, _ := e2eStage(t, f, "jE2Eko", "primeKo", jurors)
	primAcc, err := f.addressCodec.StringToBytes(primOp)
	require.NoError(t, err)
	f.bank.setBalance(primAcc, sdk.NewCoins())
	require.NoError(t, f.keeper.Pools.Set(f.ctx, types.Pools{Treasury: 0}))
	before, err := f.keeper.Miner.Get(f.ctx, "primeKo")
	require.NoError(t, err)

	for _, j := range jurors {
		require.NoError(t, e2eVote(t, f, srv, "jE2Eko", j, "0"))
	}

	_, err = srv.AdjudicateDispute(f.ctx, &types.MsgAdjudicateDispute{
		Creator: e2eAddr(t, f, "e2e_disputer_ad_E2Eko"[:20]), JobId: "jE2Eko",
	})
	require.NoError(t, err)

	job, err := f.keeper.Job.Get(f.ctx, "jE2Eko")
	require.NoError(t, err)
	require.Contains(t, job.State, "resolved")
	require.Len(t, job.SlashRecords, 1, "a unanimous invalid verdict at the floor must slash the primary")

	after, err := f.keeper.Miner.Get(f.ctx, "primeKo")
	require.NoError(t, err)
	require.Less(t, after.Stake, before.Stake, "the slash must reach the stake, not only the job record")
	require.True(t, f.bank.balOf(primAcc).AmountOf("udndr").IsZero(),
		"a slashed primary must not receive the held fee")
}

// THE GATES A DIRECT `Commit.Set` SKIPS — the reason this file is not redundant with the others.
//
// Each case below is a door a real juror walks through and no other test opens. If any of them is
// broken, every existing verdict test stays green and the network stops judging.
func TestE2EDrawToPay_TheVerdictDoorIsGuarded(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)
	jurors := []string{"jurI", "jurJ", "jurK", "jurL"}
	_, _ = e2eStage(t, f, "jE2Egd", "primeGd", jurors)

	// ① H1 — SIGNATURE BINDING. Anyone able to post in another miner's name could manufacture a
	// verdict majority and slash an honest primary for the price of gas.
	stranger := e2eAddr(t, f, "e2e_stranger_addr_01")
	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "jE2Egd__verdict__jurI", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a stranger must not anchor a verdict in a juror's name")

	// ② IMMUTABILITY. Without it an operator watches the public verdicts, then rewrites its own to
	// join the majority and escape the slash — the tally becomes forgeable after the fact.
	require.NoError(t, e2eVote(t, f, srv, "jE2Egd", "jurI", "0"))
	require.Error(t, e2eVote(t, f, srv, "jE2Egd", "jurI", "1"),
		"an anchored verdict must be immutable: a second CreateCommit on the same key must fail")

	// ③ THE MINER MUST EXIST. A verdict key naming an unknown miner is not an empty vote, it is a
	// malformed one, and it must be refused rather than counted as an abstention.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "jE2Egd__verdict__ghost", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a verdict for an unregistered miner must be refused")

	// ④ THE KEY SHAPE. `LastIndex("__")` is what extracts the miner id; a key without the separator
	// would otherwise index the whole string and anchor a commit nobody can attribute.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "no-separator-here", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a commit key without \"__\" must be refused")
}

// JUDGING IS PRODUCTION: A SIGNED VERDICT MUST REFRESH THE JUROR'S VITALITY.
//
// `MinerLastCommitHeight` is what `eligibleAsJuror` reads. A juror that judges without its date being
// written ages out of the pool while doing exactly what the protocol asks of it — and the pool
// spirals towards death silently, since nothing reports a juror that stopped being drawable.
func TestE2EDrawToPay_JudgingRefreshesTheJurorVitality(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)
	jurors := []string{"jurM", "jurN", "jurO", "jurP"}
	_, _ = e2eStage(t, f, "jE2Evi", "primeVi", jurors)

	_, had := f.keeper.MinerLastCommitHeight.Get(f.ctx, "jurM")
	require.Error(t, had, "precondition: the juror has no commit date yet")

	require.NoError(t, e2eVote(t, f, srv, "jE2Evi", "jurM", "1"))

	h, err := f.keeper.MinerLastCommitHeight.Get(f.ctx, "jurM")
	require.NoError(t, err, "a juror that VOTED must have its vitality date written: judging is production")
	require.Equal(t, uint64(20), h)
}
