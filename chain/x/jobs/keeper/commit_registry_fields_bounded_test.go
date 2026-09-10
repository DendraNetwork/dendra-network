package keeper_test

import (
	"crypto/sha256"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// THE TWO MODEL-REGISTRY FIELDS ESCAPED THE COMMIT'S LENGTH BOUNDS.
//
// `CreateCommit` bounds `result_commit`, `prompt_commit` and `kind`, and its own comment says why:
// without them "a single transaction could carry roughly a mebibyte of permanent state". `model_id` and
// `weights_hash` are written by the same store call, three lines below that list, and carried no bound.
//
// MEASURED before the bounds existed: a commit carrying 1 MiB in EACH field was ACCEPTED and stored —
// 2 MiB of permanent state from one transaction. And it cannot be reclaimed: `DeleteCommit` refuses by
// design ("IMMUTABLE commit (H1): deletion is forbidden (tally evasion)"), and the entry was still there
// afterwards. On a chain whose blocks carry no gas limit and whose validators run at a zero minimum gas
// price, the cost of writing it is the cost of a transaction.
//
// The bounds are DERIVED where the chain declares something, and honest where it does not:
//   - `weights_hash` exists to be compared with the registry's `weights_sha256`. A SHA-256 in hex is
//     exactly `2 * sha256.Size`; longer can match no anchor this chain stores.
//   - `model_id` has no declared bound ANYWHERE — `x/modelregistry` bounds neither its ids nor its
//     digests. So the number is anchored on the longest id the shipped configuration names
//     (`qwen3:30b-a3b-instruct-2507-q4_K_M`, 34 bytes) with room for the same family, and the comment
//     says plainly that this is the only bound in the system: a model registered with a longer id would
//     become uncommittable, which is a reason for the registry to grow its own.

func crfSetup(t *testing.T, f *fixture, jobID string) (string, string) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	srv := keeper.NewMsgServerImpl(f.keeper)
	op := ssAddr20(t, f, "crf-op-"+jobID)
	id := deriveIDFor(t, f, op)
	_, err := srv.CreateMiner(f.ctx, &types.MsgCreateMiner{
		Creator: op, Operator: op, MinerId: id, Stake: 1000,
	})
	require.NoError(t, err)
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{JobId: jobID, State: "open", Fee: 10000}))
	return op, id
}

// (a) THE REFUSAL, on each field separately so a single bound cannot mask the other's absence.
func TestACommitCannotParkUnboundedBytesInTheRegistryFields(t *testing.T) {
	for nom, mut := range map[string]func(*types.MsgCreateCommit){
		"model_id": func(m *types.MsgCreateCommit) { m.ModelId = strings.Repeat("A", 1<<20) },
		"weights_hash": func(m *types.MsgCreateCommit) {
			m.WeightsHash = strings.Repeat("a", 1<<20)
		},
	} {
		t.Run(nom, func(t *testing.T) {
			f := initFixture(t)
			op, id := crfSetup(t, f, "jCRF")
			srv := keeper.NewMsgServerImpl(f.keeper)

			msg := &types.MsgCreateCommit{Creator: op, JobId: "jCRF__" + id, ResultCommit: "1,2,3"}
			mut(msg)
			_, err := srv.CreateCommit(f.ctx, msg)
			require.Error(t, err, "%s: a mebibyte of permanent state must not enter for the price of a tx", nom)
			require.Contains(t, err.Error(), "too long")

			// NOTHING was written: the refusal has to land before the store, or the bound only changes the
			// error message while the bytes stay.
			has, hErr := f.keeper.Commit.Has(f.ctx, "jCRF__"+id)
			require.NoError(t, hErr)
			require.False(t, has, "%s: the refusal must precede the write -- the entry is undeletable", nom)
		})
	}
}

// (b) THE CONTROL, and the reason (a) means anything: a real commit still passes. The values used here
// are the ones the shipped configuration actually carries, so a bound that refused them would have been
// caught by this case rather than by an operator.
func TestARealisticCommitStillPasses(t *testing.T) {
	f := initFixture(t)
	op, id := crfSetup(t, f, "jCRFb")
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jCRFb__" + id, ResultCommit: "1,2,3",
		ModelId:     "qwen3:30b-a3b-instruct-2507-q4_K_M",
		WeightsHash: strings.Repeat("ab", sha256.Size), // 2 chars x 32 bytes = the hex length of a SHA-256
	})
	require.NoError(t, err, "the longest model id the shipped config names must still be committable")

	c, err := f.keeper.Commit.Get(f.ctx, "jCRFb__"+id)
	require.NoError(t, err)
	require.Equal(t, "qwen3:30b-a3b-instruct-2507-q4_K_M", c.ModelId)
	require.Len(t, c.WeightsHash, 2*sha256.Size, "a hex SHA-256 is exactly this long, and it fits")
}

// (c) THE BOUNDARY ITSELF, so nobody later "tidies" it to a round number that is off by one: a hash of
// exactly the hex length passes, one character more does not.
func TestTheWeightsHashBoundIsTheHexLengthOfASha256(t *testing.T) {
	f := initFixture(t)
	op, id := crfSetup(t, f, "jCRFc")
	srv := keeper.NewMsgServerImpl(f.keeper)

	_, err := srv.CreateCommit(f.ctx, &types.MsgCreateCommit{
		Creator: op, JobId: "jCRFc__" + id, ResultCommit: "1,2,3",
		WeightsHash: strings.Repeat("f", 2*sha256.Size),
	})
	require.NoError(t, err, "exactly one hex SHA-256 is accepted")

	f2 := initFixture(t)
	op2, id2 := crfSetup(t, f2, "jCRFd")
	srv2 := keeper.NewMsgServerImpl(f2.keeper)
	_, err = srv2.CreateCommit(f2.ctx, &types.MsgCreateCommit{
		Creator: op2, JobId: "jCRFd__" + id2, ResultCommit: "1,2,3",
		WeightsHash: strings.Repeat("f", 2*sha256.Size+1),
	})
	require.Error(t, err, "one character more can match no anchor this chain stores")
}
