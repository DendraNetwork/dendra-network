package keeper_test

import (
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// Draw, signed verdict, conclusion, payment: through the entry points a real judge uses.
//
// Every other test in this package writes verdicts straight into state:
//
//	f.keeper.Commit.Set(ctx, "jv__verdict__mD", types.Commit{ResultCommit: "0"})
//
// A real juror cannot do that. It sends `MsgCreateCommit`, and that message carries four gates the
// direct write skips: the signer must be the miner's operator (H1), the miner id is extracted from the
// key rather than supplied, an anchored commit is immutable, and the model registry is consulted fail
// closed. The path that justifies this project (a committee is drawn, it judges, the chain concludes,
// the money moves) was therefore covered end to end by nothing, and a defect anywhere in
// `CreateCommit` would have left the whole suite green.
//
// This happened once: the miner daemon could not anchor any commit whose payload began with "-",
// because the CLI read it as a flag. Half the network's commits never reached the chain for weeks, and
// no test noticed, because no test went through the command a miner actually runs.
//
// This file does not cover the audit lottery (`runOptimisticAudit`) or the VRF seed. The committee is
// anchored explicitly here, as the other ADR-032 tests do, because the draw has its own determinism
// tests. What is new here is everything after the draw.

// e2eAddr builds a bech32 operator address. The seed must be 20 bytes: a shorter one silently produces
// a different address than the caller reads, and the operator check then fails for a reason that has
// nothing to do with the property under test.
func e2eAddr(t *testing.T, f *fixture, seed string) string {
	t.Helper()
	require.Len(t, seed, 20, "an AccAddress seed must be exactly 20 bytes")
	a, err := f.addressCodec.BytesToString(sdk.AccAddress([]byte(seed)))
	require.NoError(t, err)
	return a
}

// e2eStage builds the state a job is in when its audit has been opened and its jury seated: a primary
// paid optimistically, a held fee awaiting finality, and an anchored committee. It returns the
// primary's operator address and the amount held.
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
	// ADR-032: only the anchored jury may weigh on the tally. Without this the tally fails closed and
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

// e2eVote posts a verdict through the real message, signed by the juror's operator.
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

// An honest primary is vindicated by signed verdicts, and its held fee is actually paid.
//
// The last assertion is the one that matters. A test stopping at "state contains resolved" would stay
// green on a chain that concludes every audit and pays nobody, which for the miner is the same outcome
// as no audit at all.
func TestE2EDrawToPay_SignedVerdictsVindicateAndReleaseTheHeldFee(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)

	jurors := []string{"jurA", "jurB", "jurC", "jurD"} // 4 = the participation floor
	primOp, held := e2eStage(t, f, "jE2Eok", "primeOk", jurors)
	// A known starting balance: the mock bank answers `richCoins()` for an address it has never seen,
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
		"The point of the whole protocol: a vindicated primary is paid the fee that was held. "+
			"A resolved job that pays nothing is indistinguishable from no audit at all.")
}

// A cheating primary is slashed by signed verdicts, and its held fee is not paid to it.
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

// The gates a direct `Commit.Set` skips, which is why this file is not redundant with the others.
//
// Each case below is an entry point a real juror goes through and no other test exercises. If any of
// them broke, every existing verdict test would stay green and the network would stop judging.
func TestE2EDrawToPay_TheVerdictDoorIsGuarded(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	e2eParams(t, f)
	jurors := []string{"jurI", "jurJ", "jurK", "jurL"}
	_, _ = e2eStage(t, f, "jE2Egd", "primeGd", jurors)

	// (1) H1, signature binding. Anyone able to post in another miner's name could manufacture a
	// verdict majority and slash an honest primary for the price of gas.
	stranger := e2eAddr(t, f, "e2e_stranger_addr_01")
	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "jE2Egd__verdict__jurI", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a stranger must not anchor a verdict in a juror's name")

	// (2) Immutability. Without it an operator watches the public verdicts, then rewrites its own to
	// join the majority and escape the slash, so the tally could be forged after the fact.
	require.NoError(t, e2eVote(t, f, srv, "jE2Egd", "jurI", "0"))
	require.Error(t, e2eVote(t, f, srv, "jE2Egd", "jurI", "1"),
		"an anchored verdict must be immutable: a second CreateCommit on the same key must fail")

	// (3) The miner must exist. A verdict key naming an unknown miner is a malformed vote, not an empty
	// one, and it must be refused rather than counted as an abstention.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "jE2Egd__verdict__ghost", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a verdict for an unregistered miner must be refused")

	// (4) The key shape. `LastIndex("__")` extracts the miner id; a key without the separator would
	// otherwise index the whole string and anchor a commit nobody can attribute.
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: stranger, JobId: "no-separator-here", ResultCommit: "0", Kind: "verdict",
	})
	require.Error(t, err, "a commit key without \"__\" must be refused")
}

// Judging is production: a signed verdict must refresh the juror's vitality.
//
// `MinerLastCommitHeight` is what `eligibleAsJuror` reads. A juror that judges without its date being
// written ages out of the pool while doing what the protocol asks of it, and the pool shrinks
// silently, since nothing reports a juror that stopped being drawable.
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
	require.NoError(t, err, "a juror that voted must have its vitality date written: judging is production")
	require.Equal(t, uint64(20), h)
}
