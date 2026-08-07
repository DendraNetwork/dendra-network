package app

import (
	"testing"

	"cosmossdk.io/log"
	abci "github.com/cometbft/cometbft/abci/types"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"
)

// ═══ A VOTE-EXTENSIONS INJECTION DOES NOT EXEMPT THE BLOCK FROM ITS CHECKS ═══════════════════════
//
// `ProcessProposal` has two branches: injection present, injection absent. The "absent" branch
// delegates to the default handler, which decodes every transaction and applies the proposal gas
// limit. If the "present" branch answered ACCEPT without delegating, that check would disappear —
// and it would disappear exactly when `vote_extensions_enable_height` is reached, that is, at the
// moment the decentralized VRF arms itself. A guard that switches off when the feature it accompanies
// switches on is worse than a missing guard: nobody sees it go.
//
// These cases verify the property that was missing — BOTH branches go through `next` — and that what
// `next` receives is the block WITHOUT the envelope (which is not a transaction and does not decode).

// contexteVE builds a minimal context: a real logger (the reject branch emits one) and the consensus
// parameters that decide whether vote extensions are active.
func contexteVE(enableHeight int64) sdk.Context {
	ctx := sdk.NewContext(nil, cmtproto.Header{}, false, log.NewNopLogger())
	return ctx.WithConsensusParams(cmtproto.ConsensusParams{
		Abci: &cmtproto.ABCIParams{VoteExtensionsEnableHeight: enableHeight},
	})
}

// espionSuivant records what the delegated handler receives.
type espionSuivant struct {
	appele bool
	txsVus [][]byte
}

func (e *espionSuivant) handler(_ sdk.Context, req *abci.RequestProcessProposal) (*abci.ResponseProcessProposal, error) {
	e.appele = true
	e.txsVus = req.Txs
	return &abci.ResponseProcessProposal{Status: abci.ResponseProcessProposal_ACCEPT}, nil
}

func enveloppeVE(t *testing.T) []byte {
	t.Helper()
	bz, err := (&abci.ExtendedCommitInfo{Round: 1}).Marshal()
	require.NoError(t, err)
	return append(append([]byte{}, vrfInjectPrefix...), bz...)
}

// (1) INJECTION PRESENT — the delegated handler is called, and it receives the block STRIPPED of the
// envelope. This is the case that fails on a form answering ACCEPT without delegating.
func TestProcessProposalInjectionDelegatesTheRestOfTheBlock(t *testing.T) {
	espion := &espionSuivant{}
	h := newProcessProposalHandler(espion.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil })

	inj := enveloppeVE(t)
	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{inj, []byte("tx-a"), []byte("tx-b")}}
	resp, err := h(contexteVE(1), req)
	require.NoError(t, err)
	require.Equal(t, abci.ResponseProcessProposal_ACCEPT, resp.Status)

	require.True(t, espion.appele,
		"a validated injection answered ACCEPT without submitting the block to the default handler: "+
			"transaction validation and the proposal gas limit are skipped as soon as vote extensions "+
			"are active")
	require.Equal(t, [][]byte{[]byte("tx-a"), []byte("tx-b")}, espion.txsVus,
		"the delegated handler must receive req.Txs[1:]: the extensions envelope is not a transaction")
	require.Len(t, req.Txs, 3, "the original ABCI request must not be truncated in place")
}

// (2) INJECTION ABSENT — full delegation; the dormant behaviour is strictly unchanged.
func TestProcessProposalWithoutInjectionDelegatesEverything(t *testing.T) {
	espion := &espionSuivant{}
	h := newProcessProposalHandler(espion.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil })

	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{[]byte("tx-a")}}
	_, err := h(contexteVE(1), req)
	require.NoError(t, err)
	require.True(t, espion.appele)
	require.Equal(t, [][]byte{[]byte("tx-a")}, espion.txsVus)
}

// (3) VOTE EXTENSIONS DORMANT — a leading transaction that looks like an envelope remains an ordinary
// transaction, passed through to the default handler (no privileged handling before activation).
func TestProcessProposalDormantIgnoreLePrefixe(t *testing.T) {
	espion := &espionSuivant{}
	h := newProcessProposalHandler(espion.handler,
		func(sdk.Context, abci.ExtendedCommitInfo, int64) error {
			t.Fatal("no extension validation must happen while vote extensions are dormant")
			return nil
		})

	inj := enveloppeVE(t)
	req := &abci.RequestProcessProposal{Height: 10, Txs: [][]byte{inj}}
	_, err := h(contexteVE(0), req)
	require.NoError(t, err)
	require.True(t, espion.appele)
	require.Equal(t, [][]byte{inj}, espion.txsVus)
}

// (4) INVALID INJECTION — reject, and the block is never delegated: a block whose envelope does not
// hold is not validated, because accepting the rest would reward a forged envelope.
func TestProcessProposalInjectionInvalideRejette(t *testing.T) {
	for nom, cas := range map[string]struct {
		txs      [][]byte
		validate func(sdk.Context, abci.ExtendedCommitInfo, int64) error
	}{
		"undecodable envelope": {
			txs:      [][]byte{append(append([]byte{}, vrfInjectPrefix...), 0xff, 0xff, 0xff)},
			validate: func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return nil },
		},
		"signatures rejected": {
			txs:      [][]byte{enveloppeVE(t)},
			validate: func(sdk.Context, abci.ExtendedCommitInfo, int64) error { return errValidationVE },
		},
	} {
		t.Run(nom, func(t *testing.T) {
			espion := &espionSuivant{}
			h := newProcessProposalHandler(espion.handler, cas.validate)
			resp, err := h(contexteVE(1), &abci.RequestProcessProposal{Height: 10, Txs: cas.txs})
			require.NoError(t, err)
			require.Equal(t, abci.ResponseProcessProposal_REJECT, resp.Status)
			require.False(t, espion.appele, "a rejected block must not be delegated")
		})
	}
}

var errValidationVE = errTest("invalid vote-extension signatures")

type errTest string

func (e errTest) Error() string { return string(e) }
