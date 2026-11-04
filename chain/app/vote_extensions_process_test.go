package app

import (
	"testing"

	"cosmossdk.io/log"
	abci "github.com/cometbft/cometbft/abci/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// A vote-extensions injection does not exempt the block from its checks.
//
// `ProcessProposal` has two branches: injection present and injection absent. The absent branch
// delegates to the default handler, which decodes every transaction and applies the proposal gas
// limit. If the present branch accepted without delegating, that check would disappear exactly when
// `vote_extensions_enable_height` is reached, at the moment the decentralized VRF arms itself. A guard
// that switches off when its feature switches on is worse than a missing guard, because nobody sees
// it go.
//
// These cases verify that both branches go through `next`, and that `next` receives the block without
// the envelope (which is not a transaction and does not decode).

// veContext builds a minimal context: a real logger (the reject branch emits one) and the consensus
// parameters that decide whether vote extensions are active.
func veContext(enableHeight int64) sdk.Context {
	ctx := sdk.NewContext(nil, cmtproto.Header{}, false, log.NewNopLogger())
	return ctx.WithConsensusParams(cmtproto.ConsensusParams{
		Abci: &cmtproto.ABCIParams{VoteExtensionsEnableHeight: enableHeight},
	})
}

// nextSpy records what the delegated handler receives.
type nextSpy struct {
	called  bool
	txsSeen [][]byte
}

func (e *nextSpy) handler(_ sdk.Context, req *abci.RequestProcessProposal) (*abci.ResponseProcessProposal, error) {
	e.called = true
	e.txsSeen = req.Txs
	return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_ACCEPT}, nil
}

func veEnvelope(t *testing.T) []byte {
	t.Helper()
	bz, err := (&abci.ExtendedCommitInfo{Round: 1}).Marshal()
	require.NoError(t, err)
	return append(append([]byte{}, vrfInjectPrefix...), bz...)
}

// (1) Injection present: the delegated handler is called and receives the block stripped of the
// envelope. This is the case that fails when the handler accepts without delegating.
func TestProcessProposalInjectionDelegatesTheRestOfTheBlock(t *testing.T) {
	spy := &nextSpy{}
	h := newProcessProposalHandler(spy.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil })

	inj := veEnvelope(t)
	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{inj, []byte("tx-a"), []byte("tx-b")}}
	resp, err := h(veContext(1), req)
	require.NoError(t, err)
	require.Equal(t, abci.ResponseProcessProposal_ACCEPT, resp.Status)

	require.True(t, spy.called,
		"a validated injection answered accept without submitting the block to the default handler: "+
			"transaction validation and the proposal gas limit are skipped as soon as vote extensions "+
			"are active")
	require.Equal(t, [][]byte{[]byte("tx-a"), []byte("tx-b")}, spy.txsSeen,
		"the delegated handler must receive req.Txs[1:]: the extensions envelope is not a transaction")
	require.Len(t, req.Txs, 3, "the original ABCI request must not be truncated in place")
}

// (2) Injection absent: full delegation, and the dormant behaviour is unchanged.
func TestProcessProposalWithoutInjectionDelegatesEverything(t *testing.T) {
	spy := &nextSpy{}
	h := newProcessProposalHandler(spy.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil })

	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{[]byte("tx-a")}}
	_, err := h(veContext(1), req)
	require.NoError(t, err)
	require.True(t, spy.called)
	require.Equal(t, [][]byte{[]byte("tx-a")}, spy.txsSeen)
}

// (3) Vote extensions dormant: a leading transaction that looks like an envelope stays an ordinary
// transaction, passed through to the default handler (no special handling before activation).
func TestProcessProposalDormantIgnoresThePrefix(t *testing.T) {
	spy := &nextSpy{}
	h := newProcessProposalHandler(spy.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error {
			t.Fatal("no extension validation must happen while vote extensions are dormant")
			return nil
		})

	inj := veEnvelope(t)
	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{inj}}
	_, err := h(veContext(0), req)
	require.NoError(t, err)
	require.True(t, spy.called)
	require.Equal(t, [][]byte{inj}, spy.txsSeen)
}

// (4) Invalid injection: reject, and the block is never delegated. A block whose envelope does not
// hold is not validated, since accepting the rest would reward a forged envelope.
func TestProcessProposalInvalidInjectionIsRejected(t *testing.T) {
	for name, tc := range map[string]struct {
		txs      [][]byte
		validate func(sdk.Context, abci.ExtendedCommitInfo, int64) error
	}{
		"undecodable envelope": {
			txs:      [][]byte{append(append([]byte{}, vrfInjectPrefix...), 0xff, 0xff, 0xff)},
			validate: func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil },
		},
		"signatures rejected": {
			txs:      [][]byte{veEnvelope(t)},
			validate: func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return errValidationVE },
		},
	} {
		t.Run(name, func(t *testing.T) {
			spy := &nextSpy{}
			h := newProcessProposalHandler(spy.handler, tc.validate)
			resp, err := h(veContext(1), &abci.RequestProcessProposal{Height: 10, Txs: tc.txs})
			require.NoError(t, err)
			require.Equal(t, abci.ResponseProcessProposal_REJECT, resp.Status)
			require.False(t, spy.called, "a rejected block must not be delegated")
		})
	}
}

var errValidationVE = errTest("invalid vote-extension signatures")

type errTest string

func (e errTest) Error() string { return string(e) }
