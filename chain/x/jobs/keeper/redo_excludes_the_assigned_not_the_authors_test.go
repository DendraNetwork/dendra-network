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

// THE ANTI-COLLUSION EXCLUSION REMOVES WHO WAS ASSIGNED, AND WHAT NEEDS REMOVING IS WHO ANSWERED.
//
// `anchorRedoCommittee` builds the re-adjudication jury from every registered miner except the primary,
// the disputer and `orig` — and `orig` is `assignedCommittee(jobId, CommitteeSize)`, the drawn seats.
// `AdjudicateDispute` applies the same `orig` again when counting votes.
//
// But being ASSIGNED is not the same as having WORKED, and the repository says so where it matters:
// "CreateCommit on its own requires neither that the miner be assigned to the job nor that it was DRAWN
// onto the committee" (msg_server_commit.go). So the set that produced the commits under dispute is
// `{mid : Commit[jobId__mid] exists}` — which is what the adjudication actually re-reads — and it is
// NOT `assignedCommittee(jobId, 3)`. Two different sets, not one set at two dates.
//
// MEASURED: ten registered miners, the drawn committee [co3 co2 co9], and `co0` — assigned to nothing —
// anchors a commit on the job because nothing forbids it. The anchored redo jury comes out as
// "co7,co5,co6,co8,co4,co1,co0". A producer of the contested result sits on the jury that re-judges it,
// and `orig` does not exclude it at adjudication either, because it never was in `orig`.
//
// ⛔ NOT A COROLLARY OF THE LIVE-WEIGHT DRAW. Freezing the ordering weight makes `orig` stable across
// time; it does not make `orig` equal to the set of authors. No freeze reconciles two sets built from
// different questions.
//
// ⛔ PINNED, NOT FIXED. Excluding the authors means the exclusion must read `Commit` keys rather than
// the draw — cheap to write, and it changes who may judge, so it is a governance-visible rule and a
// consensus change. It also interacts with the quorum: `redoSeats` counts ANCHORED seats while the vote
// filter uses a set computed later, so widening the exclusion shrinks the numerator without touching
// the denominator. That trade belongs to the owner.

const rxHeight = int64(300)

func rxSetup(t *testing.T, f *fixture, jobID string) ([]string, string, []string) {
	t.Helper()
	require.NoError(t, f.keeper.Params.Set(f.ctx, types.DefaultParams()))
	// The redo draw's fallback seed is the BLOCK HASH, not the AppHash: without it the seed is empty and
	// the anchoring refuses, fail-closed, writing nothing. Two fixtures died there before this line.
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

	assignes, err := f.keeper.AssignedCommitteeOrderedForTest(f.ctx, jobID, keeper.CommitteeSize)
	require.NoError(t, err)
	require.Len(t, assignes, keeper.CommitteeSize)

	// An author that was never assigned. `CreateCommit` requires no assignment, so this is an ordinary
	// state, not a fabricated one.
	auteurNonAssigne := ""
	for _, id := range ids {
		if id != assignes[0] && id != assignes[1] && id != assignes[2] {
			auteurNonAssigne = id
			break
		}
	}
	require.NotEmpty(t, auteurNonAssigne)
	for _, id := range append(append([]string{}, assignes...), auteurNonAssigne) {
		require.NoError(t, f.keeper.Commit.Set(f.ctx, jobID+"__"+id, types.Commit{ResultCommit: "1,2,3"}))
	}
	return assignes, auteurNonAssigne, ids
}

func rxAnchored(t *testing.T, f *fixture, jobID string) []string {
	t.Helper()
	membres, err := f.keeper.AuditCommittee.Get(f.ctx, keeper.RedoAnchorKeyForTest(jobID))
	require.NoError(t, err, "the anchoring must have happened, or this case measures a missing seed")
	return strings.Split(membres, ",")
}

// (a) THE DEFECT: an author of the disputed result is seated on the jury that re-judges it.
func TestAnAuthorOfTheDisputedResultCannotSitOnTheRedoJury(t *testing.T) {
	f := initFixture(t)
	assignes, auteur, _ := rxSetup(t, f, "jRX")

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(f.ctx, "jRX", assignes[0], ""))
	jury := rxAnchored(t, f, "jRX")

	require.NotContains(t, jury, auteur,
		"the author %q produced a commit on this job, so it may not judge it -- the exclusion is the set of AUTHORS, not of the drawn seats", auteur)

	// The premise, restated as an assertion so the test cannot pass by the author having vanished for
	// some unrelated reason: it IS an author, and it is NOT one of the drawn.
	has, err := f.keeper.Commit.Has(f.ctx, "jRX__"+auteur)
	require.NoError(t, err)
	require.True(t, has, "premise: this miner really did anchor a commit on the disputed job")

	orig, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jRX", keeper.CommitteeSize)
	require.NoError(t, err)
	require.False(t, orig[auteur],
		"premise: and it was never DRAWN -- which is exactly why the old exclusion could not see it")

	// And the jury is not empty: an exclusion that swept everyone would pass the assertion above while
	// making every dispute unresolvable.
	require.NotEmpty(t, jury, "the redo jury must still be seated once the authors are removed")
}

// (b) THE CONTROL, and the reason (a) means anything: the exclusion DOES work for what it names. Every
// drawn member is kept out of the redo jury, so the guard is not simply inert.
func TestTheDrawnCommitteeIsProperlyExcludedFromTheRedoJury(t *testing.T) {
	f := initFixture(t)
	assignes, _, _ := rxSetup(t, f, "jRXb")

	require.NoError(t, f.keeper.AnchorRedoCommitteeForTest(f.ctx, "jRXb", assignes[0], ""))
	jury := rxAnchored(t, f, "jRXb")

	for _, m := range assignes {
		require.NotContains(t, jury, m,
			"a drawn member must never re-judge its own committee -- that half of the rule holds")
	}
}

// (c) THE TWO SETS, NAMED. What the exclusion uses, and what the adjudication re-reads, are different
// sets — and this case measures the gap rather than arguing about it.
func TestTheAuthorsAndTheAssignedAreNotTheSameSet(t *testing.T) {
	f := initFixture(t)
	assignes, auteur, _ := rxSetup(t, f, "jRXc")

	orig, err := f.keeper.AssignedCommitteeForTest(f.ctx, "jRXc", keeper.CommitteeSize)
	require.NoError(t, err)

	auteurs := map[string]bool{}
	require.NoError(t, f.keeper.Commit.Walk(f.ctx, keeper.CommitRangeForTest("jRXc__"),
		func(key string, _ types.Commit) (bool, error) {
			if i := strings.LastIndex(key, "__"); i >= 0 {
				auteurs[key[i+2:]] = true
			}
			return false, nil
		}))

	require.Len(t, auteurs, len(assignes)+1, "one more author than there are seats")
	require.True(t, auteurs[auteur], "the extra one really produced a commit")
	require.False(t, orig[auteur], "and the set the exclusion uses does not contain it")
}
