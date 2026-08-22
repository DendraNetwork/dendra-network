package keeper_test

import (
	"strings"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ── THE GHOST COMMIT, PROVEN ON THE SHIPPED HANDLER ──────────────────────────────────────────────
//
// `CreateCommit` called neither `Job.Get` nor `Job.Has`, and no `ValidateBasic` exists in this
// module. A REGISTERED miner therefore wrote a PERMANENT entry (`UpdateCommit`/`DeleteCommit` are
// neutralised, and overwriting is refused) under a job id no job had ever opened, with no size bound
// — the only ceiling being CometBFT's default `MaxTxBytes`.
//
// This bench drives `msgServer.CreateCommit`, not a copy of its decision: the key derivation has its
// own bench in `x/jobs/types`; this one proves what the HANDLER accepts and refuses.
//
// ⛔ WHAT THIS BENCH DOES NOT PROVE, SAID RATHER THAN LEFT SILENT.
//
// The fail-closed branch on `jerr != nil` — a store read that FAILS refuses instead of authorising —
// is exercised by NO case here. `collections.Map.Has` returns `(false, nil)` for a missing key:
// forcing a real error would need a broken store, which this harness cannot fabricate (the
// neighbouring technique, `Params.Remove`, works because a missing `Item` makes its `Get` fail — a
// Map `Has` does not).
//
// Measured: removing that branch leaves this bench GREEN. It is written anyway, and not out of zeal:
// `known, _ := k.Job.Has(...)` would silently authorise the day that read can fail, which is exactly
// the shape — "ignorance becomes authorisation, always on the reassuring side" — that
// `params_fail_closed_test.go` exists to forbid elsewhere.

// prepareMiner installs a miner and returns (operator, minerId).
func prepareMiner(t *testing.T, f *fixture) (string, string) {
	t.Helper()
	op, err := f.addressCodec.BytesToString([]byte("ghostOperator_______________"))
	require.NoError(t, err)
	id := deriveIDFor(t, f, op)
	srv := keeper.NewMsgServerImpl(f.keeper)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{
		Creator: op, MinerId: id, Operator: op, Stake: 1000})
	require.NoError(t, err)
	return op, id
}

// THE DEFECT ITSELF: a key whose job was never opened.
func TestGhostCommit_RefusesAJobNeverOpened(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "madeUpJob__" + id, ResultCommit: "r"})
	require.Error(t, err, "a commit under a job id never opened must be REFUSED")
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)

	// And nothing may remain in the store: a refusal that writes anyway refused nothing.
	has, herr := f.keeper.Commit.Has(f.ctx, "madeUpJob__"+id)
	require.NoError(t, herr)
	require.False(t, has, "the refused commit was written all the same")
}

// Same miner, same gesture, once the job exists: ACCEPTED. Without this case the refusal above would
// be compatible with a guard that refuses everything.
func TestGhostCommit_AcceptsAnOpenedJob(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)

	require.NoError(t, f.keeper.Job.Set(f.ctx, "realJob", types.Job{JobId: "realJob"}))
	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "realJob__" + id, ResultCommit: "r"})
	require.NoError(t, err, "a commit on an opened job must pass")
}

// ⛔ THE CASE THAT WOULD HAVE CUT THE AUDIT LAYER. The epoch marker `e<N>__reveals__<miner>` is
// anchored by the reveal worker, armed by default, and names NO job. A guard requiring a job for it
// would refuse it at every epoch, on every judge node.
func TestGhostCommit_EpochMarkerAcceptedWithoutJob(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "e7__reveals__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.NoError(t, err, "the epoch marker names no job and must be ACCEPTED")
}

// ⛔ AND WE DO NOT ROUTE ON `kind`. The field comes from the sender and no on-chain decision reads
// it: if the exemption hung on it, writing it would be enough to bypass the whole guard.
func TestGhostCommit_KindGrantsNoExemption(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "madeUpJob__" + id, ResultCommit: "r", Kind: "revealmark"})
	require.Error(t, err, "`kind` must exempt nothing: it is the KEY that says what it is")
	require.ErrorIs(t, err, sdkerrors.ErrKeyNotFound)
}

// PADDING. The job id exists, but the key slips in one more segment: without this refusal a miner
// opens an unbounded number of permanent keys inside the `<jobId>__` range the settlement handlers
// walk.
func TestGhostCommit_RefusesPadding(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "realJob", types.Job{JobId: "realJob"}))

	for _, k := range []string{"realJob__x1__" + id, "realJob__a__b__" + id} {
		_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: k, ResultCommit: "r"})
		require.Error(t, err, "padded key accepted: %s", k)
		require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	}
}

// A verdict on an opened job stays ACCEPTED: the guard must not close the audit layer.
func TestGhostCommit_VerdictAndRedoStillPossible(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "realJob", types.Job{JobId: "realJob"}))

	for _, k := range []string{"realJob__verdict__" + id, "realJob__redo__" + id, "realJob__redo__verdict__" + id} {
		_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: k, ResultCommit: "r"})
		require.NoError(t, err, "legitimate role key refused: %s", k)
	}
}

// THE BOUNDS. They are DERIVED from what the chain already accepts to read back: at most
// `embedMaxDim` signed integers of magnitude `embedMaxMag`, comma-separated. The case AT the exact
// bound is what tells a bound apart from an arbitrary refusal.
func TestGhostCommit_LengthBounds(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, id := prepareMiner(t, f)
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jA", types.Job{JobId: "jA"}))
	require.NoError(t, f.keeper.Job.Set(f.ctx, "jB", types.Job{JobId: "jB"}))

	// Recomputed here rather than imported, so the bench does not depend on an unexported constant:
	// if the two ever diverge, this case says so.
	const bound = 384*len("-1048576") + 383

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jA__" + id, ResultCommit: strings.Repeat("9", bound)})
	require.NoError(t, err, "the value AT the bound must pass")

	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jB__" + id, ResultCommit: strings.Repeat("9", bound+1)})
	require.Error(t, err, "one byte over the bound must be refused")
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jB__" + id, ResultCommit: "r", Kind: strings.Repeat("k", 33)})
	require.Error(t, err, "an unbounded `kind` is a place to park bytes again")
}
