package app

import (
	"testing"

	abci "github.com/cometbft/cometbft/abci/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// AT THE ENABLE HEIGHT ITSELF, VALIDATION IS SKIPPED AND CONSUMPTION IS NOT.
//
// Two predicates decide what a block may do with vote extensions, and they differ by one height on
// purpose:
//
//	voteExtensionsActive(h)    = h >  enable_height   -- may this block CONSUME extensions
//	voteExtensionsRecording(h) = h >= enable_height   -- must this block RECORD its own hash
//
// The width is right for the hash: at E, CometBFT already calls ExtendVote and signs an alpha bound to
// the block hash of E, which the aggregation at E+1 reads back. `voteExtensionsRecording` documents
// that in fifteen lines.
//
// It was wrong for the ENVELOPE. `newProcessProposalHandler` starts with
// `if !voteExtensionsActive(...) { return next(ctx, req) }`, so at H == E the whole block — envelope
// included — goes to `next`; and `next` is `NoOpProcessProposal`, because the default proposal handler
// is built with `mempool.NoOpMempool{}` and cosmos-sdk v0.53.6 (`baseapp/abci_utils.go`, `NoOpProcessProposal`)
// returns an unconditional ACCEPT for a NoOp mempool. Neither `ValidateVoteExtensions` nor the
// LastCommit cross-check runs there. The PreBlocker, gated on the WIDER predicate, then aggregated that
// same unverified envelope and wrote the seed, the contributor count and the contributor power — and
// `aggregateSeed` derives the BFT 2/3 power floor as a ratio of powers read from the envelope itself,
// so the floor is met by construction.
//
// The consequence is not a nuisance: that seed freezes committees, i.e. decides who is paid. A proposer
// holding the enable-height slot could compute the draw offline and choose whether to inject.
//
// ⚠️ AND IT IS REACHABLE BY THE REPOSITORY'S OWN TOOLING: `dendra_chain_rescue.sh` sets
// `enable_height = initial_height + 1`, which is precisely the block where the whole restored backlog
// of pending reveals and audits comes due.
//
// The fix gates the CONSUMPTION on `voteExtensionsActive` while leaving the hash write wide. It costs
// nothing: CometBFT only delivers extensions from E+1, so at E a well-formed envelope cannot exist —
// only a forged one can.
//
// These cases drive the two predicates directly. Driving the PreBlocker end to end would need a full
// app with a store, which this package's other cases deliberately avoid; what is asserted here is the
// exact relation the defect lived in, plus the delegation behaviour that made it invisible.

func TestTheTwoGatesDifferByExactlyOneHeightAndOnlyAtTheEnableHeight(t *testing.T) {
	const E = int64(7)

	// AT the enable height: recording yes, consuming no. That gap is the defect's whole surface.
	require.True(t, voteExtensionsRecording(contexteVE(E), E),
		"the block hash of E must be written: the aggregation at E+1 reads it back")
	require.False(t, voteExtensionsActive(contexteVE(E), E),
		"CometBFT delivers no extension at E, so nothing may be consumed there")

	// BELOW: neither.
	require.False(t, voteExtensionsRecording(contexteVE(E), E-1))
	require.False(t, voteExtensionsActive(contexteVE(E), E-1))

	// ABOVE: both. The gap is one height wide and no wider -- otherwise the fix below would silently
	// disable the feature rather than delay it by a block.
	for _, h := range []int64{E + 1, E + 2, E + 100} {
		require.True(t, voteExtensionsRecording(contexteVE(E), h), "recording at %d", h)
		require.True(t, voteExtensionsActive(contexteVE(E), h), "consuming at %d", h)
	}

	// DORMANT (enable_height = 0): neither predicate ever fires, at any height.
	require.False(t, voteExtensionsRecording(contexteVE(0), E))
	require.False(t, voteExtensionsActive(contexteVE(0), E))
}

// AT THE ENABLE HEIGHT, THE PROPOSAL HANDLER DELEGATES THE ENVELOPE WITHOUT VALIDATING IT — and the
// real `next` in production accepts unconditionally. This case pins the delegation; the spy stands in
// for `next`, and the comment above says what the production `next` actually is.
func TestAtTheEnableHeightTheEnvelopeIsDelegatedUnvalidated(t *testing.T) {
	const E = int64(7)
	espion := &espionSuivant{}
	appele := false
	h := newProcessProposalHandler(espion.handler,
		func(_ sdk.Context, _ abci.ExtendedCommitInfo, _ int64) error {
			appele = true
			return nil
		})

	req := &abci.RequestProcessProposal{Height: E, Txs: [][]byte{enveloppeVE(t), []byte("tx-1")}}
	resp, err := h(contexteVE(E), req)
	require.NoError(t, err)
	require.Equal(t, abci.ResponseProcessProposal_ACCEPT, resp.Status)

	require.False(t, appele,
		"at the enable height the extensions are NEVER validated: the handler returns before that")
	require.True(t, espion.appele, "the whole block is handed to next...")
	require.Len(t, espion.txsVus, 2,
		"...INCLUDING the envelope, which next has no reason to inspect -- and in production next is an "+
			"unconditional ACCEPT")

	// ONE HEIGHT LATER the same envelope IS validated: the gap is a height, not a mode.
	appele = false
	req2 := &abci.RequestProcessProposal{Height: E + 1, Txs: [][]byte{enveloppeVE(t), []byte("tx-1")}}
	_, err = h(contexteVE(E), req2)
	require.NoError(t, err)
	require.True(t, appele, "from E+1 the envelope goes through the validation the enable height skips")
}

// THE DECISION THE PRE-BLOCKER ACTUALLY CALLS. This is the case that was missing: the condition lived
// inline in a closure, and removing its `voteExtensionsActive` term left the WHOLE module green —
// measured. `preBlockerMayAggregate` is now named and invoked by the shipped PreBlocker, so driving it
// here drives the line production runs.
func TestThePreBlockerRefusesToAggregateAtTheEnableHeight(t *testing.T) {
	const E = int64(7)
	env := [][]byte{enveloppeVE(t), []byte("tx-1")}

	require.False(t, preBlockerMayAggregate(contexteVE(E), E, env),
		"at the enable height NOTHING validated this envelope, so nothing may be read from it")

	// ...and one height later the same envelope IS aggregated: the refusal delays by a block, it does
	// not disable the feature. Without this half the fix could be a permanent shutdown and look correct.
	require.True(t, preBlockerMayAggregate(contexteVE(E), E+1, env),
		"from E+1 ProcessProposal has validated the envelope, so the PreBlocker may consume it")
	require.True(t, preBlockerMayAggregate(contexteVE(E), E+100, env))

	// The other two terms still hold: no envelope, no aggregation.
	require.False(t, preBlockerMayAggregate(contexteVE(E), E+1, nil), "no transaction at all")
	require.False(t, preBlockerMayAggregate(contexteVE(E), E+1, [][]byte{[]byte("tx-1")}),
		"a block whose first transaction is not an envelope")

	// Dormant: never.
	require.False(t, preBlockerMayAggregate(contexteVE(0), E+1, env))
}
