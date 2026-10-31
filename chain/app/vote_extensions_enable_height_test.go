package app

import (
	"testing"

	abci "github.com/cometbft/cometbft/abci/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// At the enable height itself, validation is skipped and consumption is not.
//
// Two predicates decide what a block may do with vote extensions, and they differ by one height on
// purpose:
//
//	voteExtensionsActive(h)    = h >  enable_height   -- may this block consume extensions
//	voteExtensionsRecording(h) = h >= enable_height   -- must this block record its own hash
//
// The width is right for the hash: at E, CometBFT already calls ExtendVote and signs an alpha bound to
// the block hash of E, which the aggregation at E+1 reads back (see `voteExtensionsRecording`).
//
// It was wrong for the envelope. `newProcessProposalHandler` starts with
// `if !voteExtensionsActive(...) { return next(ctx, req) }`, so at H == E the whole block, envelope
// included, goes to `next`, and `next` is `NoOpProcessProposal`: the default proposal handler is built
// with `mempool.NoOpMempool{}`, and in cosmos-sdk v0.53.6 (`baseapp/abci_utils.go`,
// `NoOpProcessProposal`) it accepts unconditionally for a NoOp mempool. Neither
// `ValidateVoteExtensions` nor the LastCommit cross-check runs there. The PreBlocker, gated on the
// wider predicate, then aggregated that unverified envelope and wrote the seed, the contributor count
// and the contributor power, and `aggregateSeed` derives the 2/3 power floor from powers read from the
// envelope itself, so the floor was met by construction.
//
// That seed freezes committees, so it decides who is paid. A proposer holding the enable-height slot
// could compute the draw offline and choose whether to inject. A rescue procedure that sets
// `enable_height = initial_height + 1` lands on exactly that block, where the restored backlog of
// pending reveals and audits comes due.
//
// The fix gates consumption on `voteExtensionsActive` and leaves the hash write wide. It costs
// nothing: CometBFT delivers extensions only from E+1, so at E only a forged envelope can exist.
//
// These cases drive the two predicates directly. Driving the PreBlocker end to end would need a full
// app with a store, which this package's other cases avoid; what is asserted here is the relation the
// defect lived in, plus the delegation behaviour that hid it.

func TestTheTwoGatesDifferByExactlyOneHeightAndOnlyAtTheEnableHeight(t *testing.T) {
	const E = int64(7)

	// At the enable height: recording yes, consuming no. That gap is the whole surface of the defect.
	require.True(t, voteExtensionsRecording(veContext(E), E),
		"the block hash of E must be written: the aggregation at E+1 reads it back")
	require.False(t, voteExtensionsActive(veContext(E), E),
		"CometBFT delivers no extension at E, so nothing may be consumed there")

	// Below: neither.
	require.False(t, voteExtensionsRecording(veContext(E), E-1))
	require.False(t, voteExtensionsActive(veContext(E), E-1))

	// Above: both. The gap is one height wide and no wider, otherwise the fix below would silently
	// disable the feature instead of delaying it by a block.
	for _, h := range []int64{E + 1, E + 2, E + 100} {
		require.True(t, voteExtensionsRecording(veContext(E), h), "recording at %d", h)
		require.True(t, voteExtensionsActive(veContext(E), h), "consuming at %d", h)
	}

	// Dormant (enable_height = 0): neither predicate fires, at any height.
	require.False(t, voteExtensionsRecording(veContext(0), E))
	require.False(t, voteExtensionsActive(veContext(0), E))
}

// At the enable height, the proposal handler delegates the envelope without validating it, and the
// real `next` in production accepts unconditionally. This case pins the delegation; the spy stands
// in for `next`, and the comment above says what the production `next` is.
func TestAtTheEnableHeightTheEnvelopeIsDelegatedUnvalidated(t *testing.T) {
	const E = int64(7)
	spy := &nextSpy{}
	called := false
	h := newProcessProposalHandler(spy.handler,
		func(_ sdk.Context, _ abci.ExtendedCommitInfo, _ int64) error {
			called = true
			return nil
		})

	req := &abci.RequestProcessProposal{Height: E, Txs: [][]byte{veEnvelope(t), []byte("tx-1")}}
	resp, err := h(veContext(E), req)
	require.NoError(t, err)
	require.Equal(t, abci.ResponseProcessProposal_ACCEPT, resp.Status)

	require.False(t, called,
		"at the enable height the extensions are never validated: the handler returns before that")
	require.True(t, spy.called, "the whole block is handed to next...")
	require.Len(t, spy.txsSeen, 2,
		"...including the envelope, which next has no reason to inspect -- and in production next is an "+
			"unconditional accept")

	// One height later the same envelope is validated: the gap is one height, not a mode.
	called = false
	req2 := &abci.RequestProcessProposal{Height: E + 1, Txs: [][]byte{veEnvelope(t), []byte("tx-1")}}
	_, err = h(veContext(E), req2)
	require.NoError(t, err)
	require.True(t, called, "from E+1 the envelope goes through the validation the enable height skips")
}

// This drives the decision the PreBlocker calls. Kept inline in a closure, the condition could lose its
// `voteExtensionsActive` term with the whole module still green, so `preBlockerMayAggregate` is a named
// function invoked by the shipped PreBlocker, and driving it here drives the line production
// runs.
func TestThePreBlockerRefusesToAggregateAtTheEnableHeight(t *testing.T) {
	const E = int64(7)
	env := [][]byte{veEnvelope(t), []byte("tx-1")}

	require.False(t, preBlockerMayAggregate(veContext(E), E, env),
		"at the enable height nothing validated this envelope, so nothing may be read from it")

	// One height later the same envelope is aggregated: the refusal delays by a block and does not
	// disable the feature. Without this half, a permanent shutdown would also pass as a fix.
	require.True(t, preBlockerMayAggregate(veContext(E), E+1, env),
		"from E+1 ProcessProposal has validated the envelope, so the PreBlocker may consume it")
	require.True(t, preBlockerMayAggregate(veContext(E), E+100, env))

	// The other two terms still hold: no envelope, no aggregation.
	require.False(t, preBlockerMayAggregate(veContext(E), E+1, nil), "no transaction at all")
	require.False(t, preBlockerMayAggregate(veContext(E), E+1, [][]byte{[]byte("tx-1")}),
		"a block whose first transaction is not an envelope")

	// Dormant: never.
	require.False(t, preBlockerMayAggregate(veContext(0), E+1, env))
}
