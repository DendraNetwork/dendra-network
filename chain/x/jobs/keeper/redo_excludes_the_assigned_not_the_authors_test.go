package keeper_test

import (
	"fmt"
	"strings"
	"testing"

	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The anti-collusion exclusion must remove who answered, not only who was assigned.
//
// `anchorRedoCommittee` builds the re-adjudication jury from every registered miner except the primary,
// the disputer and `orig`, and `orig` is `assignedCommittee(jobId, CommitteeSize)`, the drawn seats.
// `AdjudicateDispute` applies the same `orig` again when counting votes.
//
// Being assigned is not the same as having worked: "CreateCommit on its own requires neither that the
// miner be assigned to the job nor that it was drawn onto the committee" (msg_server_commit.go). The
// set that produced the commits under dispute is `{mid : Commit[jobId__mid] exists}`, which is what the
// adjudication actually reads, and it differs from `assignedCommittee(jobId, 3)`: two different sets,
// not one set at two dates.
//
// Measured: ten registered miners, the drawn committee [co3 co2 co9], and `co0`, assigned to nothing,
// anchors a commit on the job because nothing forbids it. The anchored redo jury came out as
// "co7,co5,co6,co8,co4,co1,co0": a producer of the contested result sat on the jury that judged it
// again, and `orig` did not exclude it at adjudication either, because it never was in `orig`.
//
// This does not follow from the live-weight draw: freezing the ordering weight makes `orig` stable
// over time, but not equal to the set of authors.
//
// `commitAuthorsOf` now adds the authors to the exclusion, which changes who may judge (a consensus
// change). It also interacts with the quorum: excluded seats leave both sides of the ratio, so
// widening the exclusion cannot raise the bar for the remaining jurors.

const rxHeight = int64(300)

func rxSetup(t *testing.T, f *fixture, jobID string) ([]string, string, []string) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	// The redo draw's fallback seed is the block hash, not the AppHash: without it the seed is empty and
	// the anchoring refuses, fail closed, writing nothing.
	f.ctx = sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(rxHeight).
		WithHeaderInfo(header.Info{Height: rxHeight, AppHash: []byte("apphash-rx")})
	require.NoError(t, f.keeper.SetBlockHash(f.ctx, rxHeight, []byte("block-hash-rx")))

	ids := []string{}
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("rx%d", i)
		ids = append(ids, id)
		op := ssAddr20(t, f, "rx-op-"+id+"-"+jobID)
		require.NoError(t, gvSetMiner(f, f.ctx, id, types.Miner{
			MinerId: id, Creator: op, Operator: op, Stake: uint64(1000 + i*211),
		}))
	}
	require.NoError(t, gvSetJob(f, f.ctx, jobID, types.Job{
		JobId: jobID, State: "open+paid", Fee: 10000,
	}))

	assigned, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, jobID, keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, assigned, keeper.CommitteeSize)

	// An author that was never assigned. `CreateCommit` requires no assignment, so this is an ordinary
	// state, not a fabricated one.
	unassignedAuthor := ""
	for _, id := range ids {
		if id != assigned[0] && id != assigned[1] && id != assigned[2] {
			unassignedAuthor = id
			break
		}
	}
	require.NotEmpty(t, unassignedAuthor)
	for _, id := range append(append([]string{}, assigned...), unassignedAuthor) {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: "1,2,3"}))
	}
	return assigned, unassignedAuthor, ids
}

func rxAnchored(t *testing.T, f *fixture, jobID string) []string {
	t.Helper()
	members, err := f.keeper.AuditCommittee.Get(f.ctx, keeper.RedoAnchorKeyForTest(jobID))
	require.NoError(t, err, "the anchoring must have happened, or this case measures a missing seed")
	return strings.Split(members, ",")
}

// (a) An author of the disputed result is not seated on the jury that judges it again.
func TestAnAuthorOfTheDisputedResultCannotSitOnTheRedoJury(t *testing.T) {
	f := initFixture(t)
	assigned, author, _ := rxSetup(t, f, "jRX")

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(f.ctx, "jRX", assigned[0], ""))
	jury := rxAnchored(t, f, "jRX")

	require.NotContains(t, jury, author,
		"the author %q produced a commit on this job, so it may not judge it -- the exclusion is the set of authors, not of the drawn seats", author)

	// The premise, restated as an assertion so the test cannot pass because the author vanished for some
	// unrelated reason: it is an author, and it is not one of the drawn.
	has, err := f.keeper.Commit.Has(f.ctx, "jRX__"+author)
	require.NoError(t, err)
	require.True(t, has, "premise: this miner really did anchor a commit on the disputed job")

	orig, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jRX", keeper.CommitteeSize)
	require.NoError(t, err)
	require.False(t, orig[author],
		"premise: and it was never drawn -- which is exactly why the old exclusion could not see it")

	// And the jury is not empty: an exclusion that swept everyone would pass the assertion above while
	// making every dispute unresolvable.
	require.NotEmpty(t, jury, "the redo jury must still be seated once the authors are removed")
}

// (b) The control, which gives (a) its meaning: the exclusion works for what it names. Every drawn
// member is kept out of the redo jury, so the guard is not simply inert.
func TestTheDrawnCommitteeIsProperlyExcludedFromTheRedoJury(t *testing.T) {
	f := initFixture(t)
	assigned, _, _ := rxSetup(t, f, "jRXb")

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(f.ctx, "jRXb", assigned[0], ""))
	jury := rxAnchored(t, f, "jRXb")

	for _, m := range assigned {
		require.NotContains(t, jury, m,
			"a drawn member must never re-judge its own committee -- that half of the rule holds")
	}
}

// (c) The two sets, named: what the exclusion uses and what the adjudication reads are different sets,
// and this case measures the gap.
func TestTheAuthorsAndTheAssignedAreNotTheSameSet(t *testing.T) {
	f := initFixture(t)
	assigned, author, _ := rxSetup(t, f, "jRXc")

	orig, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jRXc", keeper.CommitteeSize)
	require.NoError(t, err)

	authors := map[string]bool{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("jRXc__"),
		func(key string, _ types.Commit) (bool, error) {
			if i := strings.LastIndex(key, "__"); i >= 0 {
				authors[key[i+2:]] = true
			}
			return false, nil
		}))

	require.Len(t, authors, len(assigned)+1, "one more author than there are seats")
	require.True(t, authors[author], "the extra one really produced a commit")
	require.False(t, orig[author], "and the set the exclusion uses does not contain it")
}
